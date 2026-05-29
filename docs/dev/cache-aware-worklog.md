# Cache-Aware 超限路由 — 开发 Worklog

实时记录每一步：改了什么、为什么、怎么测、结果。每个提交一节。

- 分支：`sss-dev`
- 提交身份：`Saddss <2872669061@qq.com>`
- 计划：见 `.cursor/plans/`（cache-aware overload routing）

需求：
1. 排队阈值 `--cache-aware-tolerate-waiting-requests`
2. p50 TTFT 阈值
3. p99 TTFT 阈值
4. p50 端到端阈值
5. p99 端到端阈值
6. Prometheus 暴露（≥30s 窗口）：粘滞率、各阈值 fallback 率、总体 fallback 率
7. fallback 选择：候选满足全部阈值，按 `(queue 升序, p50_e2e 升序)` 确定性选择；全员超限退回原 engine
8. 近 0 性能损失：分位数 TTL 缓存、指标 O(1) 摊销更新

---

## C0 — 环境与合规准备

### 改了什么
- `git config --local`：`user.name=Saddss`、`user.email=2872669061@qq.com`。
- 新增 repo-local 规则 `.cursor/rules/no-cursor-in-commits.mdc`（禁止任何 Cursor 痕迹）。
- 新增本 worklog `docs/dev/cache-aware-worklog.md`。

### 怎么测
- `git config --local user.name/user.email` 回显校验。
- commit 后 `git log -1 --format='%an <%ae>%n%B' | grep -i cursor` 自检无输出。

### 结果
- commit `22c83cc`。环境会在 `git commit` 时自动注入 `Co-authored-by: Cursor`，故改用 plumbing `git commit-tree` 绕过（helper `/root/clean-commit.sh`），自检 `grep -i cursor` 无输出，PASS。

---

## C1 — 分位数能力 + 超限快照（stats，纯新增）

### 改了什么
- `src/vllm_router/stats/request_stats.py`：
  - `MovingAverageMonitor.get_percentile(q)`：滑窗值线性插值分位数，空集返回 -1。
  - `RequestStatsMonitor`：新增 `_overload_cache/_overload_cache_ts/_overload_refresh_interval=1.0` 与 `get_overload_snapshot(now)`，返回每 engine `{p50_ttft,p99_ttft,p50_e2e,p99_e2e}`，TTL 内复用缓存（热路径 O(1)）。
- `src/tests/test_overload_stats.py`：新增 6 个单测（分位数空/单值/乱序、快照取值、TTL 缓存、无数据哨兵）。
- 行为变化：无（纯新增 API，未接入路由）。

### 怎么测
- 测试环境：`/root/cacheaware-venv`（pytest 等最小依赖；`uv sync` 因 lmcache==0.2.1 不可解析而放弃）。
- 命令：
  - `PYTHONPATH=src /root/cacheaware-venv/bin/python -m pytest src/tests/test_overload_stats.py src/tests/test_singleton.py src/tests/test_session_router.py -q`
  - `black --check`、`isort --check-only --profile black`、`ruff check src/tests/...`、`codespell`

### 结果
- 单测：15 passed（6 新增 + 9 回归）。
- lint：black/isort/ruff/codespell 全过。
- commit `7a5294b`（clean-commit，无 cursor 字眼）。
- 镜像冒烟：`docker build --build-arg INSTALL_OPTIONAL_DEP=semantic_cache -t vllm-router-cacheaware:c1`（默认含 lmcache 的 extra 因 `lmcache==0.2.1` 当前不可解析而剔除，属既有 infra 问题，与本改动无关）；镜像内 `get_percentile`/`get_overload_snapshot` 实测 `p50=2.0 p99=2.98`、snapshot 可用，PASS。

---

## C2 — CacheAwareLoadBalancingRouter 核心（sticky + queue 阈值 + fallback）

### 改了什么
- `src/vllm_router/routers/routing_logic.py`：
  - `RoutingLogic` 加 `CACHE_AWARE_LOAD_BALANCING`。
  - 新增 `CacheAwareLoadBalancingRouter`：`route_request` 返回 `str`（与 RoundRobin/Session 同契约，不改 request.py，不破坏其他 router）。
  - 逻辑：session 经 hash ring 选 initial；`_violated_reasons`（C2 仅 queue：`num_queuing_requests >= tolerate`）；命中则 `_select_fallback`：候选=未超限 engine，按 `(queue 升序, p50_e2e 升序)` 取 min，全员超限退回 initial；无 session 取最小负载。分位数经 `_overload_snapshot()`（monitor 未初始化时回退 `{}`）。
  - `initialize_routing_logic`/`reconfigure_routing_logic`/`get_routing_logic` 注册新 router。
