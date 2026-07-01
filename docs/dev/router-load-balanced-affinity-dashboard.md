# load_balanced_affinity 路由控制面板 — 绘制规格

交给「能抓取线上信息」的 AI 落地用。所有指标名均与 router 实际 export 一致
（见 `src/vllm_router/services/metrics_service/__init__.py`）。两种数据通道都给了：

- **Prometheus 通道**：已有 Prometheus 抓 router `/metrics`，直接用下面的 PromQL。
- **直抓通道**：没有 Prometheus 时，周期性 GET `http(s)://<router-host>/metrics`
  （Prometheus 文本格式），按"原始解释"列自行算速率/比例。

## 0. 全局约定（务必先读）

- **`server` 标签 = 单个 vLLM 引擎**（如 `vllm-0`…`vllm-7`）。带 `server` 的指标
  做"跨引擎均衡"分析时不要 `sum()` 掉，要按 `server` 展开。
- **Gauge 是窗口量、多 worker 下取 `mostrecent`**（最近一次写入值），不要对 Gauge 用
  `rate()`。需要集群口径速率/比例时，**一律用 `*_total` 计数器 + `rate()`**（计数器在多
  worker 下自动求和）。
- 计数器无 `server` 标签的是**全局**（hit/shed/placement）；带标签的是**按引擎**
  （inflight）。
- 直抓时：计数器自己做"两次抓取差 / 时间差"得速率；hit_rate 这类 Gauge 直接读当前值。

### ⚠ 多 worker 聚合（必读，否则数值偏小）

每个 router 副本跑 N 个 worker 进程。下面这些 Gauge 是**各 worker 自算**的，多 worker 下
`mostrecent` 只取**某一个 worker**的值 → 对它们 `sum()` 会得到约 `1/N` 的偏小值（2 worker
≈ 真实的一半）：

> `current_qps`、`num_requests_running`、`num_prefill_requests`、`num_decoding_requests`、
> `num_requests_swapped`、`load_balanced_inflight_requests`、`avg_latency`、`avg_itl`。

对策：
- **集群 QPS 不要用 `sum(current_qps)`**，改用计数器（跨 worker 自动求和）——见 1 节。
- **按引擎**的 QPS/running/inflight 没有对应计数器，面板只反映"单 worker 切片"，用于看
  **相对均衡（CV）尚可，绝对值偏小**——已在 3 节标注。
- 根治需改代码：把这些"可加"Gauge 的 `multiprocess_mode` 由 `mostrecent` 换成 `livesum`
  （跨 worker 求和），均值/比率类（`avg_latency`/`avg_itl`/`hit_rate`）保持原样。

不受影响（引擎/服务发现来，各 worker 一致）：`gpu_prefix_cache_*`、`healthy_pods_total`。
本来就正确（Counter 自动跨 worker 求和）：`load_balanced_affinity_hit/shed/placement_total`。

---

## 1. 顶部概览（Stat 行，一眼健康）

| 面板 | 类型 | PromQL | 直抓解释 | 阈值/读法 |
|---|---|---|---|---|
| 健康引擎数 | Stat | `max(vllm:healthy_pods_total)` | 直接读 | 等于期望副本数为绿；少 1 黄；少 2+ 红 |
| 集群 QPS | Stat | `sum(rate(vllm:load_balanced_affinity_hit_total[1m])) + sum(rate(vllm:load_balanced_affinity_shed_total[1m])) + sum(rate(vllm:load_balanced_placement_total[1m]))` | 三个计数器速率相加 | **用计数器,跨 worker 正确**；切勿用 `sum(current_qps)`（偏小 1/N） |
| 亲和命中率(窗口) | Gauge/Stat | 见 2.2（用计数器 rate 重算） | — | ≥0.6 绿 / 0.3–0.6 黄 / <0.3 红 |
| 在途请求总数 | Stat | `sum(vllm:load_balanced_inflight_requests)` | 各 server 求和 | per-worker,偏小 1/N（仅看趋势/突增）；根治见 0 节 livesum |
| 等待队列总数 | Stat | `sum(vllm:num_requests_waiting)`（**引擎原生**,需 Prometheus 直抓各 vLLM 引擎） | 直接读 | router **未导出**此指标（定义了但从未 set）；此面板数据来自引擎自身的 `/metrics`,标签按引擎实例,不是 `server`。持续 >0 = 引擎吃不下 |

