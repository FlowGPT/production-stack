# Router multi-worker (`--router-workers`)

## Why
The router proxies every request **and streams every response token** through a
single asyncio event loop, so one router process is CPU-bound at ~1 core. Under
heavy concurrent streaming it saturates (measured ~20–28 req/s per loop), which
both caps throughput and starves `/health` (liveness can then kill a busy-but-
healthy pod). Throughput is bounded by **router event loops, not GPUs**.

`--router-workers N` runs N uvicorn worker processes (N event loops) in one pod,
giving near-linear throughput across N cores.

## Measured (local repro, 40 mock engines, 400 concurrent streams)

| Setup | Throughput | e2e p95 | /health max |
| --- | --- | --- | --- |
| direct to engines (no router) | 293/s | 1.8s | — |
| 1 worker (single loop) | 28/s | 20s | 12.8s |
| 4 workers (1 pod) | 186–212/s | 3–5s | 4.4s |
| 8 workers (1 pod) | 308/s | 2.1s | **10ms** |

8 workers used ~6 cores under that load (each busy worker ≈ 1 core).

## Usage
```
vllm-router ... --router-workers 8
```
Default is `1` (unchanged single-process behavior).

## How it works
- `N=1`: unchanged — `main()` initializes in-process and runs `uvicorn.run(app)`.
- `N>1`: `main()` serializes the parsed args to `VLLM_ROUTER_INIT_ARGS` and runs
  `uvicorn.run("vllm_router.app:app", workers=N)`. Each worker is a fresh process
  that re-initializes itself in the FastAPI lifespan from that env var (a worker
  that starts without it fails hard rather than serving uninitialized).
- Prometheus **multiprocess** mode is enabled automatically for `N>1`
  (`PROMETHEUS_MULTIPROC_DIR`); `/metrics` aggregates all workers so `*_total`
  counters sum correctly (verified: 800 requests → counters report 800, not
  per-worker slices).

## Caveats (multi-worker = "pod-internal replicas")
1. **Per-worker state.** In-flight accounting and the sliding-window rate gauges
   are per worker (like extra replicas). Overload fallback (`engine_max_concurrency`)
   is per worker, not pod-global.
2. **Returning-session metrics need Redis.** With the default `memory` store each
   worker recognizes only its own sessions → returning metrics undercount. Use
   `--cache-aware-returning-session-store redis` so workers share state.
3. **Gauges are representative, not summed.** Under multiprocess each gauge uses
   `mostrecent`; for cluster-accurate rates use the `*_total` counters with
   `rate()`, not the window-rate gauges.
4. **N× background work.** Each worker runs its own engine-stats scraper and (k8s)
   service-discovery watcher: N workers ⇒ ~N× `/metrics` scrape load on engines
   and N watch streams per pod. Raise `--engine-stats-interval` (e.g. 10–15s).
5. **Size CPU to worker count.** Each busy worker needs ~1 core. Give the pod a
   CPU request/limit ≈ N (e.g. 8 workers ⇒ ~8 cores); a smaller limit throttles
   the workers and negates the gain. Memory also scales ~linearly with N.

## Sizing
Pick total loops for the target QPS (≈20–25 req/s per loop under heavy streaming;
e.g. ~7–8 loops for 150 QPS), then split across pods/workers with CPU ≈ loops:
e.g. `1 pod × 8 workers` (~8 cores) or `2 pods × 4 workers` (~4 cores each).
