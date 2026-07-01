"""Unit tests for LoadBalancedAffinityRouter (P2C placement + self-shedding
affinity + dead-engine guard)."""

import random
import time

from vllm_router.routers.routing_logic import LoadBalancedAffinityRouter
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.utils import SingletonABCMeta


class FakeEndpoint:
    def __init__(self, url: str):
        self.url = url


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def _fresh_router(**kwargs) -> LoadBalancedAffinityRouter:
    SingletonABCMeta._instances.pop(LoadBalancedAffinityRouter, None)
    return LoadBalancedAffinityRouter(**kwargs)


def _aget(r, sid, now=None):
    """Read the remembered engine via the pluggable store."""
    return r._store.get(sid, time.time() if now is None else now, r.affinity_ttl)


def _aput(r, sid, url, now):
    r._store.put(sid, url, now, r.affinity_ttl)


def _eps(*urls):
    return [FakeEndpoint(u) for u in urls]


def _stats(loads):
    # loads: url -> (running, queue)
    return {
        url: EngineStats(num_running_requests=r, num_queuing_requests=q)
        for url, (r, q) in loads.items()
    }


# --- effective load ---------------------------------------------------------


def test_effective_load_sums_running_queue_inflight():
    r = _fresh_router()
    es = _stats({"http://e1": (3, 2)})
    assert r._effective_load("http://e1", es, now=1000.0) == 5
    r._record_dispatch(1000.0, "http://e1")
    assert r._effective_load("http://e1", es, now=1000.0) == 6


def test_effective_load_unknown_engine_gets_penalty():
    r = _fresh_router()
    # No stats at all -> ghost penalty (so it is never an idle-looking magnet).
    assert r._effective_load("http://ghost", {}, now=1000.0) >= r._NO_STATS_PENALTY


def test_release_inflight_decrements():
    r = _fresh_router()
    r._record_dispatch(1000.0, "http://e1")
    r._record_dispatch(1000.0, "http://e1")
    assert r._pending_load("http://e1", 1000.0) == 2
    r.release_inflight("http://e1")
    assert r._pending_load("http://e1", 1000.0) == 1


# --- P2C placement ----------------------------------------------------------


def test_p2c_two_engines_picks_lighter():
    # With exactly 2 engines and d=2, both are always sampled, so the lighter
    # one is chosen deterministically.
    r = _fresh_router(d_choices=2)
    es = _stats({"http://e1": (10, 0), "http://e2": (1, 0)})
    for _ in range(20):
        assert r._p2c(["http://e1", "http://e2"], es, now=1000.0) == "http://e2"


def test_p2c_avoids_ghost_when_healthy_exists():
    r = _fresh_router(d_choices=2)
    es = _stats({"http://e1": (5, 0)})  # e2 has no stats -> ghost penalty
    for _ in range(20):
        assert r._p2c(["http://e1", "http://e2"], es, now=1000.0) == "http://e1"


def test_p2c_single_engine():
    r = _fresh_router()
    assert r._p2c(["http://only"], {}, now=1000.0) == "http://only"


def test_p2c_balances_better_than_random_hash():
    # Statistical: 8 engines, many one-shot requests, d=2 over live (equal)
    # load should keep per-engine counts tight (the variable that matters is
    # max/min). Seeded for determinism.
    random.seed(1234)
    urls = [f"http://e{i}" for i in range(8)]
    r = _fresh_router(d_choices=2, affinity=False)
    counts = {u: 0 for u in urls}
    # Engines report their current in-flight as running, so P2C actually sees
    # the load it is creating (mimics a scrape that keeps up).
    for _ in range(8000):
        es = {
            u: EngineStats(num_running_requests=counts[u], num_queuing_requests=0)
            for u in urls
        }
        chosen = r.route_request(_eps(*urls), es, {}, FakeRequest({}))
        counts[chosen] += 1
    spread = max(counts.values()) / min(counts.values())
    # Pure random/hash placement of 8000 into 8 bins has a much wider spread;
    # load-aware P2C should stay very tight.
    assert spread < 1.15, counts


# --- affinity ---------------------------------------------------------------


def test_first_visit_places_and_remembers():
    r = _fresh_router(d_choices=2, affinity=True, session_key="sid")
    es = _stats({"http://e1": (10, 0), "http://e2": (1, 0)})
    chosen = r.route_request(
        _eps("http://e1", "http://e2"), es, {}, FakeRequest({"sid": "s1"})
    )
    assert chosen == "http://e2"  # placed by load
    assert _aget(r, "s1") == "http://e2"


