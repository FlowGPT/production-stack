# Cache-Aware 回访会话指标（Returning-Session Funnel）

> 本文档**只**描述回访会话可观测性，与 cache-aware 路由核心（过载判定、fallback、inflight 等，见 `cache-aware-worklog.md`）分开维护。**本功能不改变任何路由决策**，只新增指标。

## 1. 背景与目标

典型部署中，**上层多 Provider 调度**的会话粘滞往往做得不好（同一会话可能被切到不同 Provider）；而**本 Provider 内的 Router** 负责把同一 `session id` 粘到固定引擎（cache-aware）。

原有 `cache_aware_stickiness_rate` / `cache_aware_fallback_rate` 统计**所有**带 session 的请求。生产中大量 session 只来一次，这些「一次性」访问会抬高粘滞率、掩盖真正回访用户的粘滞情况。

我们要从**本 Provider 视角**看清三件事：

1. **新请求**有多少（某 session 在本 Provider 第一次出现）
2. **回访请求**有多少（同一 session 在 TTL 内再次打到本 Provider）
3. 在**回访请求**里，Router 的**粘滞率 / fallback 率**是多少

## 2. 模型：请求漏斗

把所有带 `session id` 的路由请求集合记为 `U`，在 TTL 内按「本 Router 是否见过该 session」切成两个**不相交**子集：

```
U = N（首次访问）∪ R（回访）          |U| = |N| + |R|
|N| = N_sticky + N_fallback
|R| = R_sticky + R_fallback
```

- **首次访问 N**：该 `session id` 在 TTL 内**第一次**被本 Router 路由（一次性 session，或某会话的第一跳）。
- **回访 R**：该 `session id` 在 TTL 内**第 2 次及以后**的路由（说明上层这次把同一会话送回了本 Provider）。
- 每个请求再按「粘性引擎是否过载」分 sticky / fallback，语义与原 `cache_aware_*` 完全一致。

判定时机：在路由决策**之前**调用 store 记录本次访问——第一次返回「首次」，之后返回「回访」——因此一个 session 的第一条请求必为首次，其余为回访。

> 「回访」= 回到**本 Router 进程/集群**视角，不是全局用户回访。上层把会话切到别的 Provider 时，那一跳在本 Provider 仍算「首次」——这恰恰是诊断上层粘滞的信号。

## 3. 指标

### Counter（累积，单调；用 `rate()` 做长期 SLO）

| 指标 | 含义 |
| --- | --- |
| `vllm:cache_aware_first_visit_routed_total` | `\|N\|` 首次访问路由数 |
| `vllm:cache_aware_first_visit_sticky_total` | 首次访问且 sticky |
| `vllm:cache_aware_first_visit_fallback_total` | 首次访问且 fallback |
| `vllm:cache_aware_returning_routed_total` | `\|R\|` 回访路由数 |
| `vllm:cache_aware_returning_sticky_total` | 回访且 sticky |
| `vllm:cache_aware_returning_fallback_total` | 回访且 fallback |

**不变量**（与原有 `cache_aware_sticky_total` / `cache_aware_fallback_total` 对齐）：

```
first_visit_routed   + returning_routed   = sticky_total + fallback_total
first_visit_sticky   + returning_sticky   = sticky_total
first_visit_fallback + returning_fallback = fallback_total
```

### Gauge（滑动窗口 `--cache-aware-stats-window`，实时看盘；无流量时衰减到 0）

| 指标 | 公式 |
| --- | --- |
| `vllm:cache_aware_first_visit_request_ratio` | `\|N\| / \|U\|` |
| `vllm:cache_aware_returning_request_ratio` | `\|R\| / \|U\|` |
| `vllm:cache_aware_returning_stickiness_rate` | `R_sticky / \|R\|` |
| `vllm:cache_aware_returning_fallback_rate` | `R_fallback / \|R\|` |

后两者只在**回访子集 R** 上计算，一次性首访不会稀释分母。

### 存储健康（仅 Redis 后端）

| 指标 | 含义 |
| --- | --- |
| `vllm:cache_aware_returning_session_store_errors_total{operation}` | Redis 失败次数（fail-open，路由不受影响）；持续上涨说明回访指标失真，需查 Redis |

## 4. 推荐 PromQL

回访请求占比（流量里多少是「会话再次回到本 Provider」）：

```promql
sum(rate(vllm:cache_aware_returning_routed_total[5m]))
/
sum(rate(vllm:cache_aware_first_visit_routed_total[5m])
    + rate(vllm:cache_aware_returning_routed_total[5m]))
```

回访粘滞率（只看回访子集）：

```promql
sum(rate(vllm:cache_aware_returning_sticky_total[5m]))
/
sum(rate(vllm:cache_aware_returning_routed_total[5m]))
```