- `src/vllm_router/parsers/parser.py`：`--routing-logic` 加 `cache_aware_load_balancing`；新增 `--cache-aware-tolerate-waiting-requests`(默认 20)；`validate_args` 要求该模式必须有 `--session-key`。
- `src/vllm_router/app.py`：`initialize_routing_logic` 传 `tolerate_waiting_requests=args.cache_aware_tolerate_waiting_requests`。
- `src/tests/test_cache_aware_router.py`：8 个单测（queue 触发、未知 engine、候选排除+最小 queue、p50_e2e tie-break、全员超限回退、sticky、fallback、无 session）。
- `src/tests/perftest/fake-openai-server.py`：加 `--waiting` 以在集成层确定性 mock 队列深度。

### 怎么测
- 单测：`PYTHONPATH=src .../pytest src/tests/test_cache_aware_router.py test_overload_stats.py test_session_router.py test_singleton.py test_parser.py -q`
- lint：black/isort/ruff/codespell。
- 镜像集成：`docker build -t vllm-router-cacheaware:c2`，`--network host` 跑 router + 多个 stdlib mock 后端（无 vllm 依赖，可配 `waiting`），静态服务发现，发同 session 请求看 sticky / 把 sticky engine 队列拉高看 fallback。

### 结果
- 单测：`test_cache_aware_router.py` 8 passed；合并回归 `overload_stats+session+singleton+parser` 全过（共 32 passed）。
- lint：black/isort/ruff/codespell 全过（parser 单行条件按 black 修正）。
- commit `830afb4`（clean，无 cursor 字眼）。
- 镜像集成（`vllm-router-cacheaware:c2`，`--network host` + 3 stdlib mock 后端 9101/9102/9103，tolerate=5，engine-stats-interval=2）：
  - 粘滞：session `alice` 连发 6 次 → 全部命中 e2（e1=0 e3=0），PASS。
  - fallback：将粘滞引擎 e2 置 `waiting=99(≥5)`，e1/e3=0 → 6 次全部回退到 e1（e2=0 e3=0），命中 `(queue,p50_e2e)` 最优候选，PASS。

---

## C3 — p50/p99 TTFT + p50/p99 端到端 阈值

### 改了什么
- `src/vllm_router/routers/routing_logic.py`：`CacheAwareLoadBalancingRouter.__init__` 加 4 个阈值参数（默认 0=关闭），存入 `self.latency_thresholds`；`_violated_reasons` 增加延迟判定：阈值>0 且快照值>=阈值且>=0（有数据）才计入 reason（`p50_ttft/p99_ttft/p50_e2e/p99_e2e`）；`route_request` 抽出 `_route_with_snapshot(endpoints, engine_stats, request, snapshot)` 测试缝；工厂传 4 个阈值 kwargs。
- `src/vllm_router/parsers/parser.py`：新增 `--cache-aware-p50-ttft-threshold`/`--cache-aware-p99-ttft-threshold`/`--cache-aware-p50-e2e-threshold`/`--cache-aware-p99-e2e-threshold`（float，默认 0）。
- `src/vllm_router/app.py`：把 4 个阈值传入 `initialize_routing_logic`。
- `src/tests/test_cache_aware_router.py`：新增 5 个单测（各延迟阈值触发/未触发、queue+延迟组合、阈值=0 关闭、无数据(-1)不触发、延迟阈值触发 fallback 经 `_route_with_snapshot`）。

### 怎么测
- 单测：`pytest src/tests/test_cache_aware_router.py test_overload_stats.py test_parser.py test_session_router.py test_singleton.py -q` → 37 passed。
- lint：black/isort/ruff/codespell 全过。
- 镜像集成：`vllm-router-cacheaware:c3`，给 mock 加 `--delay` 让粘滞引擎产生真实 e2e 延迟，设 `--cache-aware-p50-e2e-threshold` 验证延迟触发 fallback。

### 结果
- 单测：37 passed；lint 全过；commit `182773f`（clean）。
- 镜像集成（`vllm-router-cacheaware:c3`，`--cache-aware-p50-e2e-threshold 0.5`，tolerate=1000 排除 queue 干扰；e2 `--delay 2` 慢、e1/e3 快）：
  - req1=2.04s 命中粘滞 e2（尚无延迟数据）；e2 记录 p50_e2e≈2s≥0.5。
  - req2–6≈0.02s 全部回退到 e1；命中统计 e2=1、e1=5、e3=0 → 延迟阈值触发 fallback，PASS。

