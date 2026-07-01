#!/usr/bin/env python3
"""A/B latency + balance benchmark for router routing policies against a REAL
vllm fleet.

Sends a session-aware, heavy-tailed (variable output length) workload at the
router and reports client-side latency percentiles, error rate, and per-engine
QPS balance scraped from the router's /metrics. Run it once per routing policy
(restart the router with a different --routing-logic, keep this workload fixed)
and compare:

  load_balanced_affinity  vs  roundrobin  vs  session

Expected: load_balanced_affinity has the lowest p95/p99 and a per-engine QPS
spread close to roundrobin's and much tighter than session (consistent hash).

Example:
  python scripts/bench_routing_ab.py --router http://127.0.0.1:8001 \
      --model kaon-v3 --label load_balanced_affinity \
      --requests 6000 --concurrency 64 --sessions 1500 --returning-frac 0.3
"""

import argparse
import random
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests


def build_plan(
    rng, n_requests, n_sessions, returning_frac, whale_frac, small_range, whale_range
):
    n_ret = max(1, int(n_sessions * returning_frac))
    returning = [f"r{i}" for i in range(n_ret)]
    plan = []
    one_shot = 0
    for _ in range(n_requests):
        if rng.random() < returning_frac:
            sid = rng.choice(returning)
        else:
            sid = f"o{one_shot}"
            one_shot += 1
        if rng.random() < whale_frac:
            mt = rng.randint(*whale_range)
        else:
            mt = rng.randint(*small_range)
        plan.append((sid, mt))
    rng.shuffle(plan)
    return plan


def scrape_qps(router):
    """Per-engine vllm:current_qps from the router /metrics."""
    try:
        txt = requests.get(f"{router}/metrics", timeout=5).text
    except Exception:
        return {}
    qps = {}
    for line in txt.splitlines():
        if line.startswith("vllm:current_qps{"):
            server = line.split('server="', 1)[1].split('"', 1)[0]
            qps[server] = float(line.rsplit(" ", 1)[1])
    return qps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--router", required=True, help="e.g. http://127.0.0.1:8001")
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", default="run", help="routing policy under test")
    ap.add_argument("--session-header", default="x-session-id")
    ap.add_argument("--endpoint", default="/v1/completions")
    ap.add_argument("--requests", type=int, default=6000)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--sessions", type=int, default=1500)
    ap.add_argument("--returning-frac", type=float, default=0.3)
    ap.add_argument("--whale-frac", type=float, default=0.05)
    ap.add_argument("--small-min", type=int, default=32)
    ap.add_argument("--small-max", type=int, default=256)
    ap.add_argument("--whale-min", type=int, default=1024)
    ap.add_argument("--whale-max", type=int, default=2048)
    ap.add_argument("--prompt", default="Summarize the history of computing.")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    plan = build_plan(
        rng,
        args.requests,
        args.sessions,
        args.returning_frac,
        args.whale_frac,
        (args.small_min, args.small_max),
        (args.whale_min, args.whale_max),
    )

    lat, codes = [], Counter()

    def one(item):
        sid, mt = item
        body = {
            "model": args.model,
            "prompt": args.prompt,
            "max_tokens": mt,
            "temperature": 0.0,
        }
        t0 = time.time()
        try:
            r = requests.post(
                args.router + args.endpoint,
                json=body,
                headers={args.session_header: sid},
                timeout=600,
            )
            codes[r.status_code] += 1
            if r.status_code == 200:
                return time.time() - t0
        except Exception:
            codes["exc"] += 1
        return None

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for res in ex.map(one, plan):
            if res is not None:
                lat.append(res)
    wall = time.time() - t_start

    qps = scrape_qps(args.router)
    s = sorted(lat)

    def pct(p):
        return s[min(len(s) - 1, int(p / 100 * len(s)))] if s else 0.0

    ok = codes.get(200, 0)
    print(f"\n=== {args.label} ===")
    print(
        f"requests={args.requests} ok={ok} errors={args.requests - ok} "
        f"codes={dict(codes)}"
    )
    print(f"throughput={ok / wall:.1f} req/s over {wall:.1f}s")
    print(
        f"latency(s): p50={pct(50):.2f} p90={pct(90):.2f} p95={pct(95):.2f} "
        f"p99={pct(99):.2f} mean={statistics.mean(lat) if lat else 0:.2f}"
    )
    if qps:
        vals = list(qps.values())
        mn, mx = min(vals), max(vals)
        cv = (
            statistics.pstdev(vals) / statistics.mean(vals)
            if statistics.mean(vals)
            else 0
        )
        print(
            f"per-engine QPS: n={len(vals)} max/min={mx / mn if mn else float('inf'):.2f} "
            f"CV={cv:.3f}  (lower = more balanced)"
        )
    else:
        print("per-engine QPS: (router /metrics had no current_qps yet)")


if __name__ == "__main__":
    main()
