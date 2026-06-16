# v1.2 更新日志 — Router 多 worker

> 镜像：`ssadds/production-stack-router:v1.2`
> 构建：`INSTALL_OPTIONAL_DEP=semantic_cache,redis`（与 v1.0/v1.1 一致，**不含 lmcache**）
> 在 v1.1 基础上，新增**多 worker** 以突破单事件循环吞吐瓶颈。默认行为不变。

## 背景
Router 把每个请求和**响应的每个 token** 都过同一个 asyncio 事件循环，单进程因此被钉在 ~1 个核（重流式负载下 ~20–28 req/s），既限吞吐，也会在高负载下饿死 `/health`（liveness 误杀）。

## 新增
- **`--router-workers N`（默认 1）**：一个 pod 内起 N 个 uvicorn worker（N 个事件循环 → N 个核），近线性提升吞吐。`N=1` 路径与之前完全一致（零回归）。
- **Prometheus 多进程指标**：`N>1` 时自动启用（`PROMETHEUS_MULTIPROC_DIR`），`/metrics` 跨 worker 聚合，`*_total` 计数器正确求和（已验证：800 请求 → 计数 800，而非单 worker 分片）。
- worker 缺初始化参数时 **fail-hard**；`--router-workers` 加校验（≥1）。

## 实测
- 复现栈（40 mock，400 并发）：1 worker **28 req/s** / p95 20s / `/health` max 12.8s → 8 worker(1 pod) **308 req/s** / p95 2.1s / `/health` max **10ms**。
- 真 vllm（gemma-31B，5k 输入/200 输出）：经 router 的**单请求转发开销可忽略**——TTFT 仅 +~15ms，e2e 持平。

## 升级与配置

### 保持单 worker（默认）
**无需任何额外配置**，直接换镜像 tag 到 `v1.2` 即可，行为与 v1.1 完全一致。

### 启用多 worker（`--router-workers N>1`）需要的额外配置
| 配置 | 必需 | 说明 |
| --- | --- | --- |
| `--router-workers N` | 是 | N 个事件循环；默认 1 |
| **CPU request/limit ≈ N** | **是** | 每个忙 worker ~1 核；CPU 给不够会被限流 = 白开 |
| `--cache-aware-returning-session-store redis` + `--cache-aware-returning-session-redis-url ...` | **是** | 多 worker 下各 worker 内存状态独立，回访指标会偏低；必须用 redis 共享（实测：memory 下 returning 失真，redis 下精确）|
| `--engine-stats-interval 10~15` | 建议 | 每 worker 各跑一套引擎抓取，N worker = N× 抓取压力 |
| 内存 request/limit | 注意 | 随 N 近线性增长（每 worker 一份进程内存）|
| `/tmp` 可写 | 注意 | N>1 自动用 `/tmp/vllm_router_prometheus_multiproc` 做多进程指标聚合；**若 pod 设了 `readOnlyRootFilesystem`，需给 `/tmp` 挂一个 `emptyDir`** |
| liveness 探针放宽 | 建议 | `timeoutSeconds: 5` / `failureThreshold: 5` / `periodSeconds: 10`（默认 timeout=1s 会在忙时误杀）|

示例 `extraArgs`（8 worker）：
```yaml
extraArgs:
  - "--router-workers"
  - "8"
  - "--cache-aware-returning-session-store"
  - "redis"
  - "--cache-aware-returning-session-redis-url"
  - "redis://<your-redis>:6379/0"
  - "--engine-stats-interval"
  - "10"
resources:
  requests: { cpu: "8",  memory: "4Gi" }
  limits:   { cpu: "10", memory: "8Gi" }
```

> 指标：窗口 gauge 在多进程下取 `mostrecent`（代表值），集群级速率请用 `*_total` 计数器的 `rate()`。完整说明见 [router-multi-worker.md](router-multi-worker.md)。

## 容量参考
单 loop ≈ 1 核；按目标 QPS × (输出 token / 200) 估 loop 数，CPU 同比给够。生产建议多 pod × 多 worker（如 2 pod × 8 worker）兼顾吞吐与容错；最终 worker 数请在真实引擎上 canary 校准（本地单引擎只验证了单请求开销可忽略，未验证 160 QPS 并发规模）。