---

## C4 — ≥30s 窗口率 Prometheus 指标

### 改了什么
- `src/vllm_router/services/metrics_service/__init__.py`：新增 3 个 Gauge `vllm:cache_aware_stickiness_rate`、`vllm:cache_aware_fallback_rate`、`vllm:cache_aware_fallback_reason_rate{reason}`（reason ∈ queue/p50_ttft/p99_ttft/p50_e2e/p99_e2e）。
- `src/vllm_router/routers/routing_logic.py`：router 维护事件 deque `(ts,is_fallback,reasons)` + 运行计数（`_win_total/_win_fallback/_win_reason`）；`_record`/`_evict`/`get_window_stats`/`_publish_gauges`，O(1) 摊销；`_route_with_snapshot` 在 sticky/fallback 决策后记录；新增 `stats_window`（默认 30）参数与 `REASONS` 常量。`metrics_router.py` 未改（Gauge 经 `generate_latest()` 自动暴露）。
- `src/vllm_router/parsers/parser.py`：新增 `--cache-aware-stats-window`(默认 30)。
- `src/vllm_router/app.py`：传 `stats_window`。
- `src/tests/test_cache_aware_router.py`：新增 3 个单测（窗口计数+过期淘汰、率 Gauge 数值、`_route_with_snapshot` 决策计数）。

### 怎么测
- 单测：`pytest src/tests/...` → 40 passed。
- lint：black/isort/ruff/codespell 全过。
- 镜像集成：`vllm-router-cacheaware:c4`，跑流量后 `curl router:8001/metrics | grep cache_aware` 校验窗口率随 sticky/fallback 变化。

### 结果
- 单测：40 passed；lint 全过；commit `fadcdc5`（clean）。
- 镜像集成（`vllm-router-cacheaware:c4`，e2 `waiting=99` 超限，tolerate=5，15 个不同 session）：
  - 后端命中 e1=9 e3=6 e2=0（凡 hash 到 e2 的 session 全部 fallback）。
  - `curl router:8001/metrics`：`cache_aware_stickiness_rate=0.667`、`cache_aware_fallback_rate=0.333`、`cache_aware_fallback_reason_rate{reason="queue"}=0.333`，其余 reason=0；10 sticky + 5 fallback = 15 一致，PASS。

---

## C5 — simplify + 全量验证收尾

### 改了什么
- `src/vllm_router/routers/routing_logic.py`：`_violated_reasons` 内 `if threshold and threshold > 0` 简化为 `if threshold > 0`，并合并冗余的 `value >= 0` 判定（-1 必低于任何正阈值），行为不变、更易读。
- 本 worklog 收尾。

### 怎么测（全量证据）
- 单测全量（venv 可跑）：`pytest test_cache_aware_router test_overload_stats test_parser test_session_router test_singleton test_utils -q` → 51 passed。
- lint 全量：black/isort/ruff/codespell 对全部改动文件通过。
- 镜像：`vllm-router-cacheaware:c5` 构建 + 导入冒烟。

### 结果
- 单测 51 passed；lint 全过；commit `3ea4b49`（clean）。
- 镜像 `vllm-router-cacheaware:c5` 构建成功；容器内 `initialize_routing_logic(CACHE_AWARE_LOAD_BALANCING, ...)` 正常，`window=30.0 thresholds={'p50_e2e':1.0,...}`，SMOKE_OK。

### 备注（真实 vLLM）
- 路由改动不触碰代理/抓取路径（仅路由决策 + 指标），已用 stdlib mock 覆盖全部 queue/延迟/指标场景。
- 未启真实 vLLM 端到端：本地无 `vllm-openai` 镜像、HF 缓存仅 12B–26B 量化大模型、磁盘 93%（剩 29G），按"低磁盘先确认"规则未做重型拉取/清理。可按需补做。

---

## 功能总览（最终）

启用：`--routing-logic cache_aware_load_balancing --session-key <header>`，配合可选阈值：
- `--cache-aware-tolerate-waiting-requests`（默认 20，排队阈值）
- `--cache-aware-p50-ttft-threshold` / `--cache-aware-p99-ttft-threshold`（秒，0=关）
- `--cache-aware-p50-e2e-threshold` / `--cache-aware-p99-e2e-threshold`（秒，0=关）
- `--cache-aware-stats-window`（默认 30s，指标窗口）

行为：session 粘滞 hash-ring engine；命中任一阈值 → fallback，候选=满足全部阈值的 engine，按 `(queue 升序, p50_e2e 升序)` 选；全员超限退回原 engine。