def test_returning_session_sticks_when_not_busier():
    r = _fresh_router(d_choices=2, affinity=True, session_key="sid")
    eps = _eps("http://e1", "http://e2")
    # First visit -> remembered on e2 (lighter).
    es1 = _stats({"http://e1": (10, 0), "http://e2": (1, 0)})
    first = r.route_request(eps, es1, {}, FakeRequest({"sid": "s1"}))
    assert first == "http://e2"
    r.release_inflight(first)  # the first turn completed (sequential session)
    # Now both equal: remembered e2 is not busier than a P2C pick -> stays.
    es2 = _stats({"http://e1": (3, 0), "http://e2": (3, 0)})
    for _ in range(20):
        chosen = r.route_request(eps, es2, {}, FakeRequest({"sid": "s1"}))
        assert chosen == "http://e2"
        r.release_inflight(chosen)  # each turn completes before the next


def test_returning_session_sheds_when_remembered_is_busier():
    r = _fresh_router(d_choices=2, affinity=True, session_key="sid", affinity_slack=0.0)
    eps = _eps("http://e1", "http://e2")
    es1 = _stats({"http://e1": (10, 0), "http://e2": (0, 0)})
    assert r.route_request(eps, es1, {}, FakeRequest({"sid": "s1"})) == "http://e2"
    # e2 becomes much busier than e1 -> shed to e1 and re-remember e1.
    es2 = _stats({"http://e1": (0, 0), "http://e2": (20, 0)})
    # account for the inflight from the first dispatch on e2 too; either way e1 wins
    chosen = r.route_request(eps, es2, {}, FakeRequest({"sid": "s1"}))
    assert chosen == "http://e1"
    assert _aget(r, "s1") == "http://e1"


def test_affinity_slack_keeps_sticky_within_gap():
    r = _fresh_router(d_choices=2, affinity=True, session_key="sid", affinity_slack=5.0)
    eps = _eps("http://e1", "http://e2")
    es1 = _stats({"http://e1": (10, 0), "http://e2": (0, 0)})
    assert r.route_request(eps, es1, {}, FakeRequest({"sid": "s1"})) == "http://e2"
    # e2 busier by 4 (<= slack 5) -> still sticky despite e1 being lighter.
    # e2 load = 4 (+1 inflight from first dispatch = 5), e1 = 1; gap within slack.
    es2 = _stats({"http://e1": (1, 0), "http://e2": (4, 0)})
    assert r.route_request(eps, es2, {}, FakeRequest({"sid": "s1"})) == "http://e2"


def test_no_session_id_is_pure_p2c():
    r = _fresh_router(d_choices=2, affinity=True, session_key="sid")
    es = _stats({"http://e1": (10, 0), "http://e2": (1, 0)})
    # No sid header -> placement only, nothing remembered.
    chosen = r.route_request(_eps("http://e1", "http://e2"), es, {}, FakeRequest({}))
    assert chosen == "http://e2"
    assert _aget(r, "s1") is None


def test_affinity_disabled_without_session_key():
    r = _fresh_router(d_choices=2, affinity=True, session_key=None)
    assert r.affinity_enabled is False
    es = _stats({"http://e1": (10, 0), "http://e2": (1, 0)})
    r.route_request(_eps("http://e1", "http://e2"), es, {}, FakeRequest({"sid": "s1"}))
    assert _aget(r, "s1") is None


def test_affinity_ttl_eviction():
    r = _fresh_router(affinity=True, session_key="sid", affinity_ttl=100.0)
    _aput(r, "s1", "http://e1", 1000.0)
    assert _aget(r, "s1", 1099.0) == "http://e1"
    # Past TTL -> treated as gone.
    assert _aget(r, "s1", 1101.0) is None


def test_refresh_window_metrics_publishes_inflight():
    from vllm_router.services.metrics_service import load_balanced_inflight_requests

    r = _fresh_router()
    r._record_dispatch(1000.0, "http://e1")
    r._record_dispatch(1000.0, "http://e1")
    r.refresh_window_metrics(now=1000.0)
    val = load_balanced_inflight_requests.labels(server="http://e1")._value.get()
    assert val == 2


def test_affinity_hit_rate_window_decays():
    r = _fresh_router(affinity=True, session_key="sid", stats_window=100.0)
    # one hit then one shed within the window -> 0.5
    r._record_affinity(1000.0, True)
    r._record_affinity(1000.0, False)
    assert abs(r._aff_hit / len(r._aff_events) - 0.5) < 1e-9
    # advance past the window with no traffic -> rate decays to 0
    r.refresh_window_metrics(now=1000.0 + 200.0)
    assert len(r._aff_events) == 0


def test_remembered_engine_gone_replaces():
    r = _fresh_router(d_choices=2, affinity=True, session_key="sid")
    _aput(r, "s1", "http://dead", 1000.0)
    eps = _eps("http://e1", "http://e2")
    es = _stats({"http://e1": (5, 0), "http://e2": (1, 0)})
    # dead engine no longer in endpoints -> re-place by load and remember.
    chosen = r.route_request(eps, es, {}, FakeRequest({"sid": "s1"}))
    assert chosen == "http://e2"
    assert _aget(r, "s1") == "http://e2"
