"""Unit tests for the load_balanced_affinity session->engine stores."""

import time

from vllm_router.routers.affinity_store import (
    MemoryAffinityStore,
    RedisAffinityStore,
    create_affinity_store,
)
from vllm_router.routers.routing_logic import LoadBalancedAffinityRouter
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.utils import SingletonABCMeta

# --- MemoryAffinityStore ----------------------------------------------------


def test_memory_put_get_roundtrip():
    s = MemoryAffinityStore()
    assert s.get("s1", now=1000.0, ttl=100.0) is None
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)
    assert s.get("s1", now=1050.0, ttl=100.0) == "http://e1"


def test_memory_ttl_expiry():
    s = MemoryAffinityStore()
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)
    assert s.get("s1", now=1099.0, ttl=100.0) == "http://e1"
    assert s.get("s1", now=1101.0, ttl=100.0) is None


def test_memory_lru_cap_evicts_oldest():
    s = MemoryAffinityStore(max_size=2)
    s.put("s1", "http://e1", now=1000.0, ttl=1000.0)
    s.put("s2", "http://e2", now=1001.0, ttl=1000.0)
    s.put("s3", "http://e3", now=1002.0, ttl=1000.0)  # evicts s1 (oldest)
    assert s.get("s1", now=1003.0, ttl=1000.0) is None
    assert s.get("s2", now=1003.0, ttl=1000.0) == "http://e2"
    assert s.get("s3", now=1003.0, ttl=1000.0) == "http://e3"


def test_memory_revisit_refreshes_recency():
    s = MemoryAffinityStore(max_size=2)
    s.put("s1", "http://e1", now=1000.0, ttl=1000.0)
    s.put("s2", "http://e2", now=1001.0, ttl=1000.0)
    s.put("s1", "http://e1", now=1002.0, ttl=1000.0)  # s1 now most-recent
    s.put("s3", "http://e3", now=1003.0, ttl=1000.0)  # evicts s2, not s1
    assert s.get("s1", now=1004.0, ttl=1000.0) == "http://e1"
    assert s.get("s2", now=1004.0, ttl=1000.0) is None


# --- RedisAffinityStore (fail-open, no live redis) --------------------------


class _BoomClient:
    """Stands in for redis.Redis: every op raises, so we exercise fail-open."""

    def get(self, *a, **k):
        raise RuntimeError("redis down")

    def set(self, *a, **k):
        raise RuntimeError("redis down")

    def close(self):
        pass


def _redis_store_with_client(client) -> RedisAffinityStore:
    from collections import OrderedDict

    s = RedisAffinityStore.__new__(RedisAffinityStore)
    s._key_prefix = "vllm:lb-affinity:"
    s._client = client
    s._refresh_fraction = 0.5
    s._write_cache_size = 100_000
    s._writes = OrderedDict()
    s._breaker_threshold = 3
    s._breaker_cooldown = 5.0
    s._consec_failures = 0
    s._breaker_open_until = 0.0
    return s


def test_redis_get_fails_open_to_none():
    s = _redis_store_with_client(_BoomClient())
    # Error must not propagate; treated as "no memory" -> None.
    assert s.get("s1", now=1000.0, ttl=100.0) is None


def test_redis_put_fails_open_silently():
    s = _redis_store_with_client(_BoomClient())
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)  # must not raise


class _CountingBoomClient:
    """Fails every call; counts how many redis calls actually reached it."""

    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        raise RuntimeError("redis down")

    def set(self, *a, **k):
        self.calls += 1
        raise RuntimeError("redis down")


def test_breaker_opens_after_threshold_and_skips_redis():
    c = _CountingBoomClient()
    s = _redis_store_with_client(c)  # threshold=3, cooldown=5.0
    # First 3 calls actually hit redis (each blocks/fails), tripping the breaker.
    for i in range(3):
        s.get("s1", now=1000.0, ttl=100.0)
    assert c.calls == 3
    # Breaker now open: further calls within the cooldown do NOT touch redis.
    for i in range(20):
        s.get("s1", now=1001.0, ttl=100.0)
        s.put("s1", "http://e1", now=1001.0, ttl=100.0)
    assert c.calls == 3  # zero extra redis calls while open -> loop not blocked


def test_breaker_half_opens_after_cooldown():
    c = _CountingBoomClient()
    s = _redis_store_with_client(c)
    for _ in range(3):
        s.get("s1", now=1000.0, ttl=100.0)
    assert c.calls == 3
    # Past the cooldown window -> one probe call is allowed through.
    s.get("s1", now=1000.0 + 5.0 + 0.001, ttl=100.0)
    assert c.calls == 4


def test_breaker_resets_on_success():
    # A working client keeps the breaker closed and failure count at 0.
    client = _DictClient()
    s = _redis_store_with_client_throttled(client)
    for i in range(10):
        s.put(f"s{i}", "http://e1", now=1000.0 + i, ttl=100.0)
        s.get(f"s{i}", now=1000.0 + i, ttl=100.0)
    assert s._consec_failures == 0
    assert s._breaker_open_until == 0.0


class _DictClient:
    """Minimal in-memory stand-in for redis; counts set calls for throttle tests."""

    def __init__(self):
        self.kv = {}
        self.set_calls = 0
        self.get_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.set_calls += 1
        self.kv[key] = value.encode("utf-8")

    def close(self):
        pass


