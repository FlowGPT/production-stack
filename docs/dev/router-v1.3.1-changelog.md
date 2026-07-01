# v1.3.1 更新日志 — 多副本生产化（Redis 共享亲和 + 可靠性 + 容量信号）

在 v1.3 的 load_balanced_affinity 路由（见 router-v1.3-changelog.md）基础上，让它在"**多副本 + 每副本多 worker**"的线上拓扑里做到**正确聚合、可观测饱和、抗 Redis 故障**。镜像 ssadds/production-stack-router:v1.3.1（在 v1.2 上覆盖式 overlay，构建见 docker/Dockerfile.v131）。

真机验证见 router-load-balanced-affinity-multireplica-redis-validation.md（8×H100，6/6 PASS）。

## 1. Redis 共享亲和（跨副本一致）

- --lb-affinity-store memory|redis。redis 模式下 session 到 engine 的映射经一个 Redis 实例**跨所有 worker / 副本共享**，任意副本路由回访都读到同一记忆引擎，解决 v1.3 memory 后端在"多副本 / 无 ingress sticky"时的亲和稀释（真机实测：跨副本命中 **3.2 倍**于 memory）。
- 键为 prefix + xxhash(session_id)，值为 engine url，Redis TTL 淘汰。
- **写节流**（--lb-affinity-redis-refresh-fraction，默认 0.5）：回访多为同引擎的 TTL 刷新，窗口内跳过 SET，命中热路径降到 **1 个 GET**；改引擎（shed / 重放置）必写穿，保证其他副本读到新家。真机 SET/GET 从 1.00 降到 0.90（随回访密度放大）。
- 参数：--lb-affinity-redis-url / -key-prefix / -timeout（默认 50ms）/ -refresh-fraction。
- 真实开销：跨网 LAN 约 33µs/请求（1 个 GET），相对秒级 prefill 可忽略；端到端 redis vs memory 吞吐 / TTFT 持平。

## 2. Redis 非强依赖（软依赖 + 自愈 + 断路器）

- **软依赖**：Redis 启动连不上**不 crash**，打 warning 照常起（fail-open 退化为纯 P2C），Redis 恢复后**自动开始用、无需重启**。想要强一致保证用 --lb-affinity-redis-required opt-in 硬失败（K8s 下 crash-loop）。
- **运行时 fail-open**：任何 Redis 错误退化为一次新放置，不阻塞路由，计入 vllm:load_balanced_affinity_store_errors_total{operation}。
- **断路器**：连续失败（默认 3 次）后打开（默认冷却 5s），冷却期内**跳过 Redis 调用**（零阻塞），半开探一次自动恢复。防止 Redis 挂死（TCP blackhole）时**同步客户端把事件循环阻塞成秒级 lag**，真机曾观测到峰值 event-loop lag 3.6s，断路器把 outage 期间的阻塞从"每请求撞一次超时"降到"每冷却窗几次"。

## 3. 容量 / 何时加副本

Router 是每 worker 一个 asyncio 事件循环的代理，"扛不住"= loop 处理不过来（CPU 打满 / 同步阻塞 / 协程过多）。新增两个信号：

- **router_event_loop_lag_seconds**（gauge，livemax 跨 worker 取最差）：事件循环调度漂移，最灵敏的 router 饱和**先行**信号，一个数字同时捕捉 CPU / 阻塞 / 过载三种原因。**建议 >50ms 报警、>100ms 扩容**（真机 c768 峰值约 350ms，与 TTFT 劣化同步）。适合挂 HPA。
- **router_active_requests**（gauge，livesum 跨 worker 求和）：正在代理的并发（真机实测精确等于打到该副本的并发）。
- **判别 router-bound vs engine-bound**：只有 router 侧（lag / CPU）高、引擎侧（waiting / GPU）有余量时加 router 副本才有用；引擎满时要加引擎。真机 §2.2 印证：加副本能摊分 router 层负载、每副本 lag 回落，但当瓶颈在共享引擎时 TTFT 不变。

## 4. 多 worker / 多副本指标聚合

- **Counter**（placement / hit / shed / store_errors_total）多 worker 经 MultiProcessCollector **自动求和**，跨副本再靠 Prometheus sum() 聚合，集群口径正确（真机：两副本 counter 相加 == driver 实发，ratio 1.000）。
- **集群 QPS 必须用 counter 速率**，不能用 sum(vllm:current_qps)：后者是 mostrecent 窗口 gauge，多 worker 下只留某个 worker 的值，欠计约 1/N（真机实测差约 5 倍，正是"总 QPS 接近 10、面板显示 5.57"的根因）。
- 容量 gauge 用 livesum / livemax 正确聚合；按引擎的窗口 gauge 仍 mostrecent（相对均衡 / CV 可信，绝对值偏小）。
- 面板 PromQL 与判读见 router-load-balanced-affinity-dashboard.md。

## 5. Carry-forward 修复（打包在同一镜像）

- **K8s 服务发现 reconcile**：周期性全量核对 pod 列表，清理 watch 事件漏掉的 stale 引擎（与死引擎守卫互补）。
- **非 ASCII header 转发**：转发时按原始 wire 字节传 header，修复 header 值含中文 / emoji（如 X-Flow-Conversation-Id）导致的裸 500，避免误 fallback。

## 升级

换镜像 ssadds/production-stack-router:v1.3.1。默认行为与 v1.3 一致（memory 亲和）；要跨副本一致亲和，追加下面的参数（--lb-affinity-redis-required 可选，加上则 Redis 变启动硬依赖，默认软依赖）：

```bash
--lb-affinity-store redis \
  --lb-affinity-redis-url redis://<host>:6379/0
```

注：§4.3 慢-Redis 事件循环污染（3.6s lag）的断路器修复已并入本镜像；报告中的 v1.3.1 数据是**加断路器前**测的，建议按验证文档 §4.3 复测确认 outage 期间 lag 不再飙升（预期 store_errors_total 在 outage 期间近乎持平而非每请求 +1）。
