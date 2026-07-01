# Router v1.3 使用方法 + K8s 部署

v1.3 在原有 `roundrobin` / `session` / `cache_aware_load_balancing` 之外，新增路由模式 **`load_balanced_affinity`**：P2C 负载摆放 + 自卸载会话亲和 + 死引擎守卫，去掉所有延迟/队列硬门。真机 8×H100 实测：同 8 卡满载比轮询 **+13% 吞吐、−26% E2E p99**，QPS 均衡接近轮询，亲和命中 31.7%（轮询 13.8%）。

- 镜像：`ssadds/production-stack-router:v1.3.1`（含 redis 共享亲和 / 软依赖+断路器 / 容量信号，见 `router-v1.3.1-changelog.md`）
- 入口：`vllm-router`（容器 ENTRYPOINT 已是它，Helm/manifest 直接传参即可）

## 1. 参数速查（load_balanced_affinity）

| 参数                                       | 默认    | 说明                                                                                              |
| ------------------------------------------ | ------- | ------------------------------------------------------------------------------------------------- |
| `--routing-logic load_balanced_affinity` | —      | 启用新模式                                                                                        |
| `--session-key <header>`                 | —      | **必填**（开亲和时）。亲和键，用线上的会话 header，如 `X-Flow-Conversation-Id`            |
| `--lb-affinity` / `--no-lb-affinity`   | on      | 开/关会话亲和。关掉 = 纯 P2C 负载均衡                                                             |
| `--lb-d-choices`                         | 2       | P2C 采样引擎数。2 是经典甜点，调高收紧均衡但收益递减                                              |
| `--lb-affinity-slack`                    | 0       | 回访会话黏性：原引擎负载在新 P2C 选择 ±slack 个请求内就贴回。调大=更黏（更多缓存复用、均衡略松） |
| `--lb-affinity-ttl`                      | 3600    | 会话→引擎映射记忆秒数，过期按首访重摆                                                            |
| `--lb-affinity-max-size`                 | 0(无界) | 亲和表 LRU 上限。**会话基数大务必设**（如 200000），防内存无界                              |
| `--lb-inflight-decay`                    | 300     | 在飞计数安全上限秒数，丢弃没观测到完成的派发（防泄漏），设到大于最长请求                          |
| `--lb-stats-window`                      | 30      | 亲和命中率指标的滑窗秒数（仅指标，不影响路由）                                                    |
| `--engine-stats-interval`                | —      | 抓引擎负载的间隔秒数（线上用 2）                                                                  |
| `--router-workers`                       | 1       | 每副本 worker 数（线上 4）。注意亲和表**按 worker/副本独立**                                |

> 多副本/多 worker：亲和映射在内存、**按副本独立**。同一会话命中别的副本会按负载重摆——均衡不受影响，只少一次缓存命中（TTFT 偶发抬升）。跨副本一致亲和需共享存储（暂未实现）。

## 2. K8s 部署 —— Helm（推荐）

Helm chart 已内建：`routerSpec.routingLogic` 直接选模式，`--lb-*` 走 `routerSpec.extraArgs`；k8s 服务发现、RBAC、Service 都自动生成。

`values-lb.yaml`：

```yaml
routerSpec:
  repository: "ssadds/production-stack-router"
  tag: "v1.3"
  imagePullPolicy: "IfNotPresent"
  enableRouter: true
  replicaCount: 3                 # 按 QPS/引擎数定，见下方"规模与抓取"

  serviceDiscovery: "k8s"         # 自动发现带标签的 vLLM pod
  routingLogic: "load_balanced_affinity"
  sessionKey: "X-Flow-Conversation-Id"   # 必填：线上会话 header

  engineScrapeInterval: 2         # 抓引擎负载间隔（与线上一致）
  requestStatsWindow: 60

  # load_balanced_affinity 专属旗标走 extraArgs（注意 flag 与值要拆成两个元素）
  extraArgs:
    - "--lb-d-choices"
    - "2"
    - "--lb-affinity-max-size"
    - "200000"                    # 会话基数大→给亲和表封顶，防内存无界
    - "--lb-affinity-slack"
    - "0"                         # 想更黏(更多缓存复用)就调到 1~2
    - "--k8s-reconcile-interval"
    - "30"                        # 清 ghost 引擎，与死引擎守卫互补

  serviceType: ClusterIP
  servicePort: 80
  containerPort: 8000
  resources:                      # 实测单副本 RSS 峰值 ~650MiB，下面很宽裕
    requests: { cpu: "2", memory: "4G" }
    limits:   { cpu: "4", memory: "8G" }

# 关键：路由按这个标签选择器发现 vLLM 引擎 pod；引擎 Deployment 要带同样的 labels
servingEngineSpec:
  enableEngine: true
  labels:
    model: "gemma4-31B-it-FP8"
  # ... 你的引擎配置（镜像、资源、模型等）
```

部署：

```bash
helm upgrade --install vllm ./helm -n <namespace> --create-namespace -f values-lb.yaml
kubectl -n <namespace> rollout status deploy/vllm-deployment-router
kubectl -n <namespace> logs deploy/vllm-deployment-router | grep -i "load-balanced affinity"  # 确认模式
```

