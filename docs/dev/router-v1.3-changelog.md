# v1.3 更新日志 — load_balanced_affinity 路由

新增路由模式 load_balanced_affinity：Power-of-Two-Choices 负载放置 + 自卸式会话亲和，**同时**拿到"轮询的 QPS 均衡与短长尾"和"哈希的前缀缓存亲和"，且**没有任何二值过载阈值**（避免"窗口一超时全体 fallback"的级联震荡）。

注：设计动机、仿真证明、参数详解见 router-load-balanced-affinity-changelog.md；离线复现 scripts/routing_sim.py。多副本 / redis / 容量 / 故障相关能力在 **v1.3.1**（见 router-v1.3.1-changelog.md）。

## 路由逻辑

- **放置（P2C）**：随机采样 lb-d-choices（默认 2）个引擎，选 effective_load 最低者，平手随机。effective_load = 引擎上报 running + queuing + 本 router 进程 in-flight。busier 的引擎只是拿到按比例更少的请求，不是被一刀切开。
- **自卸亲和（需 session-key）**：记住会话到引擎，回访时**仅当**记忆引擎不比一次新 P2C pick 忙超过 lb-affinity-slack（请求数）才贴回；否则 shed 到更轻引擎并改记。所以亲和**从不倒贴均衡**，低复用负载自动退化为纯 P2C（不差于轮询）。
- **死引擎守卫**：抓不到 stats 的引擎给一个有限大惩罚（非 0），既不会因"看起来很闲"被当放置磁铁，冷启动全未知仍随机均分。
- **in-flight**：本进程已派发未完成的请求计数，补足周期性 stats 抓取之间的即时负载反馈，让同副本内的并发 P2C 决策彼此可见。lb-inflight-decay 兜底清理未观测到完成的计数。

## 亲和存储

本版为**进程内 memory**（TTL + LRU，lb-affinity-max-size）。多副本 / 多 worker 下按进程独立，同会话命中别的进程会被按负载重新放置（均衡不受影响，只少一次缓存命中）。跨副本一致亲和用 redis，见 v1.3.1。

## 指标

- vllm:load_balanced_placement_total / _affinity_hit_total / _affinity_shed_total —— 三者**分区全部请求**（首访 / 无 session / 亲和关 = placement；回访贴回 = hit；回访改投 = shed）。
- vllm:load_balanced_affinity_hit_rate —— 窗口命中率 hit/(hit+shed)（gauge，多 worker 下 mostrecent，集群口径请用上面 counter 的 rate 重算）。
- vllm:load_balanced_inflight_requests{server} —— 本 router 派发的在途请求。

## 参数

各参数含义：

- --routing-logic load_balanced_affinity —— 启用本模式
- --session-key x-session-id —— 亲和键；不给则纯 P2C
- --lb-d-choices 2 —— P2C 采样数
- --lb-affinity / --no-lb-affinity —— 亲和开关，默认开
- --lb-affinity-slack 0 —— 记忆引擎可比新抽样忙多少（请求数）仍贴回；越大越粘
- --lb-affinity-ttl 3600 —— 会话到引擎映射保留秒数
- --lb-affinity-max-size 0 —— 内存后端 LRU 上限，0 = 不限（仅 TTL 淘汰）
- --lb-inflight-decay 300 —— in-flight 计数安全上限
- --lb-stats-window 30 —— hit-rate 指标窗口

示例（可直接粘贴）：

```bash
--routing-logic load_balanced_affinity \
  --session-key x-session-id \
  --lb-d-choices 2 \
  --lb-affinity \
  --lb-affinity-slack 0 \
  --lb-affinity-ttl 3600 \
  --lb-affinity-max-size 0 \
  --lb-inflight-decay 300 \
  --lb-stats-window 30
```

## 升级

把 --routing-logic 切到 load_balanced_affinity（加 --session-key 启用亲和）即可；不切则行为与 v1.2 一致。镜像随 v1.3.1 发布（含本路由 + 多副本生产化），见 router-v1.3.1-changelog.md。