回访 fallback 率：

```promql
sum(rate(vllm:cache_aware_returning_fallback_total[5m]))
/
sum(rate(vllm:cache_aware_returning_routed_total[5m]))
```

> 说明：以上是**请求维度**（一个 session 来 N 次贡献 1 次首访 + N-1 次回访），不是「独立 session 维度」。要精确统计「有多少独立 session、其中多少会回访」需要额外的 session 级计数器，本功能未实现。

## 5. 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--cache-aware-returning-session-ttl` | 3600 | 回访识别 TTL（秒）。session 在 TTL 内再次出现才算回访；超时后重来视为新会话。仅影响指标，不改路由。 |
| `--cache-aware-returning-session-store` | memory | `memory`（单副本）或 `redis`（多副本共享回访状态）。 |
| `--cache-aware-returning-session-redis-url` | — | `store=redis` 时**必填**，如 `redis://host:6379/0`。 |
| `--cache-aware-returning-session-redis-key-prefix` | `vllm:returning-session:` | Redis key 前缀。 |
| `--cache-aware-returning-session-redis-timeout` | 0.05 | 单次 Redis socket 超时（秒）；超时按首次访问计（fail-open）。 |
| `--cache-aware-returning-session-max-size` | 0 | `store=memory` 的 LRU 上限（0=不限，仅按 TTL 淘汰）。unique session 基数极高时设上限防内存膨胀。 |
| `--cache-aware-returning-session-local-cache-size` | 0 | `store=redis` 时，用每副本 LRU 缓存「已确认回访」的 session id，跳过 Redis 往返（适合 LB 亲和）；Redis 仍是跨副本首次接触的权威。0=关闭。 |

## 6. 后端：memory vs redis

| | memory（默认） | redis |
| --- | --- | --- |
| 适用 | 单 Router 副本 | 多 Router 副本 |
| 状态 | 进程内 dict，惰性 TTL 淘汰 | 共享一个 Redis，Redis TTL 淘汰 |
| 多副本回访指标 | 偏低（同 session 打到别的副本算「首次」） | 正确（全副本共享「见过没」） |
| Router 内存 | 随 TTL 内 unique session 增长 | 不随 unique session 增长 |
| 每请求开销 | O(1) 摊还（见下） | 1 次 Redis 往返（单条 Lua：`EXISTS`+`SET EX`） |

**Redis key**：`{prefix}{xxhash64(session_id)}`，避免超长/特殊字符撑大 key。

### 每请求开销（实测，本机 loopback）

`visit()` 是本功能加到路由热路径上的**唯一**开销。

| 后端 | 单次 visit 均值 | 说明 |
| --- | --- | --- |
| memory | **~0.9 µs（与 session 数无关）** | `OrderedDict` 按时间有序，淘汰只弹队头过期项，O(1) 摊还，无全表扫描 |
| redis（同节点） | ~0.18 ms | 一次 TCP 往返；同 AZ 跨节点 ~0.3–0.8ms、跨 AZ ~1–3ms |
| redis + 本地缓存 | 亲和场景 **~0.02 ms** | 命中本地（约访问 9/10）跳过往返，未命中才查 Redis |

放进端到端看，Redis 模式每请求多 ~0.18ms，相对 Router 转发 p50（~7.5ms）约 2–3%，相对 LLM 推理（百 ms~秒级）可忽略。memory 模式开销可忽略不计。

### 故障策略（启动 fail-hard，运行期 fail-open）

- **启动时** Redis 连不上：**fail-hard**——直接报错退出，不静默降级为 memory。因为你显式选了 `store=redis`（多副本共享），悄悄退化成 per-replica 会让回访指标长期失真却无人察觉。K8s 下 Pod 会 crash-loop 重启，直到 Redis 可达，问题立即暴露。
- **运行时**（已启动后）Redis 超时/报错：本次 `visit()` 返回「首次访问」，**路由决策不变**（推理不受影响），并递增 `cache_aware_returning_session_store_errors_total{operation="visit"}`。
- Redis 恢复后无需重启，下一笔请求自动恢复读写。

> 即：启动时把「连不上 Redis」当配置错误（硬失败、要修）；运行中把「Redis 抖动」当可容忍故障（软失败、保推理）。

> 客户端为连接池复用（`redis.Redis.from_url`），不是每请求新建 TCP。

## 7. 多副本只解决「回访指标」，不解决路由共享

Redis 仅共享「这个 session 见过没」，**不参与选引擎**。以下多副本局限**依旧存在**（与本功能无关）：

- `cache_aware_inflight_requests` 仍是 per-replica 本地计数
- 引擎 queue/running 仍靠周期 scrape，有滞后
- 过载 fallback、hash ring 选引擎逻辑不变

## 8. 镜像构建

