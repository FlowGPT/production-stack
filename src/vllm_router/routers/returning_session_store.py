# Copyright 2024-2025 The vLLM Production Stack Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pluggable store that recognises "returning" sessions for the cache-aware
router's returning-session metrics.

A session is "returning" when this router has already routed at least one
request for the same session id within the retention TTL. The store ONLY backs
the ``cache_aware_returning_*`` / ``cache_aware_first_visit_*`` metrics; it does
not influence which engine a request is routed to.

Two backends:

* ``memory`` (default): a process-local store. Each router replica recognises
  only the sessions it has seen itself, so in a multi-replica deployment the
  returning metrics undercount (the same session hitting a different replica
  looks new). Eviction is O(1) amortized: because ``now`` only moves forward,
  entries are time-ordered, so expired ids are popped from the front and a
  ``max_size`` LRU cap bounds memory.
* ``redis``: shares the "seen" state across all router replicas through one
  Redis instance, so returning recognition is correct cluster-wide and Redis
  TTL evicts keys (the router does not grow memory with unique sessions). A
  single Lua round-trip per visit (``EXISTS`` + ``SET EX``). Fails open: any
  Redis error is counted and treated as a first visit so routing is never
  affected.

Optionally, the redis backend can be fronted by a per-replica LRU of session
ids known to be returning (``LocalCachedReturningSessionStore``) to skip the
network round-trip for sessions that keep hitting the same replica (LB
affinity), while redis stays authoritative for cross-replica first contact.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Optional, Protocol

import xxhash

from vllm_router.services.metrics_service import (
    cache_aware_returning_session_store_errors_total,
)

logger = logging.getLogger(__name__)

# One round-trip: report whether the key already existed, then (re)set it with a
# fresh TTL. Returning recognition + mark-seen in a single atomic call.
_REDIS_VISIT_SCRIPT = """
local existed = redis.call('EXISTS', KEYS[1])
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
return existed
"""


class ReturningSessionStore(Protocol):
    """Backend that records and queries whether a session was seen before."""

    def visit(self, session_id: str, now: float, ttl: float) -> bool:
        """Record a routing decision for ``session_id`` and return whether the
        session had already been seen within ``ttl`` (i.e. this is a returning
        request, not the first visit)."""
        ...

    def close(self) -> None:
        """Release any backend resources (no-op for in-memory)."""
        ...


class MemoryReturningSessionStore:
    """Process-local store. Suitable for a single router replica.

    Backed by an ``OrderedDict`` keyed by session id, value = last-seen time.
    ``now`` only moves forward, so re-inserting a visited id at the end keeps the
    dict ordered oldest-first. Eviction therefore only pops expired entries from
    the front and stops at the first live one -- O(number actually expired),
    amortized O(1) per visit (no full-table scan). ``max_size`` (> 0) bounds
    memory with an LRU cap on top of TTL eviction.
    """

    def __init__(self, max_size: int = 0) -> None:
        # session_id -> last-seen wall time. Insertion order == time order.
        self._known: "OrderedDict[str, float]" = OrderedDict()
        self._max_size = max_size

    def visit(self, session_id: str, now: float, ttl: float) -> bool:
        self._evict(now, ttl)
        is_returning = session_id in self._known
        # Move/insert to the end so the front stays the oldest entry.
        self._known[session_id] = now
        self._known.move_to_end(session_id)
        if self._max_size and len(self._known) > self._max_size:
            self._known.popitem(last=False)  # drop least-recently-seen
        return is_returning

    def _evict(self, now: float, ttl: float) -> None:
        cutoff = now - ttl
        # Front is always the oldest; stop at the first non-expired entry.
        while self._known:
            sid, ts = next(iter(self._known.items()))
            if ts >= cutoff:
                break
            del self._known[sid]

    def close(self) -> None:
        self._known.clear()


