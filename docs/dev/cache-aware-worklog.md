# Cache-Aware 超限路由 设计与实现说明

## 概述

本文档说明 vLLM Router 中新增的缓存感知（cache-aware）超限路由模式的设计、实现位置、决策流程、配置项、监控指标及已知约束，供开发与运维人员阅读代码和部署时参考。

该模式通过 `--routing-logic cache_aware_load_balancing` 启用。其目标是在保证会话（session）粘滞以提升 KV cache 命中率的前提下，当目标引擎过载时将请求转发至其他引擎，避免单点过载。

配置示例：

```
--routing-logic cache_aware_load_balancing
--session-key x-session-id
--cache-aware-tolerate-waiting-requests 20
```

实现位于 `vllm_router` 包内，不影响其他路由模式与请求代理路径。

---

## 1. 设计目标

- 同一会话默认固定路由到同一引擎，以提升 KV cache 命中率。
- 当目标引擎过载（排队或延迟超过阈值）时，将该次请求转发至满足约束的其他引擎。
- 提供窗口化的 Prometheus 指标用于观测粘滞率与转发率。
- 对请求热路径的性能影响接近于零。

---

## 2. 代码结构

| 文件 | 职责 |
|---|---|
| `src/vllm_router/routers/routing_logic.py` | 核心实现：`CacheAwareLoadBalancingRouter` |
| `src/vllm_router/stats/request_stats.py` | 延迟分位数计算（`get_percentile`、`get_overload_snapshot`）及每请求记账 |
| `src/vllm_router/services/metrics_service/__init__.py` | Prometheus 指标（Gauge）定义 |
| `src/vllm_router/routers/metrics_router.py` | `/metrics` 接口；抓取时刷新窗口指标 |
| `src/vllm_router/services/request_service/request.py` | 请求代理；请求结束时通知路由器释放在途计数 |
| `src/vllm_router/parsers/parser.py` | `--cache-aware-*` 命令行参数定义 |
| `src/vllm_router/app.py` | 参数装配，初始化路由器 |

`CacheAwareLoadBalancingRouter` 的主要方法：

- `route_request(...)`：路由入口，转调 `_route_with_snapshot(...)`。
- `_violated_reasons(url, ...)`：判定指定引擎是否过载，返回被触发的阈值名称列表（空列表表示未过载）。
- `_select_fallback(...)`：目标引擎过载时选择替代引擎。
- `_fallback_sort_key(url, ...)`：计算引擎的有效负载，用于候选排序。
- `_record_dispatch` / `release_inflight` / `_pending_load`：在途请求计数（见第 4.2 节）。
- `_record` / `_evict` / `_publish_gauges` / `refresh_window_metrics`：窗口指标维护（见第 4.3 节）。

---

## 3. 路由决策流程

请求处理入口为 `_route_with_snapshot`，流程如下：

1. 请求未携带 session：选择有效负载最小的引擎并返回（线上请求均携带 session，此分支为防御性处理）。
2. 请求携带 session：通过一致性哈希计算其归属引擎 `initial`。
3. 判定 `initial` 是否过载（`_violated_reasons`），命中以下任一条件即视为过载：
   - 排队请求数 ≥ `--cache-aware-tolerate-waiting-requests`；
   - p50 / p99 TTFT ≥ 对应阈值；
   - p50 / p99 端到端延迟 ≥ 对应阈值；
   - 阈值为 0 表示该项关闭，不参与判定。
4. 未过载：路由至 `initial`（粘滞），记录一次 sticky 决策。
5. 过载：调用 `_select_fallback` 选择替代引擎，记录一次 fallback 决策：
   1. 候选集合 = 满足全部阈值（未过载）的引擎；
   2. 在候选中按有效负载升序选择（有效负载定义见第 4.1 节）；
   3. 若多个候选负载并列（差值 ≤ `--cache-aware-tie-tolerance`），按 p50 端到端延迟升序选择；
   4. 若仍并列（例如均无延迟数据），在并列候选中随机选择；
   5. 候选集合为空（所有引擎均过载）：退回 `initial`，不进行转发。

