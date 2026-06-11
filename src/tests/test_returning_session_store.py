"""Tests for the returning-session store backends (memory + redis fail-open)."""

import fakeredis
import pytest

from vllm_router.routers.returning_session_store import (
    LocalCachedReturningSessionStore,
    MemoryReturningSessionStore,
    RedisReturningSessionStore,
    create_returning_session_store,
)
from vllm_router.services.metrics_service import (
    cache_aware_returning_session_store_errors_total,
)


def test_memory_store_first_visit_then_returning():
    store = MemoryReturningSessionStore()
    ttl = 100.0
    # First time the id is seen -> first visit.
    assert store.visit("a", now=1000.0, ttl=ttl) is False
    # Seen again within TTL -> returning.
    assert store.visit("a", now=1001.0, ttl=ttl) is True
    # A different id is independent.
    assert store.visit("b", now=1001.0, ttl=ttl) is False


def test_memory_store_ttl_expiry_resets_to_first_visit():
    store = MemoryReturningSessionStore()
    ttl = 10.0
    assert store.visit("a", now=1000.0, ttl=ttl) is False
    assert store.visit("a", now=1005.0, ttl=ttl) is True  # within TTL
    # Last seen at 1005; at 1016 that is older than ttl -> evicted -> first visit.
    assert store.visit("a", now=1016.0, ttl=ttl) is False


def test_memory_store_evicts_only_expired_from_front():
    # O(1) front eviction must drop only genuinely-expired entries and keep
    # the rest (the dict is ordered oldest-first because now only advances).
    store = MemoryReturningSessionStore()
    ttl = 10.0
    store.visit("a", now=100.0, ttl=ttl)  # expires at 110
    store.visit("b", now=105.0, ttl=ttl)  # expires at 115
    # At t=112, cutoff=102: 'a' (100) is stale, 'b' (105) survives.
    assert store.visit("c", now=112.0, ttl=ttl) is False
    assert store.visit("a", now=112.0, ttl=ttl) is False  # 'a' was evicted
    assert store.visit("b", now=112.0, ttl=ttl) is True  # 'b' survived


def test_memory_store_max_size_evicts_oldest_on_overflow():
    store = MemoryReturningSessionStore(max_size=2)
    ttl = 1000.0
    store.visit("a", now=1.0, ttl=ttl)  # {a}
    store.visit("b", now=2.0, ttl=ttl)  # {a, b}
    store.visit("c", now=3.0, ttl=ttl)  # over cap -> evict oldest 'a' -> {b, c}
    assert store.visit("a", now=4.0, ttl=ttl) is False  # 'a' was evicted


def test_memory_store_max_size_lru_protects_touched_entry():
    store = MemoryReturningSessionStore(max_size=2)
    ttl = 1000.0
    store.visit("a", now=1.0, ttl=ttl)  # {a}
    store.visit("b", now=2.0, ttl=ttl)  # {a, b}
    assert store.visit("a", now=3.0, ttl=ttl) is True  # touch 'a' -> {b, a}
    store.visit("c", now=4.0, ttl=ttl)  # over cap -> evict LRU 'b' -> {a, c}
    assert store.visit("b", now=5.0, ttl=ttl) is False  # 'b' evicted, 'a' survived


# --- B: LocalCachedReturningSessionStore (skip redis round-trips) ---


class _CountingBackend:
    """Fake shared backend that models TTL and counts visit() calls."""

    def __init__(self):
        self.calls = 0
        self._seen = {}

    def visit(self, session_id, now, ttl):
        self.calls += 1
        last = self._seen.get(session_id)
        is_returning = last is not None and (now - last) < ttl
        self._seen[session_id] = now
        return is_returning

    def close(self):
        pass


def test_local_cache_skips_backend_for_known_returning():
    b = _CountingBackend()
    s = LocalCachedReturningSessionStore(b, max_size=100, refresh_fraction=0.5)
    ttl = 100.0
    assert s.visit("x", 0.0, ttl) is False  # first -> backend queried
    assert b.calls == 1
    # Subsequent visits within the refresh window are served locally.
    assert s.visit("x", 1.0, ttl) is True
    assert s.visit("x", 2.0, ttl) is True
    assert b.calls == 1  # no extra backend round-trips


