#!/usr/bin/env python3
"""Conversation-replay A/B for router routing policies against a REAL vllm fleet,
driven by the production replay log (multi-turn, shared 5k-token prefixes).

Why this and not a synthetic load: the affinity payoff of this workload is the
KV PREFIX CACHE. Every request of a conversation re-sends the same
system-prompt + history (avg ~5k tokens); if a turn lands on the engine that
already cached that prefix, prefill is near-free and TTFT is low. So the metric
that separates the policies here is TTFT (and the engines' prefix-cache hit
rate), not just end-to-end latency.

Faithful arrival model: a conversation's turns are causally ordered (turn t+1 is
a reply to turn t), so each conversation is replayed SEQUENTIALLY by one worker;
many conversations run concurrently (``--concurrency`` = simultaneously active
conversations). This reproduces the "returning session" pattern the router sees.

Dataset format: JSON Lines, one object per line:
    {"ts": <ns>, "conv_id": "<uuid>", "body": {"prompt": "<full templated ctx>"}}

Run once per policy (restart the router with a different --routing-logic, keep
this workload fixed) and compare load_balanced_affinity vs roundrobin vs session.

Example:
  python scripts/replay_conv_ab.py \
      --dataset /mnt/shared/sss/data/replay-logs-conv-avg5k.json \
      --router http://127.0.0.1:8001 --model kaon-v3 \
      --session-header X-Flow-Conversation-Id \
      --label load_balanced_affinity \
      --max-convs 3000 --concurrency 64
"""

import argparse
import json
import queue
import random
import statistics
import threading
import time
from collections import OrderedDict, defaultdict

import requests


def load_conversations(path, max_records, max_convs):
    """Stream the JSONL log into {conv_id: [prompt, ...]} in ts order.

    Records are time-ordered in the file, so reading the first ``max_records``
    lines yields a contiguous traffic window with conversations in turn order.
    ``max_convs`` caps the number of distinct conversations kept.
    """
    convs = OrderedDict()
    n = 0
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = r.get("conv_id")
            prompt = r.get("body", {}).get("prompt")
            if cid is None or prompt is None:
                continue
            if cid not in convs:
                if max_convs and len(convs) >= max_convs:
                    continue  # full on new conversations; keep filling known ones
                convs[cid] = []
            convs[cid].append(prompt)
            n += 1
            if max_records and n >= max_records:
                break
    return convs


def sample_max_tokens(rng):
    """Roleplay reply length: mostly short, occasionally long (heavy tail)."""
    x = rng.random()
    if x < 0.80:
        return rng.randint(64, 256)
    if x < 0.95:
        return rng.randint(256, 512)
    return rng.randint(512, 1024)