---

## 4. 核心机制

### 4.1 有效负载

`_fallback_sort_key` 计算每个引擎的有效负载，作为候选排序的主键：

```
有效负载 = scraped_waiting + scraped_running + inflight
```

- `scraped_waiting` / `scraped_running`：从引擎 `/metrics` 周期抓取的排队数与运行数；
- `inflight`：本路由器已派发但尚未结束的请求数（见 4.2）；
- 三者之和用于估计引擎当前负载，数值越小优先级越高。

### 4.2 在途计数（inflight）

在途计数记录本路由器进程已派发但尚未收到结束的请求数，按引擎分别统计。

- 实现：派发时 `_record_dispatch(url)` 入队，请求结束时 `release_inflight(url)` 出队，`_pending_load(url)` 读取当前值。
- 设计原因：引擎 `/metrics` 为周期抓取，存在滞后。在并发请求集中到达时，若仅依据抓取到的旧值判断，多个请求会同时观察到引擎空闲并被路由至同一引擎。在途计数在派发后立即生效，使后续请求能够感知本路由器刚发出的负载，从而分散路由。该机制解决单副本内并发请求集中转发的问题。
- 安全上限：`--cache-aware-inflight-decay`（默认 300 秒）。正常请求结束即递减；若请求未正常结束（客户端断开、后端无响应），超过该时限的计数将被丢弃，避免计数无法归零。

### 4.3 窗口指标

路由器维护一个 `--cache-aware-stats-window`（默认 30 秒）时间窗口内的决策事件队列，每条记录包含时间戳、是否 fallback、触发原因。

- `_record` 追加事件并更新计数；`_evict` 清除过期事件；`_publish_gauges` 将比率写入 Gauge。
- `refresh_window_metrics` 在 `/metrics` 被抓取时调用，确保在无流量期间比率仍随时间衰减，而非停留在最后一次记录的值。

---

## 5. 配置项

参数定义见 `parser.py`。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--routing-logic cache_aware_load_balancing` | — | 启用本模式 |
| `--session-key` | — | 提取 session id 的请求头名称（本模式必填） |
| `--cache-aware-tolerate-waiting-requests` | 20 | 排队阈值：粘滞引擎排队数达到此值即触发 fallback |
| `--cache-aware-p50-ttft-threshold` | 0（关闭） | p50 TTFT 阈值（秒） |
| `--cache-aware-p99-ttft-threshold` | 0（关闭） | p99 TTFT 阈值（秒） |
| `--cache-aware-p50-e2e-threshold` | 0（关闭） | p50 端到端延迟阈值（秒） |
| `--cache-aware-p99-e2e-threshold` | 0（关闭） | p99 端到端延迟阈值（秒） |
| `--cache-aware-stats-window` | 30 | 指标统计窗口（秒） |
| `--cache-aware-inflight-decay` | 300 | 在途计数安全上限（秒），用于回收未正常结束的请求 |
| `--cache-aware-tie-tolerance` | 0 | 负载差值不超过此值的候选视为并列并随机选择；0 表示仅对完全相等者随机 |
| `--engine-stats-interval`（上游既有） | 30 | 引擎 `/metrics` 抓取间隔；多副本部署建议设为 3–5 |

---

## 6. 监控指标

通过 `/metrics` 暴露：

- `vllm:cache_aware_stickiness_rate`：窗口内"粘滞引擎未过载"的请求占比。
- `vllm:cache_aware_fallback_rate`：窗口内"粘滞引擎过载（触发 fallback）"的请求占比。注意：fallback 表示**触发了转发**；若所有引擎都过载，请求仍留在原引擎，但仍计入本指标。
- `vllm:cache_aware_fallback_reason_rate{reason}`：各阈值触发 fallback 的占比，`reason ∈ {queue, p50_ttft, p99_ttft, p50_e2e, p99_e2e}`。
- `vllm:cache_aware_inflight_requests{server}`：本路由器对各引擎的在途请求数。

累积计数器（自路由器启动起单调递增，配合 Prometheus `rate()` 可计算任意窗口的速率，不随 `stats-window` 衰减）：

- `vllm:cache_aware_sticky_total`：粘滞决策累计次数。
- `vllm:cache_aware_fallback_total`：fallback 累计次数。
- `vllm:cache_aware_fallback_reason_total{reason}`：各原因触发 fallback 的累计次数。

第 6 节中以 `_rate` 结尾的 Gauge 为窗口内即时比率，便于直接观察；上述以 `_total` 结尾的 Counter 为累积量，便于在 Grafana 中自定义统计窗口。

### 6.1 日志

路由决策在 `CacheAwareLoadBalancingRouter._route_with_snapshot` 中输出日志：

- 发生 fallback（转发至其他引擎）：`INFO` 级，格式为

  ```
  cache_aware fallback: session=<id> from=<原引擎> to=<目标引擎> reasons=[...]
    from_load=[queue=.. running=.. inflight=.. p50_ttft=.. p99_ttft=.. p50_e2e=.. p99_e2e=..]
    to_load=[...]
  ```

  其中 `reasons` 为被触发的阈值名称列表（`queue` / `p50_ttft` / `p99_ttft` / `p50_e2e` / `p99_e2e`，可多项同时触发），`from_load` / `to_load` 为两台引擎在决策时刻的实际负载与延迟分位数，便于定位转发原因与目标选择依据。
- 所有引擎均过载、退回原引擎：`WARNING` 级，包含触发原因与原引擎负载。
- 正常粘滞（未过载）：`DEBUG` 级，默认不输出，避免每请求刷屏；排障时可调高日志级别查看。

---

## 7. 使用说明

### 7.1 启用（单副本）

```
vllm-router \
  --routing-logic cache_aware_load_balancing \
  --session-key x-session-id \
  --cache-aware-tolerate-waiting-requests 20
