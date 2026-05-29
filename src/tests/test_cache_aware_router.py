"""Unit tests for CacheAwareLoadBalancingRouter (C2: queue threshold + fallback)."""

from vllm_router.routers.routing_logic import CacheAwareLoadBalancingRouter
from vllm_router.services.metrics_service import (
    cache_aware_fallback_rate,
    cache_aware_fallback_reason_rate,
    cache_aware_stickiness_rate,
)
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.stats.request_stats import (
    RequestStatsMonitor,
    SingletonMeta,
    initialize_request_stats_monitor,
)
from vllm_router.utils import SingletonABCMeta


class FakeEndpoint:
    def __init__(self, url: str):
        self.url = url


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def _fresh_router(**kwargs) -> CacheAwareLoadBalancingRouter:
    SingletonABCMeta._instances.pop(CacheAwareLoadBalancingRouter, None)
    # ensure the stats monitor singleton exists but is empty (no latency data)
    SingletonMeta._instances.pop(RequestStatsMonitor, None)
    initialize_request_stats_monitor(60.0)
    kwargs.setdefault("session_key", "session_id")
    kwargs.setdefault("tolerate_waiting_requests", 5)
    return CacheAwareLoadBalancingRouter(**kwargs)


def _stats(queues):
    return {url: EngineStats(num_queuing_requests=q) for url, q in queues.items()}


def test_violated_reasons_queue():
    r = _fresh_router(tolerate_waiting_requests=5)
    es = _stats({"http://e1": 5, "http://e2": 4})
    assert r._violated_reasons("http://e1", es, {}) == ["queue"]
    assert r._violated_reasons("http://e2", es, {}) == []


def test_violated_reasons_unknown_engine():
    r = _fresh_router(tolerate_waiting_requests=5)
    assert r._violated_reasons("http://missing", {}, {}) == []


def test_select_fallback_excludes_overloaded_picks_min_queue():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 10, "http://e2": 2, "http://e3": 1})
    chosen = r._select_fallback("http://e1", eps, es, {}, now=1000.0)
    assert chosen == "http://e3"  # lowest queue among non-overloaded


def test_select_fallback_tie_break_by_p50_e2e():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 10, "http://e2": 2, "http://e3": 2})
    snap = {
        "http://e2": {"p50_e2e": 9.0, "p99_e2e": 9, "p50_ttft": 1, "p99_ttft": 1},
        "http://e3": {"p50_e2e": 3.0, "p99_e2e": 3, "p50_ttft": 1, "p99_ttft": 1},
    }
    chosen = r._select_fallback("http://e1", eps, es, snap, now=1000.0)
    assert chosen == "http://e3"  # same queue=2, smaller p50_e2e wins


def test_select_fallback_all_overloaded_returns_initial():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2")]
    es = _stats({"http://e1": 8, "http://e2": 9})
    assert r._select_fallback("http://e1", eps, es, {}, now=1000.0) == "http://e1"


def test_route_request_sticky_when_under_threshold():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 0, "http://e2": 0, "http://e3": 0})
    req = FakeRequest({"session_id": "abc"})
    r._update_hash_ring(eps)
    expected = r.hash_ring.get_node("abc")
    assert r.route_request(eps, es, {}, req) == expected


def test_route_request_fallback_when_initial_overloaded():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    req = FakeRequest({"session_id": "abc"})
    r._update_hash_ring(eps)
    initial = r.hash_ring.get_node("abc")
    others = [u for u in ("http://e1", "http://e2", "http://e3") if u != initial]
    # overload the sticky engine, keep one clearly-best other engine
    es = _stats({initial: 99, others[0]: 0, others[1]: 3})
    assert r.route_request(eps, es, {}, req) == others[0]


def test_route_request_no_session_picks_least_queue():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 7, "http://e2": 1, "http://e3": 4})
    req = FakeRequest({})  # no session id
    assert r.route_request(eps, es, {}, req) == "http://e2"