def scrape_engine_metrics(router):
    """Per-engine current_qps and gpu_prefix_cache_hit_rate from /metrics."""
    try:
        txt = requests.get(f"{router}/metrics", timeout=5).text
    except Exception:
        return {}, {}
    qps, hit = {}, {}
    for line in txt.splitlines():
        if line.startswith("vllm:current_qps{"):
            srv = line.split('server="', 1)[1].split('"', 1)[0]
            qps[srv] = float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:gpu_prefix_cache_hit_rate{"):
            srv = line.split('server="', 1)[1].split('"', 1)[0]
            hit[srv] = float(line.rsplit(" ", 1)[1])
    return qps, hit


def send_turn(args, conv_id, prompt, max_tokens, ttfts, e2es, codes, lock):
    """One request; stream the response to measure TTFT and end-to-end time."""
    body = {
        "model": args.model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
    }
    headers = {args.session_header: conv_id}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    t0 = time.time()
    ttft = None
    try:
        with requests.post(
            args.router + args.endpoint,
            json=body,
            headers=headers,
            stream=True,
            timeout=args.timeout,
        ) as resp:
            with lock:
                codes[resp.status_code] += 1
            if resp.status_code != 200:
                return
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if line.startswith("data: "):
                    line = line[6:]
                if line.strip() == "[DONE]":
                    break
                if ttft is None:
                    try:
                        chunk = json.loads(line)
                        txt = chunk.get("choices", [{}])[0].get("text", "")
                    except json.JSONDecodeError:
                        txt = ""
                    if txt:
                        ttft = time.time() - t0
        e2e = time.time() - t0
        with lock:
            if ttft is not None:
                ttfts.append(ttft)
            e2es.append(e2e)
    except Exception:
        with lock:
            codes["exc"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--router", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--session-header", default="X-Flow-Conversation-Id")
    ap.add_argument(
        "--api-key",
        default=None,
        help="sent as 'Authorization: Bearer <key>' (router forwards "
        "it to the engines, which require it)",
    )
    ap.add_argument("--endpoint", default="/v1/completions")
    ap.add_argument(
        "--max-records",
        type=int,
        default=30000,
        help="cap log lines loaded (0 = all ~74k)",
    )
    ap.add_argument(
        "--max-convs",
        type=int,
        default=3000,
        help="cap distinct conversations (0 = all ~14k)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=64,
        help="simultaneously active conversations (load knob)",
    )
    ap.add_argument(
        "--think-time",
        type=float,
        default=0.0,
        help="seconds to wait between a conversation's turns",
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="fixed reply length; 0 = sample a heavy-tailed length",
    )
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    print(f"loading {args.dataset} ...")
    convs = load_conversations(args.dataset, args.max_records, args.max_convs)
    turns_total = sum(len(v) for v in convs.values())
    print(
        f"loaded {len(convs)} conversations, {turns_total} turns "
        f"(mean {turns_total/max(1,len(convs)):.1f} turns/conv)"
    )

    work = queue.Queue()
    for cid in convs:
        work.put(cid)

    ttfts, e2es = [], []
    codes = defaultdict(int)
    lock = threading.Lock()

    def worker():
        while True:
            try:
                cid = work.get_nowait()
            except queue.Empty:
                return
            for i, prompt in enumerate(convs[cid]):
                mt = args.max_tokens or sample_max_tokens(rng)
                send_turn(args, cid, prompt, mt, ttfts, e2es, codes, lock)
                if args.think_time and i < len(convs[cid]) - 1:
                    time.sleep(args.think_time)

    t_start = time.time()
    threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t_start

    qps, hit = scrape_engine_metrics(args.router)

    def pct(data, p):
        s = sorted(data)
        return s[min(len(s) - 1, int(p / 100 * len(s)))] if s else 0.0

    ok = codes.get(200, 0)
    print(f"\n=== {args.label} ===")
    print(f"turns={turns_total} ok={ok} errors={turns_total - ok} codes={dict(codes)}")
    print(
        f"throughput={ok/wall:.1f} turns/s over {wall:.1f}s, concurrency={args.concurrency}"
    )
    print(
        f"TTFT(s):  p50={pct(ttfts,50):.2f} p90={pct(ttfts,90):.2f} "
        f"p95={pct(ttfts,95):.2f} p99={pct(ttfts,99):.2f}   "
        f"<- affinity/prefix-cache sensitive"
    )
    print(
        f"E2E(s):   p50={pct(e2es,50):.2f} p90={pct(e2es,90):.2f} "
        f"p95={pct(e2es,95):.2f} p99={pct(e2es,99):.2f}"
    )
    if qps:
        v = list(qps.values())
        m = statistics.mean(v)
        cv = statistics.pstdev(v) / m if m else 0
        mn, mx = min(v), max(v)
        print(
            f"per-engine QPS: n={len(v)} max/min={mx/mn if mn else float('inf'):.2f} "
            f"CV={cv:.3f}  (lower = more balanced)"
        )
    if hit:
        vals = list(hit.values())
        print(
            f"prefix-cache hit rate: mean={statistics.mean(vals):.3f} "
            f"min={min(vals):.3f} max={max(vals):.3f}  (higher = affinity paying off)"
        )


if __name__ == "__main__":
    main()