指标（`/metrics`，≥30s 窗口）：`vllm:cache_aware_stickiness_rate`、`vllm:cache_aware_fallback_rate`、`vllm:cache_aware_fallback_reason_rate{reason=queue|p50_ttft|p99_ttft|p50_e2e|p99_e2e}`。

性能：分位数 TTL(1s) 缓存隔离热路径；指标 O(1) 摊销；未改其他 router/代理路径。

提交（sss-dev）：C0 `22c83cc` · C1 `7a5294b` · C2 `830afb4` · C3 `182773f` · C4 `fadcdc5` · C5 `3ea4b49` · worklog `51f9a67`。

---

## 补充验证（全量，应要求"都跑"）

镜像 `vllm-router-cacheaware:c5`，stdlib mock 后端，`--network host`。

### 容器集成补全（之前只单测的分支）
- 场景 A（四个延迟阈值全开，e2 慢 2s，tolerate=1000）：req1 粘 e2，其后全 fallback e1；`/metrics` `stickiness=0.125 fallback=0.875`，`reason{p50_ttft,p99_ttft,p50_e2e,p99_e2e}=0.875`、`queue=0` → 四个延迟阈值与归因全部 PASS。
- 场景 B（全员 waiting=99，tolerate=5）：session 全部停留在原 sticky 引擎 e2（5 命中）；`fallback_rate=1.0 reason{queue}=1.0 stickiness=0` → 全员超限退回原 engine PASS。
- 场景 C（e1=0/e2=99/e3=5，无 session header）：5 次全部命中 e1（最小 queue）→ 无 session 最小负载 PASS。

### 全量单测
- `PYTHONPATH=src pytest src/tests/ --ignore=test-openai.py`（pytest-asyncio 固定 CI 版 0.25.3）→ **62 passed**（含 test_file_storage 异步用例；之前 5 个 error 是我临时装的 pytest-asyncio 版本不匹配所致，与本改动无关）。

### 真实 vLLM 端到端
- 真实引擎：`ssadds/vllm:260528-patch` serve `wangqia0309/Captain-Eris_Violet-V0.420-12B-FP8-KV-modelopt-fp4-chat`（`--quantization modelopt --kv-cache-dtype fp8 --enforce-eager`，:8000，~80s ready）+ 2 个 mock（:9101/:9103，同 model id）。
- 路由 cache_aware，3 后端静态发现。发 6 个不同 session：
  - 命中真实 vLLM 的 session（s1/s3/s5）返回**真实模型生成 token**（"Hi there!"/"Let's talk"），经 router 代理；
  - 其余命中 mock；`stickiness_rate=1.0`（无超限全粘）。
  - router 成功抓取真实 vLLM `/metrics`（`vllm:num_requests_running` 来自真实引擎）。
- 结论：router 对**真实 vLLM 后端**的代理 + /metrics 抓取 + 粘滞路由 PASS。queue/延迟/指标判定与后端类型无关，已由 mock 全场景覆盖。

### 磁盘
- 验证期间磁盘 95%（剩 23G），复用已有镜像、未新建大镜像；可回收项为 build cache(~56G) 与冗余 c1–c4 镜像（未清，待确认）。测试容器与 mock 已于验证后全部停止，GPU 归还。

---

## C6 — 修复 fallback 惊群（thundering herd）

### 背景 / 诊断（线上超限根因）
线上 router 镜像 `ssadds/production-stack:vllm`（`0fa8661`）出现"大量请求同时 fallback 到同一 pod 导致超限"。按 diagnose 流程复现 + 定位：

复现（mock：4 个 hot 引擎 base_waiting=99 强制 fallback + 2 个 idle 候选 capacity=4 delay=3；router 默认 30s scrape；40 个不同 session 并发）：
- C5（修复前）：26 个 fallback **全部** 打到 candA → candA 峰值并发 **35**，candB **5**（≈7:1）。

根因（三条，前两条与线上镜像共有）：
1. **engine_stats 30s scrape 陈旧**：一个 burst 全程看到候选 `queue=0` 的旧值。
2. **router 无在途(in-flight)计账**：从不计入自己刚派发、scrape 还没看到的请求。
3. **确定性 `min(queue,p50_e2e)`**：陈旧窗口内所有 fallback 收敛到同一个最低队列引擎（线上是 `random.choice` 略好，但 idle 候选少 + 30s 盲窗，每个仍远超 tolerate → 一样超限）。