def test_local_cache_refreshes_backend_past_midlife():
    b = _CountingBackend()
    s = LocalCachedReturningSessionStore(b, refresh_fraction=0.5)
    ttl = 100.0
    s.visit("x", 0.0, ttl)  # calls=1, last write=0
    s.visit("x", 10.0, ttl)  # 10 < 50 -> skip
    assert b.calls == 1
    s.visit("x", 60.0, ttl)  # 60 >= 50 (mid-life) -> refresh backend TTL
    assert b.calls == 2
    s.visit("x", 70.0, ttl)  # 70-60=10 < 50 -> skip again
    assert b.calls == 2


def test_local_cache_expiry_requeries_backend():
    b = _CountingBackend()
    s = LocalCachedReturningSessionStore(b)
    ttl = 10.0
    assert s.visit("x", 0.0, ttl) is False  # calls=1
    # Local entry older than ttl -> re-query backend, which (modelling redis
    # TTL) also reports a new session.
    assert s.visit("x", 20.0, ttl) is False
    assert b.calls == 2


def test_local_cache_lru_evicts_and_requeries():
    b = _CountingBackend()
    s = LocalCachedReturningSessionStore(b, max_size=1)
    ttl = 100.0
    s.visit("a", 0.0, ttl)  # local={a}, calls=1
    s.visit("b", 1.0, ttl)  # over cap -> drop 'a', local={b}, calls=2
    s.visit("a", 2.0, ttl)  # 'a' no longer local -> backend queried, calls=3
    assert b.calls == 3


def _fake_redis_store(prefix="test:returning:"):
    """Build a RedisReturningSessionStore backed by fakeredis.

    fakeredis (without lupa) cannot run Lua, so we emulate the single
    ``visit`` round-trip with the equivalent EXISTS + SET EX against the fake
    client. This still exercises key hashing, the visit() wrapper and the
    fail-open path.
    """
    store = RedisReturningSessionStore.__new__(RedisReturningSessionStore)
    store._key_prefix = prefix
    store._client = fakeredis.FakeStrictRedis()

    def _visit(keys, args):
        key, ttl = keys[0], args[0]
        existed = store._client.exists(key)
        store._client.set(key, "1", ex=ttl)
        return existed

    store._visit = _visit
    return store


def test_redis_store_first_visit_then_returning():
    store = _fake_redis_store()
    assert store.visit("sess-1", now=0.0, ttl=60.0) is False
    assert store.visit("sess-1", now=0.0, ttl=60.0) is True
    assert store.visit("sess-2", now=0.0, ttl=60.0) is False


def test_redis_store_keys_are_hashed_with_prefix():
    store = _fake_redis_store(prefix="p:")
    store.visit("some-long-session-id", now=0.0, ttl=60.0)
    keys = [k.decode() for k in store._client.keys("*")]
    assert len(keys) == 1
    assert keys[0].startswith("p:")
    # Hashed, so the raw session id is not embedded in the key.
    assert "some-long-session-id" not in keys[0]


def test_redis_store_fails_open_and_counts_errors():
    store = _fake_redis_store()

    def _boom(keys, args):
        raise RuntimeError("redis down")

    store._visit = _boom
    before = cache_aware_returning_session_store_errors_total.labels(
        operation="visit"
    )._value.get()
    # Fail-open: a backend error is treated as a first visit (False), never raises.
    assert store.visit("sess", now=0.0, ttl=60.0) is False
    after = cache_aware_returning_session_store_errors_total.labels(
        operation="visit"
    )._value.get()
    assert after - before == 1


def test_factory_memory_default():
    store = create_returning_session_store("memory")
    assert isinstance(store, MemoryReturningSessionStore)


def test_factory_redis_requires_url():
    with pytest.raises(ValueError):
        create_returning_session_store("redis", redis_url=None)


def test_factory_redis_unreachable_fails_hard_at_startup():
    # An explicit store=redis whose Redis is unreachable must fail hard at
    # startup (not silently downgrade to per-replica memory), so the broken
    # cross-replica setup surfaces instead of running with wrong metrics.
    with pytest.raises(Exception):
        create_returning_session_store(
            "redis",
            redis_url="redis://127.0.0.1:1/0",
            redis_timeout=0.05,
        )
