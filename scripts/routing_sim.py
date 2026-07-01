#!/usr/bin/env python3
"""Discrete-event queueing simulation comparing router policies on an IDENTICAL
workload.

Goal: show that ``load_balanced_affinity`` (power-of-two-choices) beats
``roundrobin`` on tail latency and beats ``session`` (consistent hashing) on
both per-engine balance and tail latency.

It drives the REAL router classes from vllm_router so the routing decisions are
exactly the shipped logic -- only the engines and the clock are simulated.

Engine model: each engine runs up to ``capacity`` requests concurrently
(continuous batching); extra requests wait FIFO. A request of ``work`` output
tokens admitted while ``a`` requests are active takes
``work * token_time * (1 + alpha*(a-1))`` seconds -- per-token time grows with
the running batch (GPU contention), which is what turns load imbalance into
super-linear (tail) latency. Latency includes queue wait.

Why variable work matters: with identical request sizes round-robin is already
optimally balanced. Real traffic has heavy-tailed output lengths, so a blind
round-robin keeps sending into an engine that just drew a whale; a load-aware
policy avoids that engine. The workload below is heavy-tailed on purpose.
"""

from __future__ import annotations

import argparse
import heapq
import random
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from vllm_router.routers.routing_logic import (
    LoadBalancedAffinityRouter,
    RoundRobinRouter,
    SessionRouter,
)
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.utils import SingletonABCMeta


class Endpoint:
    def __init__(self, url: str):
        self.url = url
        self.model_names = ["mock"]


class Req:
    def __init__(self, headers: Dict[str, str]):
        self.headers = headers


@dataclass
class Arrival:
    t: float
    session_id: str
    work: int


@dataclass
class Engine:
    url: str
    capacity: int
    active: int = 0
    queue: List = field(default_factory=list)  # list of (req_idx, work)
    handled: int = 0


def make_workload(
    rng: random.Random,
    n_requests: int,
    rate: float,
    n_sessions: int,
    returning_frac: float,
    whale_frac: float,
    small_range,
    whale_range,
) -> List[Arrival]:
    """Poisson arrivals; heavy-tailed work; a fraction of multi-request sessions."""
    # Decide which sessions are "returning" (issue several requests) vs one-shot.
    n_returning = max(1, int(n_sessions * returning_frac))
    returning_sessions = [f"r{i}" for i in range(n_returning)]
    arrivals: List[Arrival] = []
    t = 0.0
    one_shot_idx = 0
    for _ in range(n_requests):
        t += rng.expovariate(rate)
        if rng.random() < returning_frac:
            sid = rng.choice(returning_sessions)  # a repeat customer
        else:
            sid = f"o{one_shot_idx}"  # brand-new one-shot session
            one_shot_idx += 1
        if rng.random() < whale_frac:
            work = rng.randint(*whale_range)
        else:
            work = rng.randint(*small_range)
        arrivals.append(Arrival(t, sid, work))
    return arrivals


def build_router(mode: str):
    for cls in (RoundRobinRouter, SessionRouter, LoadBalancedAffinityRouter):
        SingletonABCMeta._instances.pop(cls, None)
    if mode == "roundrobin":
        return RoundRobinRouter()
    if mode == "session":
        return SessionRouter(session_key="sid")
    if mode == "load_balanced_affinity":
        return LoadBalancedAffinityRouter(session_key="sid", d_choices=2, affinity=True)
    if mode == "load_balanced_noaffinity":
        return LoadBalancedAffinityRouter(session_key="sid", affinity=False)
    raise ValueError(mode)