def _snap(p50_ttft=-1, p99_ttft=-1, p50_e2e=-1, p99_e2e=-1):
    return {
        "p50_ttft": p50_ttft,
        "p99_ttft": p99_ttft,
        "p50_e2e": p50_e2e,
        "p99_e2e": p99_e2e,
    }


def test_violated_reasons_latency_thresholds():
    r = _fresh_router(
        tolerate_waiting_requests=100,
        p50_ttft_threshold=2.0,
        p99_ttft_threshold=5.0,
        p50_e2e_threshold=8.0,
        p99_e2e_threshold=20.0,
    )
    es = _stats({"http://e1": 0})
    over = {"http://e1": _snap(p50_ttft=3.0, p99_ttft=6.0, p50_e2e=9.0, p99_e2e=25.0)}
    assert set(r._violated_reasons("http://e1", es, over)) == {
        "p50_ttft",
        "p99_ttft",
        "p50_e2e",
        "p99_e2e",
    }
    under = {"http://e1": _snap(p50_ttft=1.0, p99_ttft=4.0, p50_e2e=7.0, p99_e2e=19.0)}
    assert r._violated_reasons("http://e1", es, under) == []


def test_violated_reasons_queue_and_latency_combined():
    r = _fresh_router(tolerate_waiting_requests=5, p99_e2e_threshold=10.0)
    es = _stats({"http://e1": 9})
    snap = {"http://e1": _snap(p99_e2e=15.0)}
    reasons = r._violated_reasons("http://e1", es, snap)
    assert reasons[0] == "queue"
    assert "p99_e2e" in reasons


def test_threshold_disabled_when_zero():
    r = _fresh_router(tolerate_waiting_requests=5, p50_ttft_threshold=0.0)
    es = _stats({"http://e1": 0})
    snap = {"http://e1": _snap(p50_ttft=999.0)}
    assert r._violated_reasons("http://e1", es, snap) == []


def test_latency_no_data_does_not_trigger():
    r = _fresh_router(tolerate_waiting_requests=5, p50_e2e_threshold=1.0)
    es = _stats({"http://e1": 0})
    # snapshot reports -1 (no completed requests) -> must not fall back
    snap = {"http://e1": _snap(p50_e2e=-1)}
    assert r._violated_reasons("http://e1", es, snap) == []


def test_route_request_fallback_on_latency_threshold():
    r = _fresh_router(tolerate_waiting_requests=100, p99_e2e_threshold=10.0)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    req = FakeRequest({"session_id": "abc"})
    r._update_hash_ring(eps)
    initial = r.hash_ring.get_node("abc")
    others = [u for u in ("http://e1", "http://e2", "http://e3") if u != initial]
    es = _stats({"http://e1": 0, "http://e2": 0, "http://e3": 0})
    # only the sticky engine breaches the p99 e2e latency threshold
    snapshot = {initial: _snap(p99_e2e=50.0)}
    chosen = r._route_with_snapshot(eps, es, req, snapshot)
    assert chosen in others


def test_window_counts_and_eviction():
    r = _fresh_router(stats_window=30.0)
    r._record(100.0, False, [])
    r._record(101.0, True, ["queue"])
    r._record(102.0, True, ["p50_e2e"])
    total, fallback, reasons = r.get_window_stats()
    assert total == 3
    assert fallback == 2
    assert reasons["queue"] == 1
    assert reasons["p50_e2e"] == 1

    # record well past the window -> earlier events evicted
    r._record(140.0, False, [])
    total, fallback, reasons = r.get_window_stats()
    assert total == 1
    assert fallback == 0
    assert reasons["queue"] == 0


def test_window_rate_gauges():
    r = _fresh_router(stats_window=30.0)
    r._record(200.0, False, [])
    r._record(201.0, True, ["queue"])
    r._record(202.0, True, ["queue", "p99_e2e"])
    # 3 total, 2 fallback, queue=2, p99_e2e=1
    assert abs(cache_aware_stickiness_rate._value.get() - (1 / 3)) < 1e-9
    assert abs(cache_aware_fallback_rate._value.get() - (2 / 3)) < 1e-9
    assert (
        abs(
            cache_aware_fallback_reason_rate.labels(reason="queue")._value.get()
            - (2 / 3)
        )
        < 1e-9
    )
    assert (
        abs(
            cache_aware_fallback_reason_rate.labels(reason="p99_e2e")._value.get()
            - (1 / 3)
        )
        < 1e-9
    )


