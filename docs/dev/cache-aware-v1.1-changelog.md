# v1.1 更新日志 — 回访会话指标 + Redis 多副本

> 镜像：`ssadds/production-stack-router:v1.1`
> digest：`sha256:e713ffe1df19efadf4ee3626479fe2b0c20fd0fd358a7c70b9e7257fafbb137e`
> 在 v1.0（cache-aware 路由）基础上的一次**纯观测性**更新：新增回访会话指标，路由行为不变。
> 详细设计见 [cache-aware-returning-session.md](cache-aware-returning-session.md)。

> **基线说明**：v1.1 是 v1.0 的**超集**——保留 v1.0 的全部能力，包括容量瞬时队列触发参数 `--cache-aware-engine-max-concurrency`（突发时用 in-flight 估算队列、抢在 scrape 之前触发 fallback），原有 `--cache-aware-*` 参数均不变。本次只在其上**新增**回访会话观测。

## 新增能力

1. **回访请求漏斗指标**：把所有带 session 的请求分成「新请求 N / 回访请求 R」，各再分 sticky / fallback，从本 Provider 视角回答「多少新请求、多少回访、回访里粘滞率多少」。
2. **可插拔回访状态存储**：`memory`（单副本）/ `redis`（多副本共享回访状态）。
3. **memory 后端 O(1)**：`OrderedDict` 队头淘汰，无全表扫描；可选 `max_size` LRU 上限。
4. **redis 本地缓存**：每副本 LRU 跳过 Redis 往返（适合 LB 亲和），Redis 仍是跨副本权威。
5. **故障策略**：启动连不上 Redis → fail-hard（暴露配置错误）；运行期 Redis 抖动 → fail-open（推理不受影响），错误计入指标。

## 这功能具体有啥用

背景:Router 在**单个 Provider 内**做会话→引擎的粘滞(同一 session 尽量固定到同一引擎以命中 KV cache);但**上层多 Provider 调度**的会话粘滞往往不稳,同一个用户会话可能这次进 A Provider、下次被切到 B。v1.0 的 `cache_aware_stickiness_rate` 统计**所有**带 session 的请求,而生产里大量 session 只来一次(一次性),这些"首次访问"会把粘滞率**稀释/抬高**,导致看不清真正回访用户的情况。v1.1 把"回访"单独拎出来度量,解决以下实际问题:

1. **分清一次性用户 vs 回访用户。**
   `returning_request_ratio` = 流量里有多少是"老会话又回到本 Provider"。一次性占比高的业务,旧指标会高估粘滞;新指标只看回访子集,不被首次访问污染。

2. **诊断上层多 Provider 的会话粘滞质量(本功能最大价值)。**
   `returning_request_ratio` **偏低** = 上层很少把同一会话送回本 Provider(上层粘滞差,KV 复用机会被浪费);**偏高** = 上层粘滞好。这是直接评估"上层调度有没有把会话稳定留在同一 Provider"的信号——以前没有任何指标能回答这个。

3. **量化 cache-affinity 的真实收益面。**
   只有**回访请求**才可能命中已有 KV cache。`returning_stickiness_rate` **高** = 回访用户确实被粘回原引擎(cache 有机会命中,省 prefill);**低** = 回访被 fallback 冲散到别的引擎(等于每次冷启动,affinity 收益打折)。这比笼统的总 stickiness 更能说明"粘滞到底有没有带来 cache 价值"。

4. **容量 / 过载的早期信号。**
   `returning_fallback_rate` 上升 = 引擎过载到连回访用户都被迫换引擎——既丢 KV cache 又是扩容信号。比总 fallback 更敏感地反映"高价值流量受影响了"。

5. **多副本统一口径(Redis)。**
   多个 Router 副本默认各记各的,同一会话打到不同副本会被误判成"新请求",回访指标偏低。`store=redis` 让所有副本共享"这个 session 见过没",指标在集群层面才准确。

**它不改变路由**:以上全是观测,选哪台引擎、过载/ fallback 逻辑与 v1.0 完全一致。

### 典型读图