> `--lb-affinity` 默认就是开的，不必写进 extraArgs；要纯负载均衡才显式加 `--no-lb-affinity`。
> 引擎侧用 k8s 服务发现时，vLLM pod 必须带 `servingEngineSpec.labels` 里的标签、并在 `--k8s-port`（默认 8000）暴露 OpenAI + `/metrics` 接口。

## 3. K8s 部署 —— 原生 manifest（不用 Helm 时）

k8s 服务发现要求 router 能 `list/watch` 同 namespace 的 pod，所以**必须配 RBAC**。下面是自包含的最小集（替换 `NS` / 标签 / 镜像）：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata: { name: vllm-router-sa, namespace: NS }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: vllm-router-pod-reader, namespace: NS }
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "watch", "list", "patch"]   # 服务发现 + 死引擎清理需要
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: vllm-router-pod-reader-binding, namespace: NS }
subjects:
  - kind: ServiceAccount
    name: vllm-router-sa
    namespace: NS
roleRef: { kind: Role, name: vllm-router-pod-reader, apiGroup: rbac.authorization.k8s.io }
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: vllm-router, namespace: NS, labels: { app: vllm-router } }
spec:
  replicas: 3
  selector: { matchLabels: { app: vllm-router } }
  template:
    metadata: { labels: { app: vllm-router } }
    spec:
      serviceAccountName: vllm-router-sa
      containers:
        - name: router
          image: ssadds/production-stack-router:v1.3.1
          imagePullPolicy: IfNotPresent
          # 引擎要 API key 时，让 router 透传（按需）：
          # env:
          #   - name: VLLM_API_KEY
          #     valueFrom: { secretKeyRef: { name: vllm-secrets, key: vllmApiKey } }
          args:
            - "--host"
            - "0.0.0.0"
            - "--port"
            - "8000"
            - "--service-discovery"
            - "k8s"
            - "--k8s-namespace"
            - "NS"
            - "--k8s-label-selector"
            - "model=gemma4-31B-it-FP8"        # 选你的引擎 pod
            - "--k8s-port"
            - "8000"
            - "--routing-logic"
            - "load_balanced_affinity"
            - "--session-key"
            - "X-Flow-Conversation-Id"
            - "--engine-stats-interval"
            - "2"
            - "--request-stats-window"
            - "60"
            - "--lb-d-choices"
            - "2"
            - "--lb-affinity-max-size"
            - "200000"
            - "--k8s-reconcile-interval"
            - "30"
            - "--router-workers"
            - "4"
          ports:
            - { name: http, containerPort: 8000 }
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 30
            periodSeconds: 5
          startupProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 12
---
apiVersion: v1
kind: Service
metadata: { name: vllm-router, namespace: NS }
spec:
  selector: { app: vllm-router }
  ports:
    - { name: http, port: 80, targetPort: 8000 }
  type: ClusterIP
```

> `args` 里每个 flag 和它的值各占一个列表元素（如上）。`--session-key` 不能省：开亲和时 parser 会校验缺失会启动失败。把 `NS` 换成你的 namespace、标签换成引擎实际标签。

应用：

```bash
kubectl apply -f router.yaml
kubectl -n NS rollout status deploy/vllm-router
```

## 4. 验证与关键指标

```bash
# 服务内访问（带会话 header 才走亲和）
curl -s http://vllm-router.NS.svc/v1/completions \
  -H "Content-Type: application/json" \
  -H "X-Flow-Conversation-Id: conv-123" \
  -d '{"model":"gemma4-31B-it-FP8","prompt":"hi","max_tokens":16}'

# 路由健康度（压测进行中抓，QPS 窗口才不衰减）
kubectl -n NS exec deploy/vllm-router -- sh -c 'curl -s localhost:8000/metrics' | grep -E \
  "vllm:current_qps|load_balanced_(placement|affinity_hit_total|affinity_shed_total|affinity_hit_rate)"
```

- `vllm:current_qps{server=}` 各引擎应接近（均衡）；
- `load_balanced_affinity_hit_rate` 高 = 回访多数贴回原引擎；`affinity_shed_total` 随负载上升 = 在自卸载；
- 不变式：`placement_total + affinity_hit_total + affinity_shed_total ≈ 成功请求数`。

## 5. 规模与抓取（100–150 引擎时）

每引擎被抓频率 = `副本数 × worker数 / engine-stats-interval`。副本多时这是扩展瓶颈：

- 先按 `replicaCount` 适度起（如 3–6），`engine-stats-interval` 保 2；
- 副本数 × worker 偏大导致抓取过密时，优先减副本/worker 或拉大 interval；
- 跨副本一致亲和 + 共享负载快照（leader 单采集→Redis）属后续工作，当前每副本独立采集、独立亲和。

## 6. 调参建议（基于真机结果）

- **`--lb-affinity-slack`**：默认 0（均衡优先）。小集群粘滞差或想多吃缓存→调到 1~2，黏性增强、均衡略松。
- **`--lb-affinity-max-size`**：会话基数大（线上 1.4w+ 活跃对话）务必设（如 200000），否则亲和表只靠 TTL 回收，内存可能涨。
- **`--lb-d-choices`**：2 足够；引擎极多且想更紧均衡可试 3，收益递减。
- **KV offload**：实测**低压略负、高压(+17% 吞吐)才正收益**——按负载/显存压力决定是否在引擎侧开 `SimpleCPUOffloadConnector`，不建议无脑常开。