class RedisReturningSessionStore:
    """Redis-backed store shared across router replicas.

    Keys are ``{key_prefix}{xxhash(session_id)}`` so arbitrarily long or
    exotic session ids map to bounded, safe keys. Fails open on any Redis
    error: the visit is treated as a first visit and the error is counted, so
    routing is never blocked by Redis.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "vllm:returning-session:",
        socket_timeout: float = 0.05,
    ) -> None:
        import redis

        self._key_prefix = key_prefix
        self._client = redis.Redis.from_url(
            redis_url,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
        )
        # Register the Lua script once; redis-py caches the SHA and uses EVALSHA.
        self._visit = self._client.register_script(_REDIS_VISIT_SCRIPT)
        # Probe at construction so a misconfigured URL surfaces to the factory,
        # which decides whether to fail open to memory.
        self._client.ping()

    def _key(self, session_id: str) -> str:
        return self._key_prefix + xxhash.xxh64(session_id.encode("utf-8")).hexdigest()

    def visit(self, session_id: str, now: float, ttl: float) -> bool:
        del now  # Redis TTL handles expiry; wall time is not needed.
        # Synchronous Redis call on the asyncio event loop thread (one RTT,
        # bounded by socket_timeout). Kept synchronous to match the sync
        # route_request path; the timeout caps its hot-path cost.
        try:
            existed = self._visit(keys=[self._key(session_id)], args=[max(1, int(ttl))])
            return bool(existed)
        except Exception as exc:  # noqa: BLE001 - fail open, never block routing
            cache_aware_returning_session_store_errors_total.labels(
                operation="visit"
            ).inc()
            logger.warning("returning-session redis visit failed: %s", exc)
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


class LocalCachedReturningSessionStore:
    """Per-replica LRU in front of a shared (redis) store.

    Skips the network round-trip for sessions that keep hitting THIS replica
    (e.g. LB session affinity): once a session is known returning locally, the
    backend is not queried again until the local entry approaches expiry. The
    backend stays authoritative for cross-replica first contact -- a session not
    in the local cache always queries the backend -- so the returning/first-visit
    classification is identical to using the backend directly; only the number
    of round-trips drops.

    Correctness notes:
    * The local cache only ever answers "returning" (True); "unknown locally"
      defers to the backend, so it never invents a returning request.
    * A local entry older than ``ttl`` is treated as expired and re-queried,
      matching the backend's TTL semantics (revisit after TTL == new session).
    * To stop an affinity-served session from aging out of the shared backend,
      the backend TTL is refreshed once the local entry passes
      ``refresh_fraction`` of the TTL (default mid-life), instead of every visit.
    """

    def __init__(
        self,
        backend: "ReturningSessionStore",
        max_size: int = 100_000,
        refresh_fraction: float = 0.5,
    ) -> None:
        self._backend = backend
        self._max_size = max_size
        self._refresh_fraction = refresh_fraction
        # session_id -> last time we wrote to the backend (for refresh + TTL).
        self._local: "OrderedDict[str, float]" = OrderedDict()

    def visit(self, session_id: str, now: float, ttl: float) -> bool:
        last = self._local.get(session_id)
        if last is not None and (now - last) < ttl:
            # Known returning on this replica; skip the backend round-trip,
            # only refreshing the shared TTL past mid-life so it does not expire.
            self._local.move_to_end(session_id)
            if (now - last) >= ttl * self._refresh_fraction:
                self._backend.visit(session_id, now, ttl)
                self._local[session_id] = now
            return True
        # Unknown (or locally expired): the backend is authoritative.
        is_returning = self._backend.visit(session_id, now, ttl)
        self._local[session_id] = now
        self._local.move_to_end(session_id)
        if self._max_size and len(self._local) > self._max_size:
            self._local.popitem(last=False)
        return is_returning

    def close(self) -> None:
        self._local.clear()
        self._backend.close()


def create_returning_session_store(
    store_type: str,
    redis_url: Optional[str] = None,
    redis_key_prefix: str = "vllm:returning-session:",
    redis_timeout: float = 0.05,
    max_size: int = 0,
    local_cache_size: int = 0,
) -> ReturningSessionStore:
    """Build the configured store.

    ``store_type == "redis"`` requires ``redis_url``. If Redis is unreachable at
    startup we fail HARD (raise): the operator explicitly asked for the shared
    backend, so silently downgrading to per-replica memory would quietly break
    the cross-replica returning metrics. In K8s the pod simply crash-loops until
    Redis is reachable. (Redis errors that occur *after* startup fail open per
    ``RedisReturningSessionStore.visit`` so live inference is never affected.)

    ``max_size`` caps the in-memory backend (0 = unbounded). ``local_cache_size``
    (> 0, redis only) fronts redis with a per-replica returning-id LRU to skip
    round-trips for affinity-served sessions.
    """
    if store_type == "redis":
        if not redis_url:
            raise ValueError(
                "cache_aware_returning_session_store=redis requires "
                "--cache-aware-returning-session-redis-url"
            )
        store: ReturningSessionStore = RedisReturningSessionStore(
            redis_url,
            key_prefix=redis_key_prefix,
            socket_timeout=redis_timeout,
        )
        if local_cache_size > 0:
            store = LocalCachedReturningSessionStore(store, max_size=local_cache_size)
            logger.info(
                "Returning-session store: redis (%s) + local cache (size=%d)",
                redis_url,
                local_cache_size,
            )
        else:
            logger.info("Returning-session store: redis (%s)", redis_url)
        return store
    logger.info("Returning-session store: memory (max_size=%d)", max_size)
    return MemoryReturningSessionStore(max_size=max_size)