| 现象 | 含义 | 处置 |
| --- | --- | --- |
| `returning_request_ratio` 很低 | 上层多 Provider 粘滞差,会话很少回到本 Provider | 推动上层做 client→Provider 亲和;否则本层 cache 收益有限 |
| `returning_stickiness_rate` 高、总 `stickiness_rate` 也高 | 回访用户稳定粘在原引擎,cache 复用良好 | 健康 |
| `returning_stickiness_rate` 明显低于总 `stickiness_rate` | 回访(高价值)流量正被 fallback 冲散,而一次性流量拉高了总数 | 查引擎过载;调 `tolerate_waiting_requests` / 扩容 |
| `returning_fallback_rate` 抬升 | 引擎过载,连回访用户都被迫换引擎 | 容量告警:扩容或降负载 |
| `returning_session_store_errors_total` 增长 | Redis 故障,回访指标失真(推理不受影响) | 查 Redis,恢复前回访数据按偏低看待 |

> 量级直觉(取自压测):10 万请求、2.7 万 unique session → 首次约 27%、回访约 73%;若只看旧的总 stickiness,2.7 万次首访会把分母撑大、掩盖回访用户的真实粘滞。

## 新增指标

Counter：`cache_aware_first_visit_routed_total` / `_sticky_total` / `_fallback_total`、`cache_aware_returning_routed_total` / `_sticky_total` / `_fallback_total`、`cache_aware_returning_session_store_errors_total{operation}`
Gauge：`cache_aware_first_visit_request_ratio`、`cache_aware_returning_request_ratio`、`cache_aware_returning_stickiness_rate`、`cache_aware_returning_fallback_rate`

不变量：`first_visit_routed + returning_routed == sticky_total + fallback_total`（原有 `cache_aware_*` 指标语义与数值不变）。

## 新增参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--cache-aware-returning-session-ttl` | 3600 | 回访识别 TTL（秒）；仅影响指标归类，**不影响路由粘滞** |
| `--cache-aware-returning-session-store` | memory | `memory` / `redis` |
| `--cache-aware-returning-session-redis-url` | — | `store=redis` 必填 |
| `--cache-aware-returning-session-redis-key-prefix` | `vllm:returning-session:` | Redis key 前缀 |
| `--cache-aware-returning-session-redis-timeout` | 0.05 | 单次 Redis 超时（秒），超时 fail-open |
| `--cache-aware-returning-session-max-size` | 0 | memory LRU 上限（0=不限） |
| `--cache-aware-returning-session-local-cache-size` | 0 | redis 本地缓存条数（0=关） |

## 兼容性与开销

- **日志默认 INFO（性能/成本改进）**：`init_logger` 默认级别从 DEBUG 改为 INFO，可由环境变量 `VLLM_ROUTER_LOG_LEVEL` 或 `--log-level` 调整。此前默认 DEBUG 会对每个请求打印整串 header 等，在生产 QPS 下占用事件循环 CPU 与 stdout I/O；改为 INFO 后这些 per-request DEBUG 不再输出。排障时设 `--log-level debug` 即可恢复。
- **路由行为不变**：选引擎/过载/fallback/hash ring 一律未改，新代码只多记一个指标标签。
- 默认 `store=memory`，行为与 v1.0 一致；每请求多一次 `visit()`：memory ~0.9µs（与基数无关），redis 同节点 ~0.18ms（端到端影响 < 3%）。
- 原有 `cache_aware_*` 指标不变。

## 部署要点

- 镜像构建：沿用 v1.0 的方式（`semantic_cache`）并加上 `redis`，去掉 lmcache。Dockerfile 默认已是 `INSTALL_OPTIONAL_DEP=semantic_cache,redis`，直接 `docker build -t <repo>:v1.1 -f docker/Dockerfile .` 即可（约 5.8GB，与 v1.0 体量一致）。
- 多副本：所有 Router 副本连**同一个** Redis Service（`--cache-aware-returning-session-redis-url` 填同一 URL）；Redis 建议 `512Mi` + `maxmemory 400mb` + `volatile-ttl`，可扛百万级活跃 session。完整 K8s 配置见 [cache-aware-returning-session.md](cache-aware-returning-session.md)。

## 验证

- 单测 53 项通过（漏斗分区/比率/TTL/O(1) 淘汰/LRU/本地缓存/redis fail-open）。
- 大规模稳定性：真 vllm（gemma-4-31B）+ mock 多引擎共 **56,278 条请求**，漏斗计数对 ground truth **零偏差**、分区不变量精确、inflight 排空、跨引擎 fallback 正常、Redis/本地缓存零错误。
