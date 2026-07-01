# load_balanced_affinity 路由更新日志 — P2C 负载均衡 + 自卸载亲和

> 新增路由模式 `load_balanced_affinity`：用 power-of-two-choices(P2C)按负载摆放请求，会话亲和「能省则省、贵了就卸」。目标——**粘滞没价值时性能不低于轮询，粘滞有价值时顺手吃到 KV 命中**；QPS 比一致性哈希更均衡，长尾比轮询更短。不改动现有 `cache_aware_load_balancing`。

## 解决的问题

现有两条路在「小集群 / 上层粘滞差 / 会话权重不均」时都不理想：

1. **`session`（一致性哈希）**：按 session key **个数**摊到环上，不按「重量」摊。会话数少、或个别会话又长又频时，QPS 天然不均（实测 max/min ~1.3），不均直接转成长尾。
2. **`roundrobin`（轮询）**：请求**计数**完美均衡，但对负载**无感知**——某引擎刚抽到一条 whale（长输出）正卡着，轮询照样往里塞短请求，短请求被拖进长尾。
3. **延迟/队列**当**硬阈值**做过载判定：窗口内一次超时就把 p99 打爆 → 该引擎被整体摘掉 → 流量成群涌向下一个引擎把它压垮 → 来回震荡。实测「拿延迟当阈值会让整体流量崩掉」。

## 新方法

`LoadBalancedAffinityRouter`，两层解耦：

### 1. 摆放层：power-of-two-choices

随机抽 `d`（默认 2）个引擎，选**有效负载**更低的那个：

```
有效负载 L(e) = 抓取的 num_running_requests
             + 抓取的 num_queuing_requests
             + 本路由器对该引擎的 in-flight 计数
```

- **比轮询强**：负载感知，主动避开瞬时热点；轮询做不到。
- **比哈希强**：哈希摆放一次性会话 = 随机摆放 → Poisson 方差，反而比轮询差；P2C 拉平方差。
- **比「全局最小」强**：多副本各自抓到同一份 stale 视图时，全局最小会一致地涌向同一引擎（herding）；P2C 随机采样天然打散，**无需跨副本协调**。
- **无任何硬阈值**：忙的引擎只是「按比例少分」，单条超时不会把整条流量一次性甩走，从根上消除震荡。

### 2. 亲和层：自卸载

需要 `--session-key`，默认开。把会话**实际落到**的引擎记下来（不是哈希说该去哪），回访时只在「记住的引擎不比一次新 P2C 抽样明显更忙」（差距 ≤ `--lb-affinity-slack`）时才贴回去，否则**卸载**到更轻的引擎并改记新引擎。

- 亲和**永不牺牲均衡**：贵了立刻卸。
- 复用少（多为一次性会话）时退化成纯 P2C，即**不差于轮询**——这正是核心诉求。

### 3. 死引擎守卫

抓不到 stats 的引擎给一个**有限大**惩罚 `_NO_STATS_PENALTY`（非 0），让「被服务发现列出但其实连不通、什么都不上报」的 ghost 引擎不会因为「看起来很闲」被当成摆放磁铁；同时冷启动（全部未知 → 全部相等 → 随机）仍正常。

## 证明：比轮询/哈希更优

### 离散事件排队仿真（`scripts/routing_sim.py`）

仿真直接驱动**真实** router 类（`RoundRobinRouter` / `SessionRouter` / `LoadBalancedAffinityRouter`），只把引擎和时钟模拟掉：引擎按 `capacity` 连续批处理、超出排队；每 token 时延随在跑 batch 增大（GPU 争用 → 负载不均→超线性长尾）；输出长度重尾（5% whale）。`slowdown = 实际时延/理想服务时间`，剥离请求自身大小、只看路由可控的「排队+争用」。

默认场景（8 引擎 × cap 8，目标利用率 85%，5% whale，seed 2026）：

| 模式 | 计数 max/min | 计数 CV | slowdown p50 | p95 | **p99** | 小请求 p99(s) |
|---|---|---|---|---|---|---|
| `session`（哈希） | 1.31 | 0.095 | 1.90 | 28.2 | **58.3** | 54.3 |
| `roundrobin` | 1.00 | 0.000 | 1.60 | 15.5 | **35.5** | 40.4 |
| `load_balanced`（无亲和） | 1.05 | 0.014 | 1.60 | 2.26 | **3.82** | 6.17 |
| `load_balanced_affinity` | 1.07 | 0.019 | 1.60 | 2.25 | **3.69** | 6.17 |

读法：

