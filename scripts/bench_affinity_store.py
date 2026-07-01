#!/usr/bin/env python3
"""Quantify the per-request overhead of the load_balanced_affinity stores.

Separates two costs:
  1. Router-side CPU (key hashing + wrapper) -- memory store & redis-CPU.
  2. Redis network RTT -- redis store against a real redis (localhost here;
     cross-node adds its own RTT on top).

Also measures the store cost as a fraction of a full _choose() routing decision
(P2C + effective_load + affinity get/put), which is what actually runs per
request. Keys use a short TTL so they self-expire; nothing is deleted.
"""

import os
import statistics
import time
import uuid

from vllm_router.routers.affinity_store import (
    MemoryAffinityStore,
    RedisAffinityStore,
    create_affinity_store,
)
from vllm_router.routers.routing_logic import LoadBalancedAffinityRouter
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.utils import SingletonABCMeta

# Point at the real (cross-node) redis to measure true RTT, e.g.
# BENCH_REDIS_URL=redis://10.0.0.5:6379/0. Defaults to localhost.
REDIS_URL = os.environ.get("BENCH_REDIS_URL", "redis://127.0.0.1:6379/0")
TTL = 60.0
N = 20000
N_ENGINES = 8


class FakeEndpoint:
    def __init__(self, url):
        self.url = url


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def report(name, samples_us):
    mean = statistics.mean(samples_us)
    print(
        f"{name:<34} mean={mean:7.2f}us  p50={pct(samples_us,50):7.2f}  "
        f"p95={pct(samples_us,95):7.2f}  p99={pct(samples_us,99):7.2f}"
    )


def bench_store(name, store):
    # Returning-session pattern: get (hit) + put (refresh), the hot path.
    sid = "bench-" + uuid.uuid4().hex
    store.put(sid, "http://e0", time.time(), TTL)
    samples = []
    for _ in range(N):
        t = time.perf_counter()
        store.get(sid, time.time(), TTL)
        store.put(sid, "http://e0", time.time(), TTL)
        samples.append((time.perf_counter() - t) * 1e6)
    report(name, samples)
    return statistics.mean(samples)


def bench_raw_redis():
    import redis

    c = redis.Redis.from_url(REDIS_URL, socket_timeout=0.5)
    c.set("bench:raw", "v", ex=int(TTL))
    samples = []
    for _ in range(N):
        t = time.perf_counter()
        c.get("bench:raw")
        samples.append((time.perf_counter() - t) * 1e6)
    report("raw redis GET (1 RTT)", samples)
    c.close()


def _fresh_router(**kw):
    SingletonABCMeta._instances.pop(LoadBalancedAffinityRouter, None)
    return LoadBalancedAffinityRouter(**kw)


def bench_choose(name, store):
    r = _fresh_router(
        d_choices=2, affinity=True, session_key="sid", affinity_store=store
    )
    urls = [f"http://e{i}" for i in range(N_ENGINES)]
    eps = [FakeEndpoint(u) for u in urls]
    es = {
        u: EngineStats(num_running_requests=i, num_queuing_requests=0)
        for i, u in enumerate(urls)
    }
    req = FakeRequest({"sid": "bench-choose"})
    r.route_request(eps, es, {}, req)  # prime
    samples = []
    for _ in range(N):
        t = time.perf_counter()
        r.route_request(eps, es, {}, req)
        r.release_inflight(urls[0])
        samples.append((time.perf_counter() - t) * 1e6)
    report(name, samples)


def main():
    print(f"iterations={N}, engines={N_ENGINES}, redis={REDIS_URL}\n")
    print("== store op cost: get + put per returning request ==")
    mem = bench_store("memory store (get+put)", MemoryAffinityStore())
    red = bench_store(
        "redis store (get+put, 2 RTT)",
        create_affinity_store("redis", redis_url=REDIS_URL, redis_timeout=0.5),
    )
    bench_raw_redis()
    print(f"\n  redis adds ~{red - mem:.1f}us per request over memory ({REDIS_URL})")

    print("\n== full routing decision _choose() (8 engines) ==")
    bench_choose("_choose w/ memory store", MemoryAffinityStore())
    bench_choose(
        "_choose w/ redis store",
        create_affinity_store("redis", redis_url=REDIS_URL, redis_timeout=0.5),
    )


if __name__ == "__main__":
    main()