`store=redis` 需要 `redis` 依赖。默认 Dockerfile 已包含：

```dockerfile
ARG INSTALL_OPTIONAL_DEP=semantic_cache,redis
```

直接构建即可（与 v1.0 一致的 `semantic_cache` + 新增 `redis`，不含 lmcache）：

```bash
docker build -t <your-repo>/production-stack-router:v1.1 -f docker/Dockerfile .
```

## 9. K8s 多副本部署（完整示例）

**所有 Router 副本共用 1 个 Redis Service**（不是每副本一个；生产建议这 1 个 Redis 做 HA）。

```
Client → Service → Router Pod 1 ─┐
                   Router Pod 2 ─┼→ Redis ×1（仅回访指标）
                   Router Pod N ─┘
                 → vLLM Engine Pods（路由照旧，与 Redis 无关）
```

下面给两条完整路径：**9.1 Redis 是公共前置**，然后二选一——**9.2 Helm**（production-stack 标准部署方式，推荐）或 **9.3 裸 manifest**（不用 Helm 时）。示例 namespace 用 `inference`，按需替换。

### 9.1 部署 Redis（公共，两种方式都需要）

`redis.yaml`：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: inference
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-router-redis
  namespace: inference
  labels: {app: vllm-router-redis}
spec:
  replicas: 1
  selector:
    matchLabels: {app: vllm-router-redis}
  template:
    metadata:
      labels: {app: vllm-router-redis}
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          # maxmemory 设到 limit 的 ~75%，到顶时优先淘汰最接近过期的 key
          # （所有 key 都带 TTL）；router 是 fail-open，淘汰也不影响推理。
          args: ["redis-server", "--maxmemory", "400mb", "--maxmemory-policy", "volatile-ttl"]
          ports: [{containerPort: 6379, name: redis}]
          resources:
            requests: {cpu: "100m", memory: "256Mi"}
            limits: {memory: "512Mi"}
          readinessProbe:
            tcpSocket: {port: 6379}
            initialDelaySeconds: 2
            periodSeconds: 5
          livenessProbe:
            tcpSocket: {port: 6379}
            initialDelaySeconds: 10
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-router-redis
  namespace: inference
spec:
  selector: {app: vllm-router-redis}
  ports: [{port: 6379, targetPort: 6379}]
```

```bash
kubectl apply -f redis.yaml
kubectl get pods -n inference -l app=vllm-router-redis
```

集群内地址：`redis://vllm-router-redis.inference.svc.cluster.local:6379/0`（同 namespace 可简写 `redis://vllm-router-redis:6379/0`）。

> 已有 Redis Service（如 `kaon-router-gemma4-31b-redis`）直接用其 DNS 名，跳过本步。

### 9.2 方式一：Helm（推荐）

完整 `values.yaml` 片段（其余按你现有部署保留；关键是 `routerSpec`）：

```yaml
servingEngineSpec:
  runtimeClassName: ""
  modelSpec:
    - name: "gemma"
      repository: "vllm/vllm-openai"
      tag: "latest"
      modelURL: "RedHatAI/gemma-4-31B-it-NVFP4"
      replicaCount: 2
      requestCPU: 8
      requestMemory: "32Gi"
      requestGPU: 1

routerSpec:
  enableRouter: true
  replicaCount: 2                                   # 多副本才需要 redis
  repository: "ssadds/production-stack-router"
  tag: "v1.1"                                        # 含 redis 依赖的镜像
  routingLogic: "cache_aware_load_balancing"
  sessionKey: "x-session-id"                         # 与客户端 header 一致
  engineScrapeInterval: 3
  resources:
    requests: {cpu: "2", memory: "4Gi"}
    limits: {cpu: "4", memory: "8Gi"}
  extraArgs:
    # —— 原有 cache-aware 调优参数（按需）——
    - "--cache-aware-tolerate-waiting-requests"
    - "20"
    # —— 回访会话 + Redis（核心 4 项）——
    - "--cache-aware-returning-session-store"
    - "redis"
    - "--cache-aware-returning-session-redis-url"
    - "redis://vllm-router-redis.inference.svc.cluster.local:6379/0"
    - "--cache-aware-returning-session-ttl"
    - "1800"
    # —— 可选：本副本前置缓存，省 Redis 往返（LB 亲和时）——
    - "--cache-aware-returning-session-local-cache-size"
    - "100000"
```

```bash
helm upgrade --install <release> ./helm -n inference -f values.yaml
```

### 9.3 方式二：裸 manifest（不用 Helm）

Router 用 K8s 服务发现自动找引擎 Pod（按 label 选择）。`router.yaml`：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-router
  namespace: inference
  labels: {app: vllm-router}