---

## 2. 亲和健康（这是新路由的核心，重点看）

**2.1 请求去向分解（hit / shed / placement，按速率,堆叠面积图）**
> hit+shed+placement 划分每个请求：返回会话被留在记忆引擎=hit；被挪到更轻引擎=shed；
> 首访/无 session/亲和关=新的 P2C placement。

```promql
sum(rate(vllm:load_balanced_affinity_hit_total[5m]))    # hit
sum(rate(vllm:load_balanced_affinity_shed_total[5m]))   # shed
sum(rate(vllm:load_balanced_placement_total[5m]))       # placement
```
直抓：三个计数器各做 (Δcount/Δt)，堆叠。**读法**：shed 持续高于 hit = 集群负载不均/记忆
引擎常更忙，亲和被频繁牺牲（设计行为，护尾延迟）；但若 shed≈100% 说明亲和几乎没生效，
需查 worker 稀释或 slack。

**2.2 返回会话命中率（时间序列，0–1）**
```promql
sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
/
clamp_min(
  sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
  + sum(rate(vllm:load_balanced_affinity_shed_total[5m])),
  1)
```
> 与 `vllm:load_balanced_affinity_hit_rate` Gauge 同义，但这是按 `rate` 重算的集群真值，
> 不受多 worker mostrecent 影响，更准。**目标线 0.5**，可画阈值带。

**2.3 placement 占比（首访/无亲和流量比例）**
```promql
sum(rate(vllm:load_balanced_placement_total[5m]))
/ clamp_min(
    sum(rate(vllm:load_balanced_affinity_hit_total[5m]))
  + sum(rate(vllm:load_balanced_affinity_shed_total[5m]))
  + sum(rate(vllm:load_balanced_placement_total[5m])), 1)
```
读法：偏高=多为一次性/首轮请求,亲和本就无从谈起；偏低=多轮返回流量为主,这时 2.2 才有意义。

---

## 3. 负载 / QPS 均衡（你最关心的"轮询才有的长尾"对照）

> ⚠ 本节的 `current_qps` / `num_requests_running` / `inflight` 均为 **per-worker** Gauge（见 0 节）：
> 多 worker 下每条线只反映**一个 worker** 的切片，绝对值偏小 ~1/N。**看相对均衡（CV/线形发散）有效，
> 绝对值别当真**；要绝对正确需 0 节的 `livesum` 代码改动。

**3.1 每引擎 QPS（时间序列，按 server 展开）**
```promql
vllm:current_qps
```
读法：各线越贴合越均衡（相对形态可信，绝对值偏小）。

**3.2 QPS 不均衡度（变异系数 CV，越低越均衡）**
```promql
stddev(vllm:current_qps) / clamp_min(avg(vllm:current_qps), 0.001)
```
另可加峰均比 `max(vllm:current_qps) / clamp_min(avg(vllm:current_qps),0.001)`（1.0=完美）。
**读法**：CV 是比值，per-worker 偏小**不影响**它（分子分母同缩放）→ **这个面板可信**。
CV 持续 <0.2 健康；>0.5 说明出现热点引擎——这正是要压制的长尾来源。

**3.3 每引擎在跑/在途（时间序列，按 server）**
```promql
vllm:num_requests_running              # router 视角在跑(per-worker,偏小)
vllm:load_balanced_inflight_requests   # router 视角在途(per-worker,偏小)
```
读法：某引擎单独飙高=热点；inflight 与 running 背离=统计延迟或死引擎守卫触发。引擎自报排队
请用引擎原生 `vllm:num_requests_waiting`（需 Prometheus 直抓引擎,见 1 节）。

**3.4 换出请求（swap，溢出预警）**
```promql
vllm:num_requests_swapped
```
任意 server 持续 >0 = 显存吃紧，需降并发或扩容。

---

## 4. Router 容量 / 何时加副本

Router 是**单事件循环代理**:每 worker 一个 asyncio loop,负责路由决策 + 转发/回传字节流。
"扛不住" = loop 处理不过来(CPU 打满 / 同步 redis 阻塞 / 并发协程过多),表现为**转发延迟
飙升 + /health 排队被 K8s 杀**。加 router 副本**只在瓶颈是 router 自身**时有用;引擎满时要加
引擎。本节信号按预警先后排列。

