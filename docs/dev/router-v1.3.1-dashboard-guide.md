# Router v1.3.1（load_balanced_affinity）控制面板编写指南

面向对象：一个「能查询线上 Prometheus / 直抓 router `/metrics`」的 AI，用它照此落地
Grafana 面板。本指南只描述 **router v1.3.1** 暴露的指标、**多副本聚合规则**、以及每个
面板的**真实含义与读法**。所有指标名与代码一致
（`src/vllm_router/services/metrics_service/__init__.py`）。

本次部署形态（务必据此选择聚合方式）：

- 路由策略：`load_balanced_affinity`（P2C 负载放置 + 自削弱亲和 + 死引擎守卫）
- `--router-workers 1`：**每个 router pod 只有 1 个进程**
- `--lb-affinity-store redis`：会话→引擎映射存共享 redis
- `--lb-affinity-ttl 300`：亲和记忆 5 分钟
- 多个 router **副本（pod）** 同时对外服务，前面挂 Service/Ingress

**明确不做的面板**（这些另有面板覆盖，本指南一律不含）：端到端延迟 / TTFT / ITL、
GPU 前缀缓存命中率、客户端真实体验类。本指南聚焦四块：**亲和健康、负载均衡质量、
Router 容量/扩容信号、Redis/可靠性**。

---

## 0. 数据模型与聚合规则（最重要，先读懂再写 PromQL）

### 0.1 抓取拓扑

Prometheus 每个 **router pod** 抓一份 `/metrics`。因为 `--router-workers 1`，**pod 内没有
多进程聚合问题**（旧文档里"多 worker mostrecent 取 1/N"的坑在本配置下不存在）。所以每个
pod 导出的每条 series 都是**该副本自身的一份干净值**。你唯一要处理的是**跨副本（跨 pod）
聚合**。Prometheus 会给每条 series 自动带上 pod/instance 之类的 target 标签，用它来分组。

### 0.2 两类指标——决定跨副本该 `sum` 还是 `avg/max`（关键）

router 的指标分两种来源，聚合方式**相反**，搞错会导致数值翻 N 倍或偏小：

**A. Router 自身口径（本副本处理的那一份流量）→ 跨副本要 `sum`**

这些值来自 router 自己的 request-stats（`metrics_router.py` 用
`get_request_stats().qps / in_prefill / in_decoding …` set）。每个请求只被**一个**副本处理，
所以把各副本相加 = 全集群真值：

- `vllm:current_qps{server}`（本副本发往该引擎的 QPS）
- `vllm:num_requests_running{server}`（本副本视角，发给该引擎、在 prefill+decode 中的请求数）
- `vllm:num_prefill_requests{server}` / `vllm:num_decoding_requests{server}`
- `vllm:num_requests_swapped{server}`
- `vllm:load_balanced_inflight_requests{server}`（本副本已派发未完成的在途数）
- `router_active_requests`（本副本正在代理的请求数）
- 所有 `*_total` 计数器（hit / shed / placement / store_errors）——用 `sum(rate(...))`

> 记忆点：带引擎 `server` 标签、且是"数量/速率"的，基本都是 router 口径 → **`sum by (server)`**。

**B. 引擎绝对量 / 全局量（每个副本看到的是同一份）→ 跨副本要 `max` 或 `avg`，绝不能 `sum`**

这些值每个副本观测到的是**同一个客观事实**，相加会把它乘以副本数：

- `vllm:healthy_pods_total`（服务发现看到的健康引擎数，各副本一致）→ `max()`
- `router_event_loop_lag_seconds`（每个副本自己的 loop 延迟，不同 pod 不同）→ 跨副本看
  **最差的那个**用 `max()`，别 `sum`（见 §3.1）

**C. 比率类 → 用计数器重算，不要对 Gauge 跨副本平均**

- `vllm:load_balanced_affinity_hit_rate` 是各副本自算的窗口比率。跨副本别简单平均（各副本
  流量不等会失真），**改用计数器 `sum(rate(hit)) / sum(rate(hit+shed))` 重算集群真值**（见 §1.2）。

### 0.3 一个坑：router 不导出引擎排队数

