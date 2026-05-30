"""Unit tests for percentile + overload snapshot helpers (C1)."""

from vllm_router.stats.request_stats import (
    MovingAverageMonitor,
    RequestStatsMonitor,
    SingletonMeta,
    initialize_request_stats_monitor,
)


def _fresh_monitor(window: float = 1000.0) -> RequestStatsMonitor:
    if RequestStatsMonitor in SingletonMeta._instances:
        del SingletonMeta._instances[RequestStatsMonitor]
    return initialize_request_stats_monitor(window)


def test_get_percentile_empty():
    m = MovingAverageMonitor(1000.0)
    assert m.get_percentile(0.5) == -1
    assert m.get_percentile(0.99) == -1


def test_get_percentile_single_value():
    m = MovingAverageMonitor(1000.0)
    m.update(1.0, 7.0)
    assert m.get_percentile(0.5) == 7.0
    assert m.get_percentile(0.99) == 7.0


def test_get_percentile_ordering_independent():
    m = MovingAverageMonitor(1000.0)
    for v in [10, 1, 5, 9, 2, 8, 3, 7, 4, 6]:
        m.update(1.0, float(v))
    # linear interpolation percentile on 1..10
    p50 = m.get_percentile(0.5)
    p99 = m.get_percentile(0.99)
    assert 5.0 <= p50 <= 6.0
    assert 9.0 <= p99 <= 10.0


def test_overload_snapshot_values():
    m = _fresh_monitor()
    url = "http://e1"
    m.ttft_monitors[url] = MovingAverageMonitor(1000.0)
    m.latency_monitors[url] = MovingAverageMonitor(1000.0)
    for v in range(1, 11):
        m.ttft_monitors[url].update(1.0, float(v))
        m.latency_monitors[url].update(1.0, float(v) * 2)

    snap = m.get_overload_snapshot(1000.0)
    assert url in snap
    assert 5.0 <= snap[url]["p50_ttft"] <= 6.0
    assert 9.0 <= snap[url]["p99_ttft"] <= 10.0
    assert 10.0 <= snap[url]["p50_e2e"] <= 12.0
    assert 18.0 <= snap[url]["p99_e2e"] <= 20.0


def test_overload_snapshot_ttl_cache():
    # large window so sliding-window expiry does not interfere with the TTL test
    m = _fresh_monitor(window=1e9)
    url = "http://e1"
    m.ttft_monitors[url] = MovingAverageMonitor(1e9)
    m.ttft_monitors[url].update(1000.0, 1.0)

    snap1 = m.get_overload_snapshot(1000.0)
    p99_first = snap1[url]["p99_ttft"]

    # add an extreme value, query again within the TTL -> cached, unchanged
    m.ttft_monitors[url].update(1000.0, 100.0)
    snap2 = m.get_overload_snapshot(1000.0)
    assert snap2[url]["p99_ttft"] == p99_first

    # query past the TTL window -> recomputed, reflects the new value
    snap3 = m.get_overload_snapshot(1000.0 + m._overload_refresh_interval + 0.01)
    assert snap3[url]["p99_ttft"] > p99_first


def test_request_bookkeeping_cleaned_on_complete():
    # Per-request dicts must not grow unbounded across unique request ids.
    m = _fresh_monitor()
    url = "http://e1"
    for i in range(100):
        rid = f"r{i}"
        m.on_new_request(url, rid, 1000.0 + i)
        m.on_request_response(url, rid, 1000.0 + i + 0.1)
        m.on_request_complete(url, rid, 1000.0 + i + 0.5)
    assert len(m.request_start_time) == 0
    assert len(m.first_token_time) == 0


def test_overload_snapshot_no_data_sentinel():
    m = _fresh_monitor()
    url = "http://e1"
    # engine known to latency monitor but with no samples
    m.latency_monitors[url] = MovingAverageMonitor(1000.0)
    snap = m.get_overload_snapshot(1000.0)
    assert snap[url]["p50_e2e"] == -1
    assert snap[url]["p50_ttft"] == -1