def _redis_store_with_client_throttled(client, refresh_fraction=0.5):
    s = RedisAffinityStore.__new__(RedisAffinityStore)
    s._key_prefix = "vllm:lb-affinity:"
    s._client = client
    s._refresh_fraction = refresh_fraction
    s._write_cache_size = 100_000
    from collections import OrderedDict

    s._writes = OrderedDict()
    s._breaker_threshold = 3
    s._breaker_cooldown = 5.0
    s._consec_failures = 0
    s._breaker_open_until = 0.0
    return s


def test_redis_happy_path_roundtrip_and_hashed_key():
    client = _DictClient()
    s = _redis_store_with_client_throttled(client)
    s.put("session-中文", "http://e1", now=1000.0, ttl=100.0)
    assert s.get("session-中文", now=1000.0, ttl=100.0) == "http://e1"
    # Key is hashed + prefixed, never the raw session id.
    (stored_key,) = client.kv.keys()
    assert stored_key.startswith("vllm:lb-affinity:")
    assert "session-中文" not in stored_key


def test_write_throttle_skips_unchanged_engine_within_window():
    client = _DictClient()
    s = _redis_store_with_client_throttled(client, refresh_fraction=0.5)
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)  # first write -> SET
    assert client.set_calls == 1
    # Same engine, within 0.5*100=50s -> throttled (no SET).
    s.put("s1", "http://e1", now=1010.0, ttl=100.0)
    s.put("s1", "http://e1", now=1040.0, ttl=100.0)
    assert client.set_calls == 1


def test_write_throttle_refreshes_past_window():
    client = _DictClient()
    s = _redis_store_with_client_throttled(client, refresh_fraction=0.5)
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)
    assert client.set_calls == 1
    # Past 0.5*100=50s -> refresh SET fires.
    s.put("s1", "http://e1", now=1060.0, ttl=100.0)
    assert client.set_calls == 2


def test_write_throttle_always_writes_changed_engine():
    client = _DictClient()
    s = _redis_store_with_client_throttled(client, refresh_fraction=0.5)
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)
    assert client.set_calls == 1
    # Engine changed (shed) within the window -> MUST write through immediately.
    s.put("s1", "http://e2", now=1005.0, ttl=100.0)
    assert client.set_calls == 2
    assert s.get("s1", now=1005.0, ttl=100.0) == "http://e2"


def test_write_throttle_disabled_writes_every_time():
    client = _DictClient()
    s = _redis_store_with_client_throttled(client, refresh_fraction=0.0)
    s.put("s1", "http://e1", now=1000.0, ttl=100.0)
    s.put("s1", "http://e1", now=1001.0, ttl=100.0)
    assert client.set_calls == 2


# --- factory ----------------------------------------------------------------


def test_factory_memory_default():
    assert isinstance(create_affinity_store("memory"), MemoryAffinityStore)


def test_factory_redis_requires_url():
    try:
        create_affinity_store("redis", redis_url=None)
    except ValueError as e:
        assert "redis" in str(e).lower()
    else:
        raise AssertionError("expected ValueError when redis url missing")


# Unreachable redis (refused connection) -> ping fails quickly.
_UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


def test_factory_redis_soft_startup_does_not_raise():
    # Default: redis is not a hard dependency. Unreachable at startup must NOT
    # raise; a redis store is returned and self-heals once redis is reachable.
    store = create_affinity_store(
        "redis", redis_url=_UNREACHABLE_REDIS, redis_timeout=0.05
    )
    assert isinstance(store, RedisAffinityStore)
    # Fail-open: get on a down redis returns None instead of raising.
    assert store.get("s1", now=1000.0, ttl=100.0) is None


def test_factory_redis_required_hard_fails_when_unreachable():
    try:
        create_affinity_store(
            "redis",
            redis_url=_UNREACHABLE_REDIS,
            redis_timeout=0.05,
            redis_required=True,
        )
    except Exception:
        pass
    else:
        raise AssertionError("expected startup failure when redis_required=True")


# --- router uses an injected store ------------------------------------------


class FakeEndpoint:
    def __init__(self, url):
        self.url = url


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def _fresh_router(**kwargs) -> LoadBalancedAffinityRouter:
    SingletonABCMeta._instances.pop(LoadBalancedAffinityRouter, None)
    return LoadBalancedAffinityRouter(**kwargs)


def test_router_routes_via_injected_store():
    """A shared store makes affinity decisions visible across router instances,
    emulating two workers backed by the same redis."""
    shared = MemoryAffinityStore()
    eps = [FakeEndpoint("http://e1"), FakeEndpoint("http://e2")]
    es = {
        "http://e1": EngineStats(num_running_requests=10, num_queuing_requests=0),
        "http://e2": EngineStats(num_running_requests=1, num_queuing_requests=0),
    }

    # Worker A places the session on the lighter engine (e2) into the shared map.
    a = _fresh_router(
        d_choices=2, affinity=True, session_key="sid", affinity_store=shared
    )
    first = a.route_request(eps, es, {}, FakeRequest({"sid": "s1"}))
    assert first == "http://e2"

    # Worker B (separate instance, same shared store) sees s1 pinned to e2 and,
    # with both engines now equal, keeps it there instead of re-placing fresh.
    b = _fresh_router(
        d_choices=2, affinity=True, session_key="sid", affinity_store=shared
    )
    es_equal = {
        "http://e1": EngineStats(num_running_requests=3, num_queuing_requests=0),
        "http://e2": EngineStats(num_running_requests=3, num_queuing_requests=0),
    }
    chosen = b.route_request(eps, es_equal, {}, FakeRequest({"sid": "s1"}))
    assert chosen == "http://e2"


def test_close_affinity_store_is_safe():
    r = _fresh_router(affinity=True, session_key="sid")
    r.close_affinity_store()  # must not raise on the memory backend