`vllm:num_requests_waiting` 这个 gauge 在 router 里**定义了但从不 set**（引擎侧才有）。要看
"引擎自己排了多少队"，必须让 Prometheus **直接抓各 vLLM 引擎的 `/metrics`**，用引擎那边的
`vllm:num_requests_waiting`（标签是引擎实例，不是 router 的 `server`）。判断 engine-bound 时要用它。

### 0.4 ttl=300 对亲和面板读数的影响

亲和记忆只有 5 分钟：间隔 >5 分钟才回来的会话会被当**首访**（计入 placement，而非 hit）。
所以 §1 的命中率反映的是"5 分钟内返回会话"的黏着质量；对话停顿偏长的业务，命中率天然偏低
属正常，不代表路由有问题。

### 0.5 直抓通道（无 Prometheus 时）

周期性 `GET http(s)://<router-pod>/metrics`（Prometheus 文本格式），**每个 pod 抓一份**，
按 0.2 的 A/B 规则在客户端自己做跨 pod 的 sum / max；计数器自己用 (Δcount/Δt) 算速率。

---

## 1. 亲和健康（新路由核心，重点看）

hit / shed / placement 三者**划分每一个请求**：返回会话被留在记忆引擎 = hit；被挪到更轻的
引擎 = shed（为均衡牺牲亲和，是设计行为）；首访 / 无 session / 亲和关 = 全新 P2C placement。

### 1.1 请求去向分解（堆叠面积图，按速率）

```promql
# hit：返回会话保住了亲和（命中共享记忆，前缀缓存大概率复用）
sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
# shed：返回会话被主动挪走（记忆引擎太忙，护尾延迟，牺牲了亲和）
sum(rate(vllm:load_balanced_affinity_shed_total[5m]))
# placement：首访/无会话，按 P2C 就近放到较轻引擎
sum(rate(vllm:load_balanced_placement_total[5m]))
```

读法：三条速率相加 = 集群总请求速率（也是 §4 的集群 QPS 口径）。`shed` 长期高于 `hit`
说明负载偏斜、记忆引擎经常更忙、亲和被频繁牺牲——这本身是护尾延迟的正常代价；但若
`shed` 逼近 100%、`hit` 几乎为 0，说明亲和基本没生效，要查 redis 是否在降级（§5）或
`--lb-affinity-slack` 是否过小。

### 1.2 返回会话命中率（时序，0–1；集群真值）

```promql
# 用计数器重算，跨副本正确；等价于 hit_rate gauge 但不受各副本口径差异影响
sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
/
clamp_min(
  sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
  + sum(rate(vllm:load_balanced_affinity_shed_total[5m])),
  1)
```

含义：在"有记忆的返回会话"里，有多大比例保住了亲和。目标线画 0.5：≥0.6 绿 / 0.3–0.6 黄 /
<0.3 红。**注意结合 ttl=300**（§0.4）：这是 5 分钟窗口内的返回会话命中率。

### 1.3 placement 占比（首访/无亲和流量比例）

```promql
sum(rate(vllm:load_balanced_placement_total[5m]))
/ clamp_min(
    sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
  + sum(rate(vllm:load_balanced_affinity_shed_total[5m]))
  + sum(rate(vllm:load_balanced_placement_total[5m])), 1)
```

含义：偏高 = 流量以一次性/首轮为主，亲和本就无从谈起（此时 1.2 的命中率意义不大）；偏低 =
多轮返回流量为主，1.2 才是关键指标。用它给命中率"定语境"。

---

## 2. 负载均衡质量（对照"轮询才有的长尾"）

本节所有 `server` 量都是 router 口径（§0.2-A）→ **跨副本 `sum by (server)`** 得到集群真值。
`--router-workers 1` 下没有 1/N 偏小问题，数值可当真。

### 2.1 每引擎 QPS（时序，按 server 展开）

```promql
sum by (server) (vllm:current_qps)
```

读法：各引擎线越贴合越均衡。某条线持续高出一截 = 热点引擎，正是要压制的长尾来源。

### 2.2 QPS 不均衡度（变异系数 CV，越低越均衡）

```promql
stddev(sum by (server) (vllm:current_qps))
/ clamp_min(avg(sum by (server) (vllm:current_qps)), 0.001)
```