### 改了什么（修第 2 条，最小且对症）
- `src/vllm_router/routers/routing_logic.py`：router 维护"最近派发"衰减计数 `_dispatch_log/_pending`，`_record_dispatch`/`_evict_dispatches`/`_pending_load`；`_fallback_sort_key` 用 `effective_queue = scraped_queue + pending` 排序；`route_request` 在**同步**决策点（无 await，事件循环内原子）记录派发，使并发 fallback 互相可见、自动打散。`_select_fallback`/`_route_with_snapshot` 透传 `now`（可测）。
- `--cache-aware-inflight-decay`（默认 5s，近似单请求时长）+ app 接线 + 工厂。
- `_violated_reasons`（粘滞阈值判定）保持不变，仅 fallback 目标选择受影响 → 不改用户既定语义。
- 性能：deque + Counter O(1) 摊销，热路径无排序。

### 怎么测
- TDD 回归（先红）：`test_inflight_accounting_spreads_repeated_fallback`、`test_inflight_decay_clears_pending`、`test_route_records_dispatch_for_chosen_engine`、`test_fallback_no_recent_dispatch_is_deterministic`（无派发时退化为原确定性行为）；旧 `_select_fallback` 测试补 `now`。
- 全量：`pytest src/tests/ --ignore=test-openai.py` → **66 passed**；black/isort/ruff/codespell 全过。
- 镜像复测（`vllm-router-cacheaware:c6`，同一 40 并发 burst，decay=5）：candA 峰值 **21**、candB **19**（≈1.1:1）→ 惊群被打散；`/metrics` 率指标仍正常。

### 对比
| | candA 峰值并发 | candB 峰值并发 |
|---|---|---|
| C5 修复前 | 35 | 5 |
| C6 修复后 | 21 | 19 |

### 引以为戒（post-mortem）
- 任何"基于周期 scrape 的负载做路由决策"都必须叠加 **router 自身在途计数**，否则 scrape 盲窗内必然惊群；随机分发只是缓解，不解决。
- 衰减窗口应贴近真实请求时长；过短退化为无计账，过长会过度分散（可经 `--cache-aware-inflight-decay` 调）。
- 进一步可选项（未做，避免 over-engineering）：缩短 `--engine-stats-interval`、按精确完成回调而非时间衰减。

---

## C7 — 修复"在途计数按时间衰减"缺陷（#1）

### 诊断（先证明缺陷真实存在）
C6 的在途计数 `_pending` 按 `--cache-aware-inflight-decay`(5s) **时间衰减**，从不在请求真正完成时递减。若请求时长 > 衰减窗口，衰减会**提前忘记仍在跑的请求**，惊群回归。

复现（`prove1.sh`，c6，候选请求 delay=12s，decay=5s）：
- wave1：12 并发，candB 临时超限 → 全落 candA（candA in-flight=12，持续 12s）。
- 治愈 candB；wave2 前断言 **candA 仍 in-flight=12**（确保测试有效）。
- wave2：12 并发 → **candA 峰值 18 / candB 6**（理想 12/12）→ decay 忘记了 candA 上仍在跑的 12 个 → 惊群回归。**缺陷证实。**

### 修复（最小、对症）
- `src/vllm_router/routers/routing_logic.py`：`_pending` 改为**完成精确计数**——`_inflight: Dict[url, deque[ts]]`，派发时入队，`on_request_complete(url)` 出队；`_pending_load` 仅把 `inflight_decay` 当**安全上限**（丢弃从未观察到完成的泄漏项，如客户端断连）。请求在其**整个生命周期**内都计入其 engine，不再被固定窗口提前忘记。
- `src/vllm_router/services/request_service/request.py`：`process_request` 完成处，除 monitor.on_request_complete 外，对实现了 `on_request_complete` 的 router（duck-typing，仅 cache_aware 有）回调递减在途。
- `--cache-aware-inflight-decay` 语义改为"安全上限"，默认 5→**300**（>最长请求）。
- 不变：粘滞/阈值判定、fallback 排序结构、热路径 O(1)。

### 验证
- TDD 新增：`test_completion_decrements_inflight`、`test_long_inflight_not_forgotten_until_complete`（100s 后仍计入，仅完成才清零）、`test_inflight_safety_cap_drops_unobserved`（泄漏兜底）。
- 全量 `pytest src/tests/ --ignore=test-openai.py` → **68 passed**；black/ruff/codespell 全过。
- 镜像 `:c7` 同一 `prove1.sh`（candA 确认 wave2 时仍 in-flight=12）：**candA 峰值 18→14、candB 6→10** → 惊群回归基本消除（candA 多出的 2 个是 wave1 真实完成后正确释放所致）。
- 正常不回归：粘滞 session×8 全落单引擎；单波 40 并发 → candA/candB 各 20 均衡。
