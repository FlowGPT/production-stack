#!/usr/bin/env python3
"""Quantify the per-request overhead of the returning-session store backends.

Measures the wall time of a single ``visit()`` call (the only work this feature
adds to the router hot path) for the memory and redis backends, over a realistic
mix of new + returning sessions, single-threaded (matching the asyncio
event-loop call pattern). Reports mean / p50 / p90 / p99 / max and throughput.
"""
import statistics
import sys
import time

from vllm_router.routers.returning_session_store import (
    MemoryReturningSessionStore,
    RedisReturningSessionStore,
)

N = 50000
TTL = 1800.0
# ~30% of visits reuse an earlier id so both EXISTS=0 and EXISTS=1 paths run.
UNIQUE = int(N * 0.7)


def _ids(n):
    return [f"sess-{i % UNIQUE}" for i in range(n)]


def bench(store, label):
    ids = _ids(N)
    now = time.time()
    # warmup
    for sid in ids[:2000]:
        store.visit(sid, now, TTL)
    lat = []
    t0 = time.perf_counter()
    for sid in ids:
        s = time.perf_counter()
        store.visit(sid, now, TTL)
        lat.append((time.perf_counter() - s) * 1e6)  # microseconds
    wall = time.perf_counter() - t0
    lat.sort()

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    print(f"\n=== {label} (n={N}) ===")
    print(f"  throughput : {N / wall:,.0f} visits/sec")
    print(f"  mean       : {statistics.mean(lat):8.2f} us")
    print(f"  p50        : {pct(0.50):8.2f} us")
    print(f"  p90        : {pct(0.90):8.2f} us")
    print(f"  p99        : {pct(0.99):8.2f} us")
    print(f"  max        : {lat[-1]:8.2f} us")


def main():
    redis_url = sys.argv[1] if len(sys.argv) > 1 else "redis://127.0.0.1:16399/0"
    bench(MemoryReturningSessionStore(), "memory store")
    store = RedisReturningSessionStore(redis_url, key_prefix="bench:", socket_timeout=0.05)
    bench(store, f"redis store (loopback {redis_url})")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