含义：CV = 跨引擎 QPS 的离散程度。**这是判断均衡性的首选单值指标**。<0.2 健康；>0.5 说明
出现热点引擎。可另加峰均比 `max/avg`（=1.0 完美均衡）。CV 是比值，任何整体缩放都不影响它。

### 2.3 每引擎在跑 / 在途（时序，按 server）

```promql
# router 视角：发给该引擎、在 prefill+decode 中的请求数（跨副本求和）
sum by (server) (vllm:num_requests_running)
# router 视角：已派发未完成的在途请求（跨副本求和）
sum by (server) (vllm:load_balanced_inflight_requests)
```

读法：某引擎单独飙高 = 热点。`inflight` 与 `running` 明显背离，或某 server 的 `inflight`
突然归零而其它仍高 = 该引擎可能被**死引擎守卫**排除（核对 §1.1 是否全转 placement、
`healthy_pods_total` 是否掉数）。

### 2.4 引擎排队（溢出预警，数据来自引擎自身）

```promql
# 注意：这条来自 vLLM 引擎自己的 /metrics，不是 router 的 server 标签（见 §0.3）
# 标签是引擎实例；按你的引擎 job 名替换
sum by (pod) (vllm:num_requests_waiting)
```

读法：任一引擎持续 >0 且增长 = 引擎吃不下（engine-bound），这时要加**引擎**而不是 router。

### 2.5 换出请求（swap，显存溢出预警）

```promql
sum by (server) (vllm:num_requests_swapped)
```

任一引擎持续 >0 = 显存吃紧，需降并发或扩容引擎。

---

## 3. Router 容量 / 何时加副本

Router 每个进程是**单 asyncio 事件循环**：负责路由决策 + 转发/回传字节流。「router 扛不住」=
loop 处理不过来（CPU 打满 / 同步阻塞 / 并发协程过多），表现为转发延迟飙升、`/health` 排队
被 K8s 杀 → 级联崩溃。加 router 副本**只在瓶颈是 router 自身时**有用；引擎满时要加引擎。

### 3.1 事件循环延迟（最灵敏的先行信号，已实现）

```promql
# 每个副本自己的 loop 延迟；跨副本看最差的一个，绝不 sum（见 §0.2-B）
max(router_event_loop_lag_seconds)
# 想定位是哪个副本，按 pod 展开：
max by (pod) (router_event_loop_lag_seconds)
```

含义：后台任务测量的 loop 调度漂移。它是**唯一能同时捕捉 CPU、同步 redis 阻塞、协程过载**
三种饱和原因的单一数字。阈值：**>50ms warn / >100ms critical**——到 100ms 量级 `/health`
开始排队 → pod 被杀。**最适合做 HPA 触发和扩容告警**。注意 router 自身指标**无 `vllm:` 前缀**。

> v1.3.1 熔断修复后，即使 redis 被黑洞，该 lag 也不再飙升（redis 调用在冷却期被跳过）；
> 若 lag 高但 redis 正常，则是真的 CPU/并发压力，需要加副本。

### 3.2 在途并发（已实现）

```promql
# livesum 已在 pod 内聚合（本配置单 worker 即单值）；跨副本求和 = 全集群在代理的请求数
sum(router_active_requests)
# 按副本看分布，判断负载是否在副本间均匀：
sum by (pod) (router_active_requests)
```

含义：loop 当前同时代理多少请求。持续贴近经验上限 = 接近饱和。副本间差异大 = 前面的
Service/Ingress 分发不均。

### 3.3 Router CPU%（已有）

```promql
max(router_cpu_usage_percent)
```

含义：`psutil` 系统级读数。**>75% warn / >85% critical**。无 `vllm:` 前缀。

### 3.4 判别 router-bound vs engine-bound（决定加谁，必看）

并排看引擎侧（§2.4 waiting、GPU 利用率）与 router 侧（§3.1 lag、§3.3 CPU）：

| 引擎侧（waiting/GPU） | Router 侧（lag/CPU） | 结论 |
|---|---|---|
| 有余量（waiting≈0） | 高 | **加 router 副本** |
| 饱和（waiting 堆积/GPU 打满） | 高或不高 | **加引擎**，加 router 无用 |
| 有余量 | 不高 | 都不用加 |