def test_refresh_window_metrics_decays_when_idle():
    # Rates must decay toward zero when no new requests arrive, not freeze.
    r = _fresh_router(stats_window=30.0)
    r._record(1000.0, True, ["queue"])
    r._record(1001.0, False, [])
    assert cache_aware_fallback_rate._value.get() == 0.5
    # long after the window, a scrape-time refresh should show no activity
    r.refresh_window_metrics(now=1000.0 + 100.0)
    assert cache_aware_fallback_rate._value.get() == 0.0
    assert cache_aware_stickiness_rate._value.get() == 0.0
    assert cache_aware_fallback_reason_rate.labels(reason="queue")._value.get() == 0.0


def test_base_router_release_inflight_is_noop():
    from vllm_router.routers.routing_logic import RoundRobinRouter

    SingletonABCMeta._instances.pop(RoundRobinRouter, None)
    rr = RoundRobinRouter()
    # default contract method exists and does nothing / does not raise
    assert rr.release_inflight("http://whatever") is None


def test_route_with_snapshot_records_decisions():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    req = FakeRequest({"session_id": "abc"})
    r._update_hash_ring(eps)
    initial = r.hash_ring.get_node("abc")

    # under threshold -> sticky recorded
    es_ok = _stats({"http://e1": 0, "http://e2": 0, "http://e3": 0})
    r._route_with_snapshot(eps, es_ok, req, {})
    total, fallback, _ = r.get_window_stats()
    assert total == 1 and fallback == 0

    # overload sticky engine -> fallback recorded with reason
    es_bad = _stats(
        {
            u: (99 if u == initial else 0)
            for u in ("http://e1", "http://e2", "http://e3")
        }
    )
    r._route_with_snapshot(eps, es_bad, req, {})
    total, fallback, reasons = r.get_window_stats()
    assert total == 2 and fallback == 1
    assert reasons["queue"] == 1


# --- C6: in-flight accounting prevents the fallback thundering herd ---


def test_fallback_no_recent_dispatch_clear_winner():
    # With a clear lowest-load candidate the choice is deterministic.
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e2", "http://e3")]
    es = _stats({"http://e2": 0, "http://e3": 2})
    assert r._select_fallback("http://e1", eps, es, {}, now=1000.0) == "http://e2"


def test_inflight_accounting_spreads_repeated_fallback():
    # Two idle candidates with identical stale queue=0. Repeated fallback
    # decisions must spread once recent dispatches are accounted for, instead
    # of all piling onto the first candidate (the thundering herd).
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e2", "http://e3")]
    es = _stats({"http://e2": 0, "http://e3": 0})
    now = 1000.0
    picks = []
    for _ in range(10):
        url = r._select_fallback("http://e1", eps, es, {}, now=now)
        r._record_dispatch(now, url)
        picks.append(url)
    assert picks.count("http://e2") >= 4
    assert picks.count("http://e3") >= 4


def test_inflight_safety_cap_drops_unobserved():
    # Without a completion signal, the safety cap eventually drops leaked entries.
    r = _fresh_router(tolerate_waiting_requests=5, inflight_decay=5.0)
    r._record_dispatch(1000.0, "http://e2")
    r._record_dispatch(1000.0, "http://e2")
    assert r._pending_load("http://e2", 1000.0) == 2
    assert r._pending_load("http://e2", 1000.0 + 5.01) == 0


def test_completion_decrements_inflight():
    r = _fresh_router(tolerate_waiting_requests=5)
    r._record_dispatch(1000.0, "http://e2")
    r._record_dispatch(1000.0, "http://e2")
    assert r._pending_load("http://e2", 1000.0) == 2
    r.release_inflight("http://e2")
    assert r._pending_load("http://e2", 1000.0) == 1
    r.release_inflight("http://e2")
    assert r._pending_load("http://e2", 1000.0) == 0
    # extra completion is harmless
    r.release_inflight("http://e2")
    assert r._pending_load("http://e2", 1000.0) == 0