```

`--session-key` 为必填项；其值应为客户端用于标识同一会话的请求头名称。仅设置排队阈值即可工作，延迟阈值默认关闭。

### 7.2 启用延迟阈值（可选）

延迟阈值用于在引擎排队不高但响应变慢时进行保护，单位为秒，0 表示关闭：

```
  --cache-aware-p50-e2e-threshold 5 \
  --cache-aware-p99-e2e-threshold 15 \
  --cache-aware-p50-ttft-threshold 1 \
  --cache-aware-p99-ttft-threshold 3
```

阈值应依据服务的 SLO 设定。注意：路由器测得的延迟为客户端侧延迟（含网络与代理开销），且仅对流式请求的 TTFT 有效；非流式请求的 TTFT 等同于端到端延迟，此时不建议启用 TTFT 阈值。

### 7.3 多副本部署

多副本部署时，跨副本负载感知依赖 `/metrics` 抓取，存在抓取周期的滞后（详见第 10 节约束 3）。建议缩短抓取间隔：

```
  --engine-stats-interval 3
```

粘滞决策由一致性哈希保证跨副本一致，无需额外配置。

### 7.4 阈值调优建议

- `--cache-aware-tolerate-waiting-requests`：设为单引擎可接受的最大排队请求数。过小会过早转发、降低缓存命中；过大会延迟过载保护。
- `--cache-aware-inflight-decay`：保持默认 300 秒即可；仅当存在大量超长请求时才需调大。
- `--cache-aware-stats-window`：指标统计窗口，默认 30 秒，影响监控曲线的平滑程度，不影响路由行为。

### 7.5 监控

通过 Prometheus 抓取路由器 `/metrics`，关注第 6 节列出的指标。`stickiness_rate` 持续偏低或 `fallback_rate` 偏高，通常表明引擎容量不足或阈值过严。

### 7.6 镜像说明

路由器镜像基于 `docker/Dockerfile` 构建。若使用本仓库默认构建，可通过 `INSTALL_OPTIONAL_DEP` 控制可选依赖。仅需 cache-aware 功能时无需安装 `lmcache`；如需同时使用 `kvaware` 路由模式，则必须保留 `lmcache` 依赖。

---

## 8. 多副本协同

多个路由器副本之间不直接通信，亦不依赖共享存储。协同依靠两个机制：

1. 粘滞决策：各副本均采用一致性哈希，且发现的引擎集合相同，因此同一 session 在任意副本均被路由至同一引擎，无需协调即可保持一致。
2. 负载感知：各副本独立抓取相同引擎的 `/metrics`。引擎上报的 running 计数包含所有副本产生的负载，是副本间感知彼此负载的唯一信号，但存在一个抓取周期的滞后。

跨副本负载感知的滞后限制及对策见第 10 节。

---

## 9. 实现演进中修复的关键缺陷

| 缺陷 | 修复方式 | 相关提交 |
|---|---|---|
| 并发 fallback 集中至单一引擎（惊群） | 引入在途计数；排序纳入 scraped running；并列时随机选择 | `c51c1ce` / `6298d20` / `beb37fc` |
| 在途计数按固定时间衰减，长请求被提前忽略 | 改为请求结束时精确递减，时间窗口仅作安全上限 | `dce7ff3` |
| 请求异常或客户端断开时在途计数未释放 | 将 `release_inflight` 置于 `try/finally`，任意退出路径均释放 | `7fb68eb` |
| 失败路径未清理每请求统计字典 | `try/finally` 中调用 `RequestStatsMonitor.discard_request`，覆盖流出错与客户端断开 | （本次） |
| 无流量时窗口指标停留在旧值 | `/metrics` 抓取时刷新并随时间衰减 | `7ae7410` |
| `RequestStatsMonitor` 按 request_id 的字典无界增长（上游既有） | 请求结束时清除对应条目 | `5c1b5ae` |

---

## 10. 已知约束

1. 路由器对后端无读超时（`request.py` 中使用 `timeout=None`，为上游既有行为）。当引擎无响应但连接未断开时，该请求流将持续占用连接；在途计数由 `--cache-aware-inflight-decay` 回收，但连接本身不会释放。建议在 vLLM 侧配置请求超时与健康探针，由 K8s 摘除异常 Pod。
2. 滚动重启期间正在终止的 Pod：本实现未包含终止状态检测，依赖上游 K8s readiness。滚动发布瞬间可能有少量请求被路由至正在关闭的 Pod。建议配置 Pod 优雅关闭；如需更强保证可补充终止状态检测。
3. 同一抓取周期内多副本同时出现流量突发：跨副本负载感知存在抓取滞后，此场景下仅依靠并列随机进行统计性分散，无法完全消除集中。建议多副本部署将 `--engine-stats-interval` 设为 3–5 秒；单副本部署不受此约束影响。

---

## 11. 提交记录（分支 `sss-dev`）

| 提交 | 内容 |
|---|---|
| `22c83cc` | 提交规范规则与本文档 |
| `7a5294b` | 延迟分位数与超限快照（`request_stats`） |
| `830afb4` | 核心路由器、排队阈值与 fallback |
| `182773f` | p50/p99 TTFT 与 p50/p99 端到端阈值 |
| `fadcdc5` | 窗口化 Prometheus 指标 |
| `3ea4b49` | 代码简化 |
| `c51c1ce` | 惊群修复（在途计数） |
| `dce7ff3` | 在途计数改为完成精确计数 |
| `6298d20` | 排序纳入 scraped running（跨副本） |
| `beb37fc` | 并列随机选择 |
| `7ae7410` | 代码评审收口 |
| `7fb68eb` | 故障路径释放在途计数；新增在途指标 |
| `5c1b5ae` | 修复统计字典内存泄漏；补充回归测试 |
| `52c1c33` | 文档改写为正式说明并补充使用说明 |
| `5739b4a` | fallback 决策结构化日志（含原因与两端负载） |

测试：单元测试全量 81 项通过；稳定性测试 P1–P5（故障注入、引擎增删、30 分钟长稳、过载、多副本）均通过，详见对应提交。
