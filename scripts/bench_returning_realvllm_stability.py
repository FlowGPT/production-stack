#!/usr/bin/env python3
"""Large-scale stability test of the returning-session funnel against a REAL
vllm backend.

Drives the router (cache_aware_load_balancing + returning-session store) with a
controlled session distribution, then verifies the funnel counters exactly match
client-side ground truth, the funnel partitions the base totals, in-flight
drains to zero, and there are no store errors.

Ground truth is computed from SUCCESSFUL requests only (retries minimise
failures): for each session with >=1 success, 1 first visit + (successes-1)
returning. Routing is synchronous per request on the router event loop, so the
first successful request of a session is its first visit regardless of client
concurrency.
"""
import argparse
import random
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

METRIC_KEYS = [
    "vllm:cache_aware_first_visit_routed_total",
    "vllm:cache_aware_first_visit_sticky_total",
    "vllm:cache_aware_first_visit_fallback_total",
    "vllm:cache_aware_returning_routed_total",
    "vllm:cache_aware_returning_sticky_total",
    "vllm:cache_aware_returning_fallback_total",
    "vllm:cache_aware_sticky_total",
    "vllm:cache_aware_fallback_total",
]


def scrape(router):
    txt = requests.get(f"{router}/metrics", timeout=5).text
    out = {}
    for line in txt.splitlines():
        if line.startswith("#"):
            continue
        for k in METRIC_KEYS:
            if line.startswith(k + " ") or line.startswith(k + "{"):
                out[k] = out.get(k, 0.0) + float(line.rsplit(" ", 1)[1])
    inflight = 0.0
    store_err = 0.0
    for line in txt.splitlines():
        if line.startswith("vllm:cache_aware_inflight_requests"):
            inflight += float(line.rsplit(" ", 1)[1])
        if line.startswith("vllm:cache_aware_returning_session_store_errors_total"):
            store_err += float(line.rsplit(" ", 1)[1])
    out["_inflight"] = inflight
    out["_store_err"] = store_err
    return out


def build_plan(n_sessions, max_visits, seed):
    rng = random.Random(seed)
    plan = []  # list of session_ids (one entry per request)
    for i in range(n_sessions):
        # Skew toward few visits so ~one-shots dominate (realistic).
        visits = rng.choices(
            range(1, max_visits + 1),
            weights=[max_visits - v + 1 for v in range(1, max_visits + 1)],
        )[0]
        plan += [f"sess-{i}"] * visits
    rng.shuffle(plan)
    return plan


def send(router, model, sid, retries=4):
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{router}/v1/completions",
                headers={"x-session-id": sid},
                json={"model": model, "prompt": "hi", "max_tokens": 1, "temperature": 0},
                timeout=30,
            )
            if r.status_code == 200:
                return sid, True
        except Exception:
            pass
        time.sleep(0.05 * (attempt + 1))
    return sid, False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--router", default="http://127.0.0.1:18080")
    p.add_argument("--model", default="RedHatAI/gemma-4-31B-it-NVFP4")
    p.add_argument("--redis-url", default="redis://127.0.0.1:16399/0")
    p.add_argument("--sessions", type=int, default=6000)
    p.add_argument("--max-visits", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=64)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--prefix", default="sess", help="session id namespace")
    args = p.parse_args()

    plan = build_plan(args.sessions, args.max_visits, args.seed)
    plan = [s.replace("sess-", args.prefix + "-") for s in plan]
    total = len(plan)
    print(f"Plan: {args.sessions:,} sessions, {total:,} requests "
          f"(concurrency={args.concurrency})")

    # warmup
    send(args.router, args.model, "warmup")
    before = scrape(args.router)

    ok_per_session = defaultdict(int)
    lock = threading.Lock()
    errors = 0
    lat = []
    t0 = time.time()

    def work(sid):
        nonlocal errors
        s = time.time()
        _, ok = send(args.router, args.model, sid)
        d = time.time() - s
        with lock:
            if ok:
                ok_per_session[sid] += 1
                lat.append(d)
            else:
                errors += 1

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for i, _ in enumerate(ex.map(work, plan)):
            if (i + 1) % 5000 == 0:
                print(f"  {i + 1:,}/{total:,} sent  ({(i+1)/(time.time()-t0):.0f} req/s)")
    wall = time.time() - t0

    # Ground truth from successful requests.
    gt_first = len(ok_per_session)
    gt_total_ok = sum(ok_per_session.values())
    gt_returning = gt_total_ok - gt_first

    # Wait for in-flight to drain.
    for _ in range(30):
        after = scrape(args.router)
        if after["_inflight"] <= 0:
            break
        time.sleep(1)

    def d(k):
        return int(after.get(k, 0) - before.get(k, 0))

    fv = d("vllm:cache_aware_first_visit_routed_total")
    ret = d("vllm:cache_aware_returning_routed_total")
    sticky = d("vllm:cache_aware_sticky_total")
    fb = d("vllm:cache_aware_fallback_total")
    fv_s = d("vllm:cache_aware_first_visit_sticky_total")
    fv_f = d("vllm:cache_aware_first_visit_fallback_total")
    ret_s = d("vllm:cache_aware_returning_sticky_total")
    ret_f = d("vllm:cache_aware_returning_fallback_total")

    print("\n=== Results ===")
    print(f"  requests sent      : {total:,}")
    print(f"  successful         : {gt_total_ok:,}")
    print(f"  HTTP errors        : {errors}")
    print(f"  throughput         : {gt_total_ok / wall:,.0f} req/s  ({wall:.1f}s)")
    if lat:
        lat.sort()
        print(f"  latency p50/p99    : {lat[len(lat)//2]*1000:.0f} / "
              f"{lat[int(len(lat)*0.99)]*1000:.0f} ms")
    print(f"\n  ground truth       : first={gt_first:,}  returning={gt_returning:,}")
    print(f"  metric counters    : first={fv:,}  returning={ret:,}")
    print(f"  sticky/fallback    : {sticky:,} / {fb:,}  (first {fv_s}/{fv_f}, "
          f"returning {ret_s}/{ret_f})")
    print(f"  inflight (drained) : {after['_inflight']:.0f}")
    print(f"  store errors       : {after['_store_err']:.0f}")

    fails = []
    if fv != gt_first:
        fails.append(f"first_visit {fv} != ground truth {gt_first}")
    if ret != gt_returning:
        fails.append(f"returning {ret} != ground truth {gt_returning}")
    if fv + ret != sticky + fb:
        fails.append(f"funnel {fv+ret} != base total {sticky+fb} (invariant)")
    if fv_s + ret_s != sticky:
        fails.append(f"sticky partition {fv_s+ret_s} != {sticky}")
    if fv_f + ret_f != fb:
        fails.append(f"fallback partition {fv_f+ret_f} != {fb}")
    if after["_inflight"] > 0:
        fails.append(f"inflight did not drain: {after['_inflight']}")
    if after["_store_err"] > 0:
        fails.append(f"store errors: {after['_store_err']}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nPASS: funnel counts == ground truth, partitions exact, "
          "inflight drained, no store errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