def test_select_fallback_clear_winner_is_deterministic():
    # A clear load winner is always chosen (no randomness) across many calls.
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 10, "http://e2": 0, "http://e3": 3})
    picks = {
        r._select_fallback("http://e1", eps, es, {}, now=1000.0) for _ in range(50)
    }
    assert picks == {"http://e2"}


def test_select_fallback_randomises_genuine_ties():
    # Two idle candidates, identical load and no latency data: independent
    # decisions (e.g. separate router replicas) must NOT all pick the same one.
    import random as _random

    _random.seed(0)
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 10, "http://e2": 0, "http://e3": 0})
    # NOTE: no _record_dispatch between calls -> simulates independent replicas
    # each seeing the same empty/stale view.
    picks = [
        r._select_fallback("http://e1", eps, es, {}, now=1000.0) for _ in range(200)
    ]
    assert set(picks) == {"http://e2", "http://e3"}
    # roughly balanced
    assert 60 < picks.count("http://e2") < 140


def test_tie_break_p50_still_deterministic_within_load_tie():
    # Load-tied but latency differs -> still deterministic by p50 (no randomness).
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 10, "http://e2": 0, "http://e3": 0})
    snap = {
        "http://e2": {"p50_e2e": 9.0, "p99_e2e": 9, "p50_ttft": 1, "p99_ttft": 1},
        "http://e3": {"p50_e2e": 3.0, "p99_e2e": 3, "p50_ttft": 1, "p99_ttft": 1},
    }
    picks = {
        r._select_fallback("http://e1", eps, es, snap, now=1000.0) for _ in range(50)
    }
    assert picks == {"http://e3"}


def test_tie_tolerance_groups_near_equal_loads():
    # With tolerance >= 1, loads 0 and 1 are treated as tied and randomised.
    import random as _random

    _random.seed(1)
    r = _fresh_router(tolerate_waiting_requests=100, tie_tolerance=1.0)
    eps = [FakeEndpoint(u) for u in ("http://e2", "http://e3")]
    es = _stats({"http://e2": 0, "http://e3": 1})
    picks = [
        r._select_fallback("http://e1", eps, es, {}, now=1000.0) for _ in range(200)
    ]
    assert set(picks) == {"http://e2", "http://e3"}


def test_fallback_prefers_lower_scraped_running():
    # Cross-replica signal: two candidates with equal queue=0, the one with
    # lower scraped running (= aggregate load from all replicas) is chosen.
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = {
        "http://e1": EngineStats(num_queuing_requests=10),
        "http://e2": EngineStats(num_queuing_requests=0, num_running_requests=8),
        "http://e3": EngineStats(num_queuing_requests=0, num_running_requests=1),
    }
    assert r._select_fallback("http://e1", eps, es, {}, now=1000.0) == "http://e3"


def test_long_inflight_not_forgotten_until_complete():
    # Core #1 regression: a long-running request stays counted for its whole
    # lifetime (well beyond any short window), not dropped on a fixed decay.
    r = _fresh_router(tolerate_waiting_requests=5, inflight_decay=300.0)
    r._record_dispatch(1000.0, "http://e2")
    # 100s later (longer than the old 5s decay) it is still in flight
    assert r._pending_load("http://e2", 1100.0) == 1
    # only an actual completion clears it
    r.release_inflight("http://e2")
    assert r._pending_load("http://e2", 1100.0) == 0


def test_route_records_dispatch_for_chosen_engine():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    req = FakeRequest({"session_id": "abc"})
    r._update_hash_ring(eps)
    initial = r.hash_ring.get_node("abc")
    es = _stats({u: 0 for u in ("http://e1", "http://e2", "http://e3")})
    now = 1000.0
    chosen = r._route_with_snapshot(eps, es, req, {}, now=now)
    assert r._pending_load(chosen, now) == 1
    assert chosen == initial  # under threshold -> sticky, still recorded