### 3.5 扩容经验公式

压测得单副本可持续的 `QPS_sat`（以 §3.1 lag 不越 50ms 为界），则
`需要副本数 ≈ ceil(总QPS / QPS_sat) × 1.3`。HPA 建议挂 §3.1（event-loop lag）或 §3.3（CPU），
不要只挂内存。

---

## 4. Redis / 可靠性

### 4.1 亲和存储错误率（Redis 健康 + 熔断可视化）

```promql
# 按 operation(get/put) 分开看
sum by (operation) (rate(vllm:load_balanced_affinity_store_errors_total[5m]))
```

含义与读法（结合 v1.3.1 熔断行为）：

- **持平在 0** = redis 健康，共享亲和正常。
- **短暂尖峰后回落** = redis 抖动，熔断触发后跳过 redis，路由自动 fail-open 到纯 P2C，
  会话亲和暂失效但**路由不阻塞、不报错给客户端**；redis 恢复后自动复原。
- **关键判据**：redis 故障期间此曲线应**接近平台/常数级**（每冷却期只有几次探测失败），
  **不是每请求 +1 的线性暴涨**。若看到线性暴涨，说明熔断没生效，需排查镜像版本。
- 该曲线升高时，§1.2 命中率会同步下探、§1.1 全转 placement——三者可交叉印证。

### 4.2 亲和降级联动（辅助判读）

把 §4.1 错误率与 §1.2 命中率叠加在同一时间轴：错误率↑ 且 命中率↓ 且 §1.1 placement 占比↑
= 典型的 redis 降级期。三线同步恢复 = 熔断自愈成功。

---

## 5. 建议布局（Grafana 行分组）

1. **Overview**：健康引擎数 `max(vllm:healthy_pods_total)`、集群 QPS（§1.1 三速率之和）、
   命中率（§1.2）、在途总数（§3.2）、event-loop lag（§3.1）——5 个 Stat。
2. **Affinity**：§1.1 堆叠 + §1.2 命中率 + §1.3 placement 占比。
3. **Load Balance**：§2.1 每引擎 QPS + §2.2 CV + §2.3 running/inflight + §2.4 引擎排队 + §2.5 swap。
4. **Router Capacity**：§3.1 lag + §3.2 并发 + §3.3 CPU + §3.4 判别表（用 Text panel 放判别逻辑）。
5. **Reliability**：§4.1 store errors + §4.2 降级联动。

模板变量：`namespace`（default）、`pod`（router pod 正则，用于按副本下钻）。数据源用现有
`prometheus`。所有 `rate()` 窗口默认 `5m`，QPS 抖动大时可临时调 `2m`。

---

## 附：跨副本聚合速查表

| 指标 | 类型 | 跨副本聚合 | 备注 |
|---|---|---|---|
| `vllm:current_qps{server}` | router 口径 | `sum by (server)` | 每引擎/集群 QPS |
| `vllm:num_requests_running{server}` | router 口径 | `sum by (server)` | router 视角在跑 |
| `vllm:load_balanced_inflight_requests{server}` | router 口径 | `sum by (server)` | 在途 |
| `vllm:num_requests_swapped{server}` | router 口径 | `sum by (server)` | swap 预警 |
| `vllm:load_balanced_affinity_hit/shed/placement_total` | 计数器 | `sum(rate(...))` | 集群速率正确 |
| `vllm:load_balanced_affinity_store_errors_total{operation}` | 计数器 | `sum by (operation)(rate(...))` | redis 健康 |
| `vllm:load_balanced_affinity_hit_rate` | 比率 gauge | **别跨副本平均**，用计数器重算 | §1.2 |
| `router_active_requests` | router 口径 | `sum` | 无 `vllm:` 前缀 |
| `router_event_loop_lag_seconds` | 每副本 | `max` | 无 `vllm:` 前缀，取最差 |
| `router_cpu_usage_percent` | 每副本 | `max` | 无 `vllm:` 前缀 |
| `vllm:healthy_pods_total` | 全局一致 | `max` | 各副本相同，别 sum |
| `vllm:num_requests_waiting` | 引擎侧 | 抓引擎 `/metrics` | router **不导出**，见 §0.3 |