- **对轮询**：计数均衡度相当，但 slowdown p99 **降约 9.5×**、小请求 p99 **降约 6.5×**——轮询的计数均衡挡不住长尾，因为它对负载无感知。
- **对哈希**：均衡度（1.05 vs 1.31）**和**长尾（3.7 vs 58）**双赢**。
- **亲和开≈亲和关**：自卸载使亲和不拖累均衡/长尾；回访比例高时亲和反而略好（见下）。

跨 seed(1/7/99)、利用率(0.7/0.9)、whale 比例(10%)、回访比例(60%) 复跑，排序稳定不翻转。回访 60% 时 `affinity` 的 p99(4.03) 优于 `noaffinity`(4.28)——亲和有价值时顺手吃到。

复现：
```bash
python scripts/routing_sim.py                       # 默认场景
python scripts/routing_sim.py --seed 7 --utilization 0.9 --whale-frac 0.10
```

### 镜像端到端 mock 冒烟

`ssadds/production-stack-router:v1.3` 容器跑 `--routing-logic load_balanced_affinity`，挂 3 个 mock 引擎（其中一个静态 `num_requests_waiting=8`），打 120 条带随机 session 的请求：

- 120/120 → `200`，无裸 500。
- `load_balanced_placement_total=39` + `affinity_hit_total=81` = 120，分区不变式成立（≈40 个不同会话各摆放一次，其余贴回）。
- 那个 `waiting=8` 的引擎 **0 流量**——P2C 正确避开重载引擎，另两个均分。

## 配置

```bash
--routing-logic load_balanced_affinity
--session-key x-session-id            # 亲和需要；不给则纯 P2C
--lb-d-choices 2                      # P2C 采样数，默认 2
--lb-affinity / --no-lb-affinity      # 亲和开关，默认开
--lb-affinity-slack 0                 # 记住的引擎可比新抽样忙多少（请求数）仍贴回；越大越粘
--lb-affinity-ttl 3600                # 会话→引擎映射保留秒数
--lb-inflight-decay 300               # in-flight 计数安全上限（清掉未观测到完成的）
--lb-stats-window 30                  # affinity-hit-rate 指标窗口
--lb-affinity-max-size 0             # 内存后端 LRU 上限，0=不限（仅 TTL 淘汰）

# 亲和映射后端（多副本/多 worker 共享）：
--lb-affinity-store memory|redis      # 默认 memory（按副本/worker 独立）
--lb-affinity-redis-url redis://h:6379/0   # store=redis 时必填
--lb-affinity-redis-key-prefix vllm:lb-affinity:
--lb-affinity-redis-timeout 0.05      # 单次读写 socket 超时（秒）；超时即 fail-open
--lb-affinity-redis-refresh-fraction 0.5   # 写节流：同引擎在 TTL*该比例 内跳过刷新 SET
--lb-affinity-redis-required          # 默认关：redis 非启动强依赖，连不上则 fail-open 并自愈
```

新指标：`vllm:load_balanced_placement_total` / `_affinity_hit_total` / `_affinity_shed_total`（三者分区全部请求）、`_affinity_hit_rate`（窗口命中率）、`vllm:load_balanced_inflight_requests{server}`、`load_balanced_affinity_store_errors_total{operation}`（redis 读写失败计数）。容量/扩容信号：`router_event_loop_lag_seconds`、`router_active_requests`。

## 升级

换镜像 `ssadds/production-stack-router:v1.3.1`（在 v1.2 上覆盖式 overlay，构建见 `docker/Dockerfile.v131`），把 `--routing-logic` 切到 `load_balanced_affinity` 即可；不切则行为与 v1.2 一致。按版本拆分的更新说明见 `router-v1.3-changelog.md`（路由本身）与 `router-v1.3.1-changelog.md`（redis 共享亲和 / 软依赖+断路器 / 容量信号 / 多副本聚合 / carry-forward 修复）。本文档是设计与仿真的**深度参考**。

## 已知限制 / 后续

- **跨副本一致亲和已支持**（`--lb-affinity-store redis`）：内存后端仍按副本/worker 独立（同会话命中别的副本会按负载重新摆放，均衡不受影响，只少一次缓存命中）；需要跨副本一致时切 redis。redis 为**软依赖**——启动连不上不 crash，fail-open 退化为纯 P2C 并在 redis 恢复后自愈（要严格保证可加 `--lb-affinity-redis-required` 改回硬失败）。
- **指标抓取扇出**（每副本/worker 各抓所有引擎）未在本次处理；大规模下用「leader 抓一次写 redis、各副本读快照」降扇出，列为后续。