def simulate(
    mode: str,
    arrivals: List[Arrival],
    n_engines: int,
    capacity: int,
    token_time: float,
    alpha: float,
    small_work_max: int,
) -> dict:
    router = build_router(mode)
    engines = {
        f"http://e{i}": Engine(f"http://e{i}", capacity) for i in range(n_engines)
    }
    endpoints = [Endpoint(u) for u in engines]
    url_list = list(engines)

    latencies: List[float] = []
    slowdowns: List[float] = []
    small_lat: List[float] = []  # latency of small (non-whale) requests only
    arr_time: Dict[int, float] = {}
    # Event heap: (time, seq, kind, payload). kind: "arr" | "done".
    seq = 0
    heap = []
    for idx, a in enumerate(arrivals):
        heapq.heappush(heap, (a.t, seq, "arr", idx))
        seq += 1

    def engine_stats() -> Dict[str, EngineStats]:
        return {
            u: EngineStats(
                num_running_requests=e.active, num_queuing_requests=len(e.queue)
            )
            for u, e in engines.items()
        }

    def start_service(e: Engine, idx: int, work: int, now: float):
        nonlocal seq
        e.active += 1
        svc = work * token_time * (1.0 + alpha * (e.active - 1))
        heapq.heappush(heap, (now + svc, seq, "done", (e.url, idx)))
        seq += 1

    while heap:
        now, _, kind, payload = heapq.heappop(heap)
        if kind == "arr":
            idx = payload
            a = arrivals[idx]
            arr_time[idx] = now
            chosen = router.route_request(
                endpoints, engine_stats(), {}, Req({"sid": a.session_id})
            )
            e = engines[chosen]
            if e.active < e.capacity:
                start_service(e, idx, a.work, now)
            else:
                e.queue.append((idx, a.work))
        else:  # done
            url, idx = payload
            e = engines[url]
            e.active -= 1
            e.handled += 1
            lat = now - arr_time[idx]
            work = arrivals[idx].work
            ideal = work * token_time  # service time alone, no contention/queue
            latencies.append(lat)
            slowdowns.append(lat / ideal)
            if work <= small_work_max:
                small_lat.append(lat)
            router.release_inflight(url)
            if e.queue:
                nidx, nwork = e.queue.pop(0)
                start_service(e, nidx, nwork, now)

    counts = [e.handled for e in engines.values()]

    def pct(data, p):
        if not data:
            return 0.0
        s = sorted(data)
        k = min(len(s) - 1, int(p / 100.0 * len(s)))
        return s[k]

    mean_c = statistics.mean(counts)
    cv = (statistics.pstdev(counts) / mean_c) if mean_c else 0.0
    return {
        "mode": mode,
        "count_maxmin": (max(counts) / min(counts)) if min(counts) else float("inf"),
        "count_cv": cv,
        # Slowdown = actual / ideal service time: isolates the routing-controlled
        # degradation (queue wait + batch contention) from intrinsic work size.
        "sd_p50": pct(slowdowns, 50),
        "sd_p95": pct(slowdowns, 95),
        "sd_p99": pct(slowdowns, 99),
        # Tail latency of small requests -- the ones that suffer most when blindly
        # placed onto an engine already chewing a whale.
        "small_p99": pct(small_lat, 99),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", type=int, default=8)
    ap.add_argument("--capacity", type=int, default=8, help="per-engine max-num-seqs")
    ap.add_argument("--requests", type=int, default=40000)
    ap.add_argument("--sessions", type=int, default=12000)
    ap.add_argument("--returning-frac", type=float, default=0.2)
    ap.add_argument("--whale-frac", type=float, default=0.05)
    ap.add_argument("--token-time", type=float, default=0.02, help="sec/token base")
    ap.add_argument("--alpha", type=float, default=0.1, help="batch contention slope")
    ap.add_argument("--utilization", type=float, default=0.85)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    small_range = (20, 150)
    whale_range = (800, 2000)
    mean_small = sum(small_range) / 2
    mean_whale = sum(whale_range) / 2
    mean_work = (1 - args.whale_frac) * mean_small + args.whale_frac * mean_whale
    # Per-engine token throughput at full batch; rate set to hit target utilization.
    eng_tok_per_s = args.capacity / (
        args.token_time * (1 + args.alpha * (args.capacity - 1))
    )
    fleet_tok_per_s = eng_tok_per_s * args.engines
    rate = args.utilization * fleet_tok_per_s / mean_work

    arrivals = make_workload(
        rng,
        args.requests,
        rate,
        args.sessions,
        args.returning_frac,
        args.whale_frac,
        small_range,
        whale_range,
    )

    print(
        f"workload: {args.requests} reqs, {args.engines} engines x cap {args.capacity}, "
        f"target util {args.utilization:.0%}, arrival rate {rate:.1f}/s, "
        f"mean_work {mean_work:.0f} tok, whale_frac {args.whale_frac:.0%}, "
        f"returning_frac {args.returning_frac:.0%}"
    )
    print(
        "metrics: count max/min & CV = per-engine request-balance; "
        "slowdown = latency/ideal (routing-controlled queue+contention); "
        "small_p99 = p99 latency of non-whale requests"
    )
    header = (
        f"{'mode':<26} {'cnt_max/min':>11} {'cnt_CV':>7} "
        f"{'sd_p50':>7} {'sd_p95':>7} {'sd_p99':>7} {'small_p99(s)':>13}"
    )
    print(header)
    print("-" * len(header))
    for mode in (
        "session",
        "roundrobin",
        "load_balanced_noaffinity",
        "load_balanced_affinity",
    ):
        r = simulate(
            mode,
            arrivals,
            args.engines,
            args.capacity,
            args.token_time,
            args.alpha,
            small_range[1],
        )
        print(
            f"{r['mode']:<26} {r['count_maxmin']:>11.2f} {r['count_cv']:>7.3f} "
            f"{r['sd_p50']:>7.2f} {r['sd_p95']:>7.2f} {r['sd_p99']:>7.2f} "
            f"{r['small_p99']:>13.2f}"
        )


if __name__ == "__main__":
    main()
