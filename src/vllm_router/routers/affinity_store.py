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

"""Pluggable store for the load_balanced_affinity router's session->engine map.

Unlike the returning-session store (which only records a boolean "seen before"),
this store maps a session id to the engine it is currently pinned to, so the
router can route a returning session back to the same engine for prefix-cache
reuse.

Two backends:

* ``memory`` (default): a process-local ``OrderedDict``. Each router worker /
  replica only knows the sessions it placed itself, so with multiple workers or
  replicas a session hitting a different one is re-placed by load -- affinity is
  diluted. TTL + LRU bound memory.
* ``redis``: the map is shared across all workers and replicas through one Redis
  instance, so any worker routing a returning session reads the same pinned
  engine. Redis TTL evicts keys. One round-trip to read (``GET``) and one to
  write (``SET EX``); both fail open (treated as "no memory" -> a fresh
  placement) so a Redis outage never blocks routing, it only degrades affinity
  back to plain load balancing.

Deliberately NO local L1 cache in front of redis (unlike the returning-session
store): the returning flag is monotonic and safe to cache, but the pinned engine
*changes* whenever a worker sheds a session to a lighter engine. A stale local
entry would route to the old engine while redis already holds the new one,
reintroducing the very cross-worker inconsistency redis is meant to remove. Redis
is therefore the single source of truth and is read on every returning request.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional, Protocol, Tuple

import xxhash

from vllm_router.log import init_logger
from vllm_router.services.metrics_service import (
    load_balanced_affinity_store_errors_total,
)

logger = init_logger(__name__)


class AffinityStore(Protocol):
    """Backend mapping a session id to its currently pinned engine url."""

    def get(self, session_id: str, now: float, ttl: float) -> Optional[str]:
        """Return the engine url remembered for ``session_id`` (or None if the
        session is unknown or its entry has expired)."""
        ...

    def put(self, session_id: str, engine_url: str, now: float, ttl: float) -> None:
        """Remember (or refresh) ``session_id`` -> ``engine_url`` for ``ttl``
        seconds."""
        ...

    def close(self) -> None:
        """Release any backend resources (no-op for in-memory)."""
        ...


class MemoryAffinityStore:
    """Process-local session->engine map. Suitable for a single worker.

    Backed by an ``OrderedDict`` keyed by session id, value = (engine_url,
    last-seen time). ``now`` only moves forward, so re-inserting at the end keeps
    the dict ordered oldest-first: TTL eviction pops expired entries from the
    front (amortized O(1) per call) and ``max_size`` (> 0) bounds memory with an
    LRU cap on top.
    """

    def __init__(self, max_size: int = 0) -> None:
        self._store: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._max_size = max_size

    def get(self, session_id: str, now: float, ttl: float) -> Optional[str]:
        self._evict(now, ttl)
        entry = self._store.get(session_id)
        if entry is None:
            return None
        url, ts = entry
        if ttl > 0 and (now - ts) >= ttl:
            del self._store[session_id]
            return None
        return url

    def put(self, session_id: str, engine_url: str, now: float, ttl: float) -> None:
        self._store[session_id] = (engine_url, now)
        self._store.move_to_end(session_id)
        if self._max_size and len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def _evict(self, now: float, ttl: float) -> None:
        if ttl <= 0:
            return
        cutoff = now - ttl
        while self._store:
            sid, (_, ts) = next(iter(self._store.items()))
            if ts >= cutoff:
                break
            del self._store[sid]

    def close(self) -> None:
        self._store.clear()


class RedisAffinityStore:
    """Redis-backed session->engine map shared across workers and replicas.

    Keys are ``{key_prefix}{xxhash(session_id)}`` so arbitrarily long or exotic
    session ids map to bounded, safe keys; the value is the engine url. Both
    operations fail open on any Redis error (read -> None, write -> ignored) and
    increment an error counter, so routing degrades to plain load balancing
    rather than blocking.

    Calls are synchronous on the asyncio event-loop thread (like
    returning_session_store); ``socket_timeout`` caps their hot-path cost and the
    write-throttle below removes most SETs.

    Circuit breaker: a hung/blackholed redis makes every call block for the full
    ``socket_timeout``, and with many concurrent requests those blocks serialize
    on the single event-loop thread into seconds of scheduling lag (observed
    ~3.6s under a TCP blackhole). After ``breaker_threshold`` consecutive
    failures the breaker opens for ``breaker_cooldown`` seconds, during which
    calls skip redis entirely (fail-open, zero blocking); one probe per cooldown
    half-opens it, so a recovered redis is picked up automatically. This bounds
    the loop cost during an outage to a few timeouts per cooldown instead of one
    per request. Set ``breaker_threshold=0`` to disable.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "vllm:lb-affinity:",
        socket_timeout: float = 0.05,
        refresh_fraction: float = 0.5,
        write_cache_size: int = 100_000,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 5.0,
    ) -> None:
        import redis

        self._key_prefix = key_prefix
        self._client = redis.Redis.from_url(
            redis_url,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
        )
        # Write throttling (see _can_skip_write): skip the redis SET that only
        # refreshes an unchanged engine's TTL, halving the hot path to one GET on
        # hits. 0 disables.
        self._refresh_fraction = refresh_fraction
        self._write_cache_size = write_cache_size
        # session_id -> (engine_url_last_written, last_write_time). Bounded LRU;
        # only records successful writes (a failed set is retried next time).
        self._writes: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        # Circuit-breaker state (see class docstring).
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._consec_failures = 0
        self._breaker_open_until = 0.0
        # No connect here: redis-py connects lazily on first command, and get/put
        # fail open. So a redis that is down at startup does not break the store;
        # it self-heals once redis is reachable. The factory probes once to warn
        # (or hard-fail if the operator opted in).

    def ping(self) -> None:
        """Probe connectivity; raises if redis is unreachable."""
        self._client.ping()

    def _key(self, session_id: str) -> str:
        return self._key_prefix + xxhash.xxh64(session_id.encode("utf-8")).hexdigest()

    def _breaker_open(self, now: float) -> bool:
        return self._breaker_threshold > 0 and now < self._breaker_open_until

    def _on_failure(self, now: float, op: str, exc: Exception) -> None:
        load_balanced_affinity_store_errors_total.labels(operation=op).inc()
        self._consec_failures += 1
        if (
            self._breaker_threshold > 0
            and self._consec_failures >= self._breaker_threshold
        ):
            self._breaker_open_until = now + self._breaker_cooldown
        logger.warning("lb-affinity redis %s failed: %s", op, exc)

    def _on_success(self) -> None:
        self._consec_failures = 0
        self._breaker_open_until = 0.0

    def get(self, session_id: str, now: float, ttl: float) -> Optional[str]:
        del ttl  # Redis TTL handles expiry; only the value is read here.
        if self._breaker_open(now):
            return None
        try:
            val = self._client.get(self._key(session_id))
        except Exception as exc:  # noqa: BLE001 - fail open, never block routing
            self._on_failure(now, "get", exc)
            return None
        self._on_success()
        if val is None:
            return None
        return val.decode("utf-8") if isinstance(val, bytes) else str(val)

    def put(self, session_id: str, engine_url: str, now: float, ttl: float) -> None:
        if self._breaker_open(now):
            return
        if self._can_skip_write(session_id, engine_url, now, ttl):
            self._writes.move_to_end(session_id)
            return
        try:
            self._client.set(self._key(session_id), engine_url, ex=max(1, int(ttl)))
        except Exception as exc:  # noqa: BLE001 - fail open, never block routing
            self._on_failure(now, "put", exc)
            return
        self._on_success()
        self._record_write(session_id, engine_url, now)

    def _can_skip_write(
        self, session_id: str, engine_url: str, now: float, ttl: float
    ) -> bool:
        if self._refresh_fraction <= 0:
            return False
        last = self._writes.get(session_id)
        # Skip only a TTL refresh of the SAME engine still within the refresh
        # window; a changed engine must write through so peers see the new home.
        return (
            last is not None
            and last[0] == engine_url
            and (now - last[1]) < ttl * self._refresh_fraction
        )

    def _record_write(self, session_id: str, engine_url: str, now: float) -> None:
        self._writes[session_id] = (engine_url, now)
        self._writes.move_to_end(session_id)
        if self._write_cache_size and len(self._writes) > self._write_cache_size:
            self._writes.popitem(last=False)

    def close(self) -> None:
        self._writes.clear()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def create_affinity_store(
    store_type: str,
    redis_url: Optional[str] = None,
    redis_key_prefix: str = "vllm:lb-affinity:",
    redis_timeout: float = 0.05,
    redis_refresh_fraction: float = 0.5,
    redis_required: bool = False,
    max_size: int = 0,
) -> AffinityStore:
    """Build the configured affinity store.

    ``store_type == "redis"`` requires ``redis_url``. Redis is NOT a hard startup
    dependency by default: if it is unreachable at startup we log a warning and
    still return the redis store, which fail-opens (routing degrades to plain
    load balancing) and self-heals -- affinity sharing resumes automatically once
    redis becomes reachable, no restart needed. Set ``redis_required=True`` to
    instead hard-fail (crash-loop) at startup for strict shared-state guarantees.
    (Redis errors after startup always fail open so live inference is never
    affected.)

    ``max_size`` caps the in-memory backend (0 = unbounded, TTL eviction only).
    """
    if store_type == "redis":
        if not redis_url:
            raise ValueError("lb_affinity_store=redis requires --lb-affinity-redis-url")
        store = RedisAffinityStore(
            redis_url,
            key_prefix=redis_key_prefix,
            socket_timeout=redis_timeout,
            refresh_fraction=redis_refresh_fraction,
        )
        try:
            store.ping()
            logger.info(
                "load_balanced_affinity store: redis (%s), refresh_fraction=%.2f",
                redis_url,
                redis_refresh_fraction,
            )
        except Exception as exc:  # noqa: BLE001
            if redis_required:
                raise
            logger.warning(
                "load_balanced_affinity store: redis (%s) unreachable at startup "
                "(%s); starting fail-open (plain load balancing) and will use "
                "redis once reachable. Pass --lb-affinity-redis-required to "
                "hard-fail instead.",
                redis_url,
                exc,
            )
        return store
    logger.info("load_balanced_affinity store: memory (max_size=%d)", max_size)
    return MemoryAffinityStore(max_size=max_size)
