"""Quantify the circuit breaker's effect during a redis outage.

Models a TCP-blackholed redis (every call blocks the full socket_timeout, then
raises). Replays the same request stream twice against RedisAffinityStore -- once
with the breaker disabled (threshold=0), once enabled -- and reports the wall
time that lands on the event-loop thread (== added scheduling lag) plus the
number of failed redis calls (== store_errors_total increment).

Run:
    PYTHONPATH=src python scripts/mock_breaker_outage.py
"""

import time
from collections import OrderedDict

from vllm_router.routers.affinity_store import RedisAffinityStore

SOCKET_TIMEOUT = 0.05  # matches --lb-affinity-redis-timeout default
QPS = 40.0             # per-worker request rate during the outage
OUTAGE_SECONDS = 5.0   # simulated timeline length
COOLDOWN = 5.0
THRESHOLD = 3


class BlackholeClient:
    """Every op blocks for socket_timeout then raises, like a blackholed redis."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.calls = 0

    def _stall(self):
        self.calls += 1
        time.sleep(self.timeout)  # this is what blocks the single loop thread
        raise RuntimeError("timeout")

    def get(self, *a, **k):
        self._stall()

    def set(self, *a, **k):
        self._stall()

    def close(self):
        pass


def _store(client, breaker_threshold: int) -> RedisAffinityStore:
    s = RedisAffinityStore.__new__(RedisAffinityStore)
    s._key_prefix = "vllm:lb-affinity:"
    s._client = client
    s._refresh_fraction = 0.5
    s._write_cache_size = 100_000
    s._writes = OrderedDict()
    s._breaker_threshold = breaker_threshold
    s._breaker_cooldown = COOLDOWN
    s._consec_failures = 0
    s._breaker_open_until = 0.0
    return s


def run(breaker_threshold: int):
    client = BlackholeClient(SOCKET_TIMEOUT)
    store = _store(client, breaker_threshold)

    n_requests = int(QPS * OUTAGE_SECONDS)
    dt = 1.0 / QPS
    blocking = 0.0  # real time spent inside store calls == loop lag added

    for i in range(n_requests):
        now = 1000.0 + i * dt  # simulated timeline the breaker reasons about
        sid = f"sess-{i}"      # fresh session -> hot path does GET then SET
        t0 = time.perf_counter()
        store.get(sid, now=now, ttl=3600.0)
        store.put(sid, "http://engine-a:8000", now=now, ttl=3600.0)
        blocking += time.perf_counter() - t0

    return client.calls, blocking, n_requests


def main():
    print(
        f"outage sim: timeout={SOCKET_TIMEOUT}s qps={QPS} "
        f"duration={OUTAGE_SECONDS}s threshold={THRESHOLD} cooldown={COOLDOWN}s\n"
    )

    off_calls, off_block, n = run(breaker_threshold=0)
    on_calls, on_block, _ = run(breaker_threshold=THRESHOLD)

    print(f"requests replayed:            {n}")
    print("-" * 56)
    print("breaker OFF (pre-fix, v1.3.1 report baseline):")
    print(f"  redis calls that blocked+failed: {off_calls}")
    print(f"  store_errors_total increment:    {off_calls}  (~+1 per request)")
    print(f"  loop blocking time:              {off_block:.3f}s")
    print("-" * 56)
    print("breaker ON (this image):")
    print(f"  redis calls that blocked+failed: {on_calls}")
    print(f"  store_errors_total increment:    {on_calls}")
    print(f"  loop blocking time:              {on_block:.3f}s")
    print("-" * 56)
    expected_probes = 1 + int(OUTAGE_SECONDS / COOLDOWN)  # open once + 1 probe/cooldown
    print(
        f"expected calls with breaker ~= threshold + probes = "
        f"{THRESHOLD} + {expected_probes} = {THRESHOLD + expected_probes}"
    )
    print(
        f"lag reduction: {off_block:.3f}s -> {on_block:.3f}s "
        f"({(1 - on_block / off_block) * 100:.1f}% less loop blocking)"
    )
    print(
        f"errors flattened: {off_calls} -> {on_calls} "
        f"({on_calls / OUTAGE_SECONDS:.1f}/s vs {off_calls / OUTAGE_SECONDS:.1f}/s)"
    )


if __name__ == "__main__":
    main()