spec:
  replicas: 2                                        # 多副本
  selector:
    matchLabels: {app: vllm-router}
  template:
    metadata:
      labels: {app: vllm-router}
    spec:
      serviceAccountName: vllm-router-sa             # 需有 list/watch pods 权限
      containers:
        - name: router
          image: ssadds/production-stack-router:v1.1
          args:
            - "--host"
            - "0.0.0.0"
            - "--port"
            - "8000"
            - "--service-discovery"
            - "k8s"
            - "--k8s-namespace"
            - "inference"
            - "--k8s-label-selector"
            - "model=gemma"                          # 选中你的引擎 Pod
            - "--k8s-port"
            - "8000"
            - "--routing-logic"
            - "cache_aware_load_balancing"
            - "--session-key"
            - "x-session-id"
            - "--engine-stats-interval"
            - "3"
            - "--cache-aware-tolerate-waiting-requests"
            - "20"
            - "--cache-aware-returning-session-store"
            - "redis"
            - "--cache-aware-returning-session-redis-url"
            - "redis://vllm-router-redis.inference.svc.cluster.local:6379/0"
            - "--cache-aware-returning-session-ttl"
            - "1800"
            - "--cache-aware-returning-session-local-cache-size"
            - "100000"
          ports: [{containerPort: 8000, name: http}]
          resources:
            requests: {cpu: "2", memory: "4Gi"}
            limits: {cpu: "4", memory: "8Gi"}
          readinessProbe:
            httpGet: {path: /health, port: 8000}
            initialDelaySeconds: 5
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-router-service
  namespace: inference
spec:
  selector: {app: vllm-router}
  ports: [{port: 80, targetPort: 8000}]
---
# K8s 服务发现需要列出/监听 Pod 的权限
apiVersion: v1
kind: ServiceAccount
metadata: {name: vllm-router-sa, namespace: inference}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: vllm-router-role, namespace: inference}
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: vllm-router-rb, namespace: inference}
subjects:
  - {kind: ServiceAccount, name: vllm-router-sa, namespace: inference}
roleRef: {kind: Role, name: vllm-router-role, apiGroup: rbac.authorization.k8s.io}
```

```bash
kubectl apply -f router.yaml
```

### 9.4 带密码的 Redis（生产建议）

```bash
kubectl create secret generic vllm-router-redis-auth -n inference \
  --from-literal=password='YOUR_PASSWORD'
```

Redis Deployment 容器改为：

```yaml
          args: ["redis-server", "--requirepass", "$(REDIS_PASSWORD)",
                 "--maxmemory", "400mb", "--maxmemory-policy", "volatile-ttl"]
          env:
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef: {name: vllm-router-redis-auth, key: password}
```

Router 的 redis-url 写成（密码用 Secret 注入到环境变量再拼，避免明文入库）：
`redis://:YOUR_PASSWORD@vllm-router-redis.inference.svc.cluster.local:6379/0`

## 10. 验证

```bash
# 1) Redis 通不通
kubectl run -it --rm redis-test -n inference --image=redis:7-alpine -- \
  redis-cli -h vllm-router-redis ping            # 期望 PONG

# 2) Service 后面有 Pod（Endpoints 非空）
kubectl get endpoints -n inference vllm-router-redis

# 3) Router 指标（端口名按你的 Service 调整）
kubectl port-forward -n inference svc/vllm-router-service 8000:80
curl -s localhost:8000/metrics | grep -E 'cache_aware_(first_visit|returning)'
curl -s localhost:8000/metrics | grep returning_session_store_errors   # 应为 0 / 无增量

# 4) Redis 里的回访 key 数（≈ TTL 内活跃 unique session 数）
kubectl run -it --rm redis-cli -n inference --image=redis:7-alpine -- \
  redis-cli -h vllm-router-redis dbsize
```

> Router 镜像**不含** `redis-cli` 二进制；查 Redis 用单独的 `redis:7-alpine` 容器或 `kubectl exec` 进 Redis Pod。
> 多副本务必让所有 Router 填**同一个** redis-url；只有 `replicaCount: 1` 时才可省去 Redis 用默认 `memory`。

## 11. 选型

| 场景 | 建议 |
| --- | --- |
| 单 Router (`replicaCount: 1`) | `memory` 即可；内存大可调短 TTL |
| 多 Router 且需准确回访指标 | `redis`，全体副本连同一个 URL |
| 不想运维 Redis | 单副本，或接受多副本回访指标近似偏低 |

## 12. 测试

- 单测：`src/tests/test_returning_session_store.py`（memory / fakeredis / fail-open / factory）、`src/tests/test_cache_aware_router.py`（漏斗分区、比率、TTL 过期、无 session 不计入）。
- 运行：`PYTHONPATH=src pytest src/tests/test_cache_aware_router.py src/tests/test_returning_session_store.py -q`