**4.1 事件循环延迟 event-loop lag（已实现,最灵敏的先行信号）**
- 指标:`router_event_loop_lag_seconds`(gauge;后台任务量 loop 调度漂移,`livemax` 跨 worker 取最差)。注意 router 自身指标**无 `vllm:` 前缀**(同 `router_cpu_usage_percent`)。
- PromQL:`max(router_event_loop_lag_seconds)`
- 阈值:**>50ms warn / >100ms critical**。到 100ms 量级时 `/health` 也开始排队 → pod 被杀
  → 级联崩溃(你踩过的坑)。这是**唯一能同时捕捉 CPU、redis 阻塞、协程过载**三种原因的单一
  数字,最适合做 HPA 触发和扩容告警。

**4.2 Router CPU%(已有）**
```promql
max(router_cpu_usage_percent)
```
> 是 `psutil.cpu_percent()` **系统级**读数(非单进程),且 scrape 时阻塞 0.1s。**>75% warn /
> >85% critical**。多 worker 同读系统值,`max`/`avg` 均可。

**4.3 在途并发（已实现)**
- `router_active_requests`(请求进入 +1、结束 −1,`livesum` 跨 worker 求和;无 `vllm:` 前缀),
  直接反映 loop 正在同时代理多少请求。持续贴近经验上限=接近饱和。
- 旧近似 `sum(vllm:load_balanced_inflight_requests)` per-worker 偏小(见 0 节),只看趋势即可。

**4.4 健康探针延迟(K8s/blackbox,非 router 指标)**
- 用探针 latency;**>probe timeout × 0.5 warn**。它是"被杀"前最后的可观测信号,4.1 是它的先行量。

**4.5 判别 router-bound vs engine-bound（决定加谁,必看)**
并排对比引擎侧饱和度:
| 引擎侧(waiting/GPU) | Router 侧(lag/CPU) | 结论 |
|---|---|---|
| 不高(有余量) | 高 | **加 router 副本** |
| 高(waiting 堆积/GPU 打满) | 高或不高 | **加引擎**,加 router 无用 |
| 不高 | 不高 | 都不用加 |

引擎侧看:`vllm:num_requests_waiting`(引擎原生,见 1 节)持续 >0 堆积、GPU 利用率打满。

**4.6 扩容经验公式**
压测得单副本(N worker)可持续的 `QPS_sat`(以 4.1 lag 不越 50ms 为界),则
`需要副本数 ≈ ceil(总QPS / QPS_sat) × 1.3(安全系数)`。HPA 建议挂 4.1(event-loop lag)或
4.2(CPU),不要只挂内存。

> 4.1 `router_event_loop_lag_seconds`(livemax)与 4.3 `router_active_requests`(livesum)均**已实现**,随 v1.3 镜像发布。

---

## 5. 故障/可靠性

| 面板 | PromQL | 读法 |
|---|---|---|
| 亲和存储错误(Redis) | `rate(vllm:load_balanced_affinity_store_errors_total[5m])` (按 `operation` 分 get/put) | >0 = 共享亲和(Redis)降级 fail-open:路由不受影响,但会退化为纯负载均衡(亲和暂失效) |
| 死引擎守卫(间接) | `vllm:load_balanced_inflight_requests` 某 server 归零但 `num_requests_running` 仍高 | 该引擎被守卫排除,核对 `healthy_pods_total` |

---

## 6. 建议布局（Grafana 行分组）

1. **Overview**（第 1 节 5 个 Stat）
2. **Affinity**（2.1 堆叠 + 2.2 命中率 + 2.3 placement 占比）
3. **Load Balance**（3.1 每引擎 QPS + 3.2 CV + 3.3 running/waiting/inflight + 3.4 swap）
4. **Router Capacity / Scale-out**（4.1 event-loop lag + 4.2 CPU + 4.3 并发 + 4.5 判别表）
5. **Reliability**（第 5 节）

数据源 uid 用现有 `prometheus`（见 `observability/vllm-dashboard.json`），可直接在该 JSON 里
追加上述 panel。所有 `rate()` 窗口默认 `5m`,QPS 抖动大时可调 `2m`。
