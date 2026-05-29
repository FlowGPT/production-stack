"""Unit tests for CacheAwareLoadBalancingRouter (C2: queue threshold + fallback)."""

from vllm_router.routers.routing_logic import CacheAwareLoadBalancingRouter
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
    chosen = r._select_fallback("http://e1", eps, es, {})
    assert chosen == "http://e3"  # lowest queue among non-overloaded


def test_select_fallback_tie_break_by_p50_e2e():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2", "http://e3")]
    es = _stats({"http://e1": 10, "http://e2": 2, "http://e3": 2})
    snap = {
        "http://e2": {"p50_e2e": 9.0, "p99_e2e": 9, "p50_ttft": 1, "p99_ttft": 1},
        "http://e3": {"p50_e2e": 3.0, "p99_e2e": 3, "p50_ttft": 1, "p99_ttft": 1},
    }
    chosen = r._select_fallback("http://e1", eps, es, snap)
    assert chosen == "http://e3"  # same queue=2, smaller p50_e2e wins


def test_select_fallback_all_overloaded_returns_initial():
    r = _fresh_router(tolerate_waiting_requests=5)
    eps = [FakeEndpoint(u) for u in ("http://e1", "http://e2")]
    es = _stats({"http://e1": 8, "http://e2": 9})
    assert r._select_fallback("http://e1", eps, es, {}) == "http://e1"


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
