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

import abc
import asyncio
import enum
import logging
import math
import random
import threading
import time
from collections import Counter, deque
from typing import Deque, Dict, List, Optional, Tuple

from fastapi import Request

try:
    from transformers import AutoTokenizer
except ImportError:
    pass

try:
    from lmcache.v1.cache_controller import controller_manager
    from lmcache.v1.cache_controller.message import (
        LookupMsg,
        QueryInstMsg,
    )
except ImportError:
    pass
from uhashring import HashRing

from vllm_router.log import init_logger
from vllm_router.routers.returning_session_store import (
    ReturningSessionStore,
    create_returning_session_store,
)
from vllm_router.service_discovery import EndpointInfo
from vllm_router.services.metrics_service import (
    cache_aware_fallback_rate,
    cache_aware_fallback_reason_rate,
    cache_aware_fallback_reason_total,
    cache_aware_fallback_total,
    cache_aware_first_visit_fallback_total,
    cache_aware_first_visit_request_ratio,
    cache_aware_first_visit_routed_total,
    cache_aware_first_visit_sticky_total,
    cache_aware_inflight_requests,
    cache_aware_returning_fallback_rate,
    cache_aware_returning_fallback_total,
    cache_aware_returning_request_ratio,
    cache_aware_returning_routed_total,
    cache_aware_returning_stickiness_rate,
    cache_aware_returning_sticky_total,
    cache_aware_stickiness_rate,
    cache_aware_sticky_total,
)
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.stats.request_stats import RequestStats, get_request_stats_monitor
from vllm_router.utils import SingletonABCMeta

logger = init_logger(__name__)


class RoutingLogic(str, enum.Enum):
    ROUND_ROBIN = "roundrobin"
    SESSION_BASED = "session"
    CACHE_AWARE_LOAD_BALANCING = "cache_aware_load_balancing"
    KVAWARE = "kvaware"
    PREFIXAWARE = "prefixaware"
    DISAGGREGATED_PREFILL = "disaggregated_prefill"


class RoutingInterface(metaclass=SingletonABCMeta):

    def _qps_routing(
        self, endpoints: List[EndpointInfo], request_stats: Dict[str, RequestStats]
    ) -> str:
        """
        Route the request to the appropriate engine URL based on the QPS of
        each engine

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
        """
        lowest_qps = float("inf")
        ret = None
        for info in endpoints:
            url = info.url
            if url not in request_stats:
                return url  # This engine does not have any requests
            request_stat = request_stats[url]
            if request_stat.qps < lowest_qps:
                lowest_qps = request_stat.qps
                ret = url
        return ret

    def _update_hash_ring(self, endpoints: List["EndpointInfo"]):
        """
        Update the hash ring with the current list of endpoints.
        """
        # Extract endpoint URLs
        endpoint_urls = [endpoint.url for endpoint in endpoints]

        # Get the current nodes in the hash ring
        current_nodes = set(self.hash_ring.get_nodes())

        # Convert the new endpoint URLs to a set for easy comparison
        new_nodes = set(endpoint_urls)

        # Remove nodes that are no longer in the list
        for node in current_nodes - new_nodes:
            self.hash_ring.remove_node(node)

        # Add new nodes that are not already in the hash ring
        for node in new_nodes - current_nodes:
            self.hash_ring.add_node(node)

    @abc.abstractmethod
    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Route the request to the appropriate engine URL

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
                the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
            request (Request): The incoming request
        """
        raise NotImplementedError

    def release_inflight(self, engine_url: str) -> None:
        """
        Notify the router that one request it dispatched to ``engine_url`` has
        finished. Overload-aware routers use this to keep their in-flight count
        accurate; the default is a no-op so the request path can call it on any
        router unconditionally.
        """
        return None

    def close_returning_session_store(self) -> None:
        """
        Release any returning-session store backend (e.g. Redis connections).
        Default is a no-op so app shutdown can call it on any router
        unconditionally; only the cache-aware router owns such a store.
        """
        return None


class RoundRobinRouter(RoutingInterface):
    # TODO (ApostaC): when available engines in the endpoints changes, the
    # algorithm may not be "perfectly" round-robin.
    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self.req_id = 0
        self._initialized = True

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Route the request to the appropriate engine URL using a simple
        round-robin algorithm

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
                the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
            request (Request): The incoming request
        """
        len_engines = len(endpoints)
        chosen = sorted(endpoints, key=lambda e: e.url)[self.req_id % len_engines]
        self.req_id += 1
        return chosen.url


class SessionRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL based on the session key
    in the request headers
    """

    def __init__(self, session_key: str = None):
        if hasattr(self, "_initialized"):
            return
        if session_key is None:
            raise ValueError("SessionRouter must be initialized with a session_key")
        self.session_key = session_key
        self.hash_ring = HashRing()
        self._initialized = True

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Route the request to the appropriate engine URL by the 'session id' in
        the request headers.
        If there is no session id in the request header, it will pick a server
        with lowest qps

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
                the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
            request (Request): The incoming request
        """
        session_id = request.headers.get(self.session_key, None)
        logger.debug(f"Got session id: {session_id}")

        # Update the hash ring with the current list of endpoints
        self._update_hash_ring(endpoints)

        if session_id is None:
            # Route based on QPS if no session ID is present
            url = self._qps_routing(endpoints, request_stats)
        else:
            # Use the hash ring to get the endpoint for the session ID
            url = self.hash_ring.get_node(session_id)

        return url


class CacheAwareLoadBalancingRouter(RoutingInterface):
    """
    Session-stickiness routing with overload protection.

    A request that carries a session id is normally routed to its hash-ring
    engine (KV cache affinity, same as SessionRouter). If that engine is
    overloaded it falls back to another engine. The fallback target is chosen
    deterministically by (num_queuing_requests asc, p50 end-to-end latency asc):
    queue length first, ties broken by the lower p50 e2e latency.

    Overload is decided by per-engine thresholds; this layer wires the queue
    threshold (``tolerate_waiting_requests``). When ``engine_max_concurrency`` is
    set, the queue signal also includes an instant estimate from this router's
    in-flight count (requests beyond engine capacity), so a burst triggers
    fallback before the periodic engine-stats scrape reports the queue. Latency
    percentile thresholds are added on top of the same ``_violated_reasons`` seam.

    Concurrency: the mutable state (the window deque/counters, the in-flight
    map, and the in-memory returning-session store) is only mutated from the
    asyncio event loop thread -- route_request and release_inflight both run
    there -- so no lock is used. Do not call these from a separate thread. The
    Redis returning-session store is itself thread-safe (connection-pooled), but
    is likewise only called from this path.
    """

    # All threshold names that can trigger a fallback (queue + latency).
    REASONS = ("queue", "p50_ttft", "p99_ttft", "p50_e2e", "p99_e2e")

    def __init__(
        self,
        session_key: str = None,
        tolerate_waiting_requests: int = 20,
        p50_ttft_threshold: float = 0.0,
        p99_ttft_threshold: float = 0.0,
        p50_e2e_threshold: float = 0.0,
        p99_e2e_threshold: float = 0.0,
        stats_window: float = 30.0,
        inflight_decay: float = 300.0,
        tie_tolerance: float = 0.0,
        engine_max_concurrency: int = 0,
        hash_vnodes: int = 1000,
        returning_session_ttl: float = 3600.0,
        returning_session_store: Optional[ReturningSessionStore] = None,
    ):
        if hasattr(self, "_initialized"):
            return
        if session_key is None:
            raise ValueError(
                "CacheAwareLoadBalancingRouter must be initialized with a session_key"
            )
        self.session_key = session_key
        self.tolerate_waiting_requests = tolerate_waiting_requests
        # Engines within this load gap of the best are treated as tied and the
        # choice among them is randomised (breaks cross-replica herding).
        self.tie_tolerance = tie_tolerance
        # Per-engine concurrency limit (= vLLM --max-num-seqs). When > 0, the
        # queue trigger also uses an instant estimate of the queue from this
        # router's in-flight count (requests beyond this capacity are assumed
        # queued), so a burst is caught before the periodic scrape reports it.
        # 0 disables the estimate -> trigger uses only the scraped queue.
        self.engine_max_concurrency = engine_max_concurrency
        # Latency percentile thresholds in seconds; <= 0 disables that check.
        # (snapshot_key, threshold) pairs evaluated in _violated_reasons.
        self.latency_thresholds = {
            "p50_ttft": p50_ttft_threshold,
            "p99_ttft": p99_ttft_threshold,
            "p50_e2e": p50_e2e_threshold,
            "p99_e2e": p99_e2e_threshold,
        }
        # Virtual nodes per physical engine on the consistent-hash ring. More
        # vnodes => smoother session-key distribution => tighter per-engine QPS
        # spread. Across 40 engines the default 160 leaves max/min ~1.4; 1000
        # brings it to ~1.1-1.2 (closer to 1.1 the more distinct active sessions
        # there are). Affinity is unchanged: a session still maps
        # deterministically to one engine. Ring is only rebuilt on topology
        # change, so the larger ring has negligible steady-state cost.
        self.hash_vnodes = hash_vnodes
        self.hash_ring = HashRing(vnodes=hash_vnodes)

        # Sliding-window tally of routing decisions for stickiness / fallback
        # rate metrics. Running counters are kept in sync with the deque so the
        # request path stays O(1) amortized.
        self.stats_window = stats_window
        # Each event also carries is_returning so the window can publish the
        # first-visit / returning request funnel alongside the base rates.
        self._events: Deque[Tuple[float, bool, Tuple[str, ...], bool]] = deque()
        self._win_total = 0
        self._win_fallback = 0
        self._win_reason: Counter = Counter()
        # Returning subset of the window (2nd+ visit of a session id).
        self._win_returning_total = 0
        self._win_returning_fallback = 0

        # Returning-session recognition (powers cache_aware_returning_* and
        # cache_aware_first_visit_* metrics only; never affects routing).
        self.returning_session_ttl = returning_session_ttl
        self._returning_session_store: ReturningSessionStore = (
            returning_session_store
            if returning_session_store is not None
            else create_returning_session_store("memory")
        )

        # Per-engine in-flight accounting. The scraped engine queue is up to
        # --engine-stats-interval stale, so during a burst every fallback would
        # otherwise pick the same lowest-queue engine (thundering herd).
        # We count requests this router dispatched but has not yet seen complete,
        # so concurrent fallbacks see each other and self-disperse, and a request
        # stays counted for its WHOLE lifetime (not a fixed decay) -- a long
        # request keeps loading its engine until it actually finishes.
        # inflight_decay is only a safety cap that drops entries whose completion
        # was never observed (e.g. client disconnect), to avoid leaking forever.
        self.inflight_decay = inflight_decay
        self._inflight: Dict[str, Deque[float]] = {}
        self._initialized = True

    def _record_dispatch(self, now: float, url: str) -> None:
        """Mark a request just routed to ``url`` as in-flight."""
        self._inflight.setdefault(url, deque()).append(now)

    def release_inflight(self, engine_url: str) -> None:
        """Notify that one request to ``engine_url`` finished (decrement in-flight)."""
        dq = self._inflight.get(engine_url)
        if dq:
            dq.popleft()

    def _pending_load(self, url: str, now: float) -> int:
        """
        In-flight request count for ``url`` (completion-accurate, TTL-capped).

        Side effect: lazily evicts entries older than the inflight_decay safety
        cap (dispatches whose completion was never observed).
        """
        dq = self._inflight.get(url)
        if not dq:
            return 0
        cutoff = now - self.inflight_decay
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)

    def _overload_snapshot(self) -> Dict[str, Dict[str, float]]:
        """Latency percentiles per engine; empty if the monitor is unavailable."""
        try:
            return get_request_stats_monitor().get_overload_snapshot(time.time())
        except ValueError:
            return {}

    def _violated_reasons(
        self,
        url: str,
        engine_stats: Dict[str, EngineStats],
        snapshot: Dict[str, Dict[str, float]],
        now: float,
    ) -> List[str]:
        """Return the threshold names ``url`` currently violates (empty if OK)."""
        reasons: List[str] = []
        stats = engine_stats.get(url)
        if stats is not None and self.tolerate_waiting_requests > 0:
            queue_signal = stats.num_queuing_requests
            if self.engine_max_concurrency > 0:
                # Instant queue estimate: this router's in-flight requests beyond
                # the engine's concurrency limit are assumed to be queued, even
                # if the (stale) scrape has not reported them yet. Capacity-based
                # so a high-concurrency engine is not falsely flagged during a
                # burst. Disabled (0) -> only the scraped queue is used.
                instant_queue = (
                    self._pending_load(url, now) - self.engine_max_concurrency
                )
                queue_signal = max(queue_signal, instant_queue)
            if queue_signal >= self.tolerate_waiting_requests:
                reasons.append("queue")
        engine_snapshot = snapshot.get(url, {})
        for key, threshold in self.latency_thresholds.items():
            if threshold > 0:
                # A snapshot value of -1 (no completed requests) is below any
                # positive threshold, so it never trips a fallback.
                if engine_snapshot.get(key, -1) >= threshold:
                    reasons.append(key)
        return reasons

    def _fallback_sort_key(
        self,
        url: str,
        engine_stats: Dict[str, EngineStats],
        snapshot: Dict[str, Dict[str, float]],
        now: float,
    ):
        stats = engine_stats.get(url)
        queue = stats.num_queuing_requests if stats is not None else 0
        # Scraped running count is the only load signal that aggregates ALL
        # router replicas (the engine reports its true load), so include it to
        # avoid cross-replica herding. It is stale by up to the scrape interval.
        running = stats.num_running_requests if stats is not None else 0
        # Add requests THIS router has in flight but the scrape has not yet seen,
        # so concurrent fallbacks within one replica also spread immediately.
        effective_load = queue + running + self._pending_load(url, now)
        p50_e2e = snapshot.get(url, {}).get("p50_e2e", -1)
        # Unknown latency (-1) is deprioritised in the tie-break.
        p50_e2e = p50_e2e if p50_e2e >= 0 else float("inf")
        return (effective_load, p50_e2e)

    def _select_fallback(
        self,
        initial_url: str,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        snapshot: Dict[str, Dict[str, float]],
        now: float,
    ) -> str:
        candidates = [
            e.url
            for e in endpoints
            if not self._violated_reasons(e.url, engine_stats, snapshot, now)
        ]
        if not candidates:
            # Every engine is overloaded: keep KV cache affinity.
            return initial_url

        keys = {
            u: self._fallback_sort_key(u, engine_stats, snapshot, now)
            for u in candidates
        }
        min_load = min(k[0] for k in keys.values())
        # Engines whose load is within tie_tolerance of the best are "equally
        # good"; a clear winner is still chosen deterministically.
        load_tied = [
            u for u in candidates if keys[u][0] <= min_load + self.tie_tolerance
        ]
        if len(load_tied) == 1:
            return load_tied[0]
        # Among load-tied engines prefer lower p50 e2e, then randomise the
        # remaining exact ties. Randomising genuine ties breaks the cross-replica
        # thundering herd -- independent replicas with identical stale views pick
        # different engines -- without any shared state.
        min_p50 = min(keys[u][1] for u in load_tied)
        best = [u for u in load_tied if keys[u][1] <= min_p50 + 1e-6]
        return random.choice(best) if len(best) > 1 else best[0]

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Route a request, sticking sessions to their hash-ring engine unless it
        is overloaded, in which case fall back to the best other engine.
        """
        return self._route_with_snapshot(
            endpoints, engine_stats, request, self._overload_snapshot()
        )

    def _route_with_snapshot(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request: Request,
        snapshot: Dict[str, Dict[str, float]],
        now: float = None,
    ) -> str:
        if now is None:
            now = time.time()
        self._update_hash_ring(endpoints)
        session_id = request.headers.get(self.session_key, None)

        if session_id is None:
            # Production traffic always carries a session id; this defensive
            # branch deliberately picks the least-loaded engine (not SessionRouter's
            # _qps_routing) because this router is overload- rather than qps-aware.
            # No stickiness/fallback metric is recorded: those describe session
            # routing only.
            chosen = min(
                (e.url for e in endpoints),
                key=lambda u: self._fallback_sort_key(u, engine_stats, snapshot, now),
            )
            self._record_dispatch(now, chosen)
            return chosen

        # Record the visit before routing so a session's first request is a
        # first visit and every later one (within the TTL) is "returning". This
        # only feeds metrics; the routing decision below is unchanged.
        is_returning = self._returning_session_store.visit(
            session_id, now, self.returning_session_ttl
        )
        initial_url = self.hash_ring.get_node(session_id)
        reasons = self._violated_reasons(initial_url, engine_stats, snapshot, now)
        if not reasons:
            self._record(now, False, [], is_returning)
            self._record_dispatch(now, initial_url)
            if logger.isEnabledFor(logging.DEBUG):
                # Guard: _engine_load_str is evaluated eagerly as an argument, so
                # only build it when DEBUG is actually enabled (this is the hot path).
                logger.debug(
                    "cache_aware sticky: session=%s engine=%s %s",
                    session_id,
                    initial_url,
                    self._engine_load_str(initial_url, engine_stats, snapshot, now),
                )
            return initial_url
        self._record(now, True, reasons, is_returning)
        chosen = self._select_fallback(
            initial_url, endpoints, engine_stats, snapshot, now
        )
        self._record_dispatch(now, chosen)
        if chosen == initial_url:
            logger.warning(
                "cache_aware fallback: session=%s sticky engine %s overloaded "
                "(reasons=%s %s) but all engines overloaded; staying on it",
                session_id,
                initial_url,
                reasons,
                self._engine_load_str(initial_url, engine_stats, snapshot, now),
            )
        else:
            logger.info(
                "cache_aware fallback: session=%s from=%s to=%s reasons=%s "
                "from_load=[%s] to_load=[%s]",
                session_id,
                initial_url,
                chosen,
                reasons,
                self._engine_load_str(initial_url, engine_stats, snapshot, now),
                self._engine_load_str(chosen, engine_stats, snapshot, now),
            )
        return chosen

    def _engine_load_str(
        self,
        url: str,
        engine_stats: Dict[str, EngineStats],
        snapshot: Dict[str, Dict[str, float]],
        now: float,
    ) -> str:
        """Human-readable load snapshot of one engine for log lines."""
        stats = engine_stats.get(url)
        queue = stats.num_queuing_requests if stats is not None else -1
        running = stats.num_running_requests if stats is not None else -1
        snap = snapshot.get(url, {})
        return (
            f"queue={queue} running={running} inflight={self._pending_load(url, now)} "
            f"p50_ttft={snap.get('p50_ttft', -1):.3f} "
            f"p99_ttft={snap.get('p99_ttft', -1):.3f} "
            f"p50_e2e={snap.get('p50_e2e', -1):.3f} "
            f"p99_e2e={snap.get('p99_e2e', -1):.3f}"
        )

    def _evict(self, now: float) -> None:
        cutoff = now - self.stats_window
        while self._events and self._events[0][0] < cutoff:
            _, was_fallback, reasons, was_returning = self._events.popleft()
            self._win_total -= 1
            if was_fallback:
                self._win_fallback -= 1
            for reason in reasons:
                self._win_reason[reason] -= 1
            if was_returning:
                self._win_returning_total -= 1
                if was_fallback:
                    self._win_returning_fallback -= 1

    def _record(
        self,
        now: float,
        is_fallback: bool,
        reasons: List[str],
        is_returning: bool = False,
    ) -> None:
        """Record one session routing decision: update window gauges and counters.

        ``is_returning`` partitions the decision into the first-visit (N) or
        returning (R) bucket of the request funnel.
        """
        reasons = tuple(reasons)
        self._events.append((now, is_fallback, reasons, is_returning))
        self._win_total += 1
        if is_fallback:
            self._win_fallback += 1
        for reason in reasons:
            self._win_reason[reason] += 1
        if is_returning:
            self._win_returning_total += 1
            if is_fallback:
                self._win_returning_fallback += 1
        # Cumulative counters (monotonic; Prometheus computes rates over any window).
        if is_fallback:
            cache_aware_fallback_total.inc()
            for reason in reasons:
                cache_aware_fallback_reason_total.labels(reason=reason).inc()
        else:
            cache_aware_sticky_total.inc()
        # Request funnel: each decision is exactly one of first-visit / returning.
        if is_returning:
            cache_aware_returning_routed_total.inc()
            if is_fallback:
                cache_aware_returning_fallback_total.inc()
            else:
                cache_aware_returning_sticky_total.inc()
        else:
            cache_aware_first_visit_routed_total.inc()
            if is_fallback:
                cache_aware_first_visit_fallback_total.inc()
            else:
                cache_aware_first_visit_sticky_total.inc()
        self._evict(now)
        self._publish_gauges()

    def get_window_stats(self, now: float = None) -> Tuple[int, int, Dict[str, int]]:
        """Return (total, fallback, reason_counts) within the current window."""
        if now is not None:
            self._evict(now)
        return self._win_total, self._win_fallback, dict(self._win_reason)

    def refresh_window_metrics(self, now: float = None) -> None:
        """
        Evict expired events and republish the rate gauges. Called on /metrics
        scrape so the rates keep decaying to zero when traffic stops, instead of
        freezing at the last recorded value. Also publishes per-engine in-flight.
        """
        now = time.time() if now is None else now
        self._evict(now)
        self._publish_gauges()
        for url in list(self._inflight.keys()):
            cache_aware_inflight_requests.labels(server=url).set(
                self._pending_load(url, now)
            )

    def close_returning_session_store(self) -> None:
        """Release the returning-session store backend (e.g. Redis connections)."""
        store = getattr(self, "_returning_session_store", None)
        if store is not None:
            store.close()

    def _publish_gauges(self) -> None:
        total = self._win_total
        if total > 0:
            sticky = total - self._win_fallback
            cache_aware_stickiness_rate.set(sticky / total)
            cache_aware_fallback_rate.set(self._win_fallback / total)
            for reason in self.REASONS:
                cache_aware_fallback_reason_rate.labels(reason=reason).set(
                    self._win_reason.get(reason, 0) / total
                )
            # Request funnel: split all session requests into first-visit (N)
            # and returning (R), with |U| = |N| + |R|.
            returning_total = self._win_returning_total
            cache_aware_returning_request_ratio.set(returning_total / total)
            cache_aware_first_visit_request_ratio.set((total - returning_total) / total)
        else:
            cache_aware_stickiness_rate.set(0)
            cache_aware_fallback_rate.set(0)
            for reason in self.REASONS:
                cache_aware_fallback_reason_rate.labels(reason=reason).set(0)
            cache_aware_returning_request_ratio.set(0)
            cache_aware_first_visit_request_ratio.set(0)

        # Stickiness / fallback rate computed ONLY over the returning subset R,
        # so one-shot first visits do not dilute the returning-user view.
        returning_total = self._win_returning_total
        if returning_total > 0:
            returning_sticky = returning_total - self._win_returning_fallback
            cache_aware_returning_stickiness_rate.set(
                returning_sticky / returning_total
            )
            cache_aware_returning_fallback_rate.set(
                self._win_returning_fallback / returning_total
            )
        else:
            cache_aware_returning_stickiness_rate.set(0)
            cache_aware_returning_fallback_rate.set(0)


class KvawareRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL by where the KV cache
    of the longest prefix match is found.
    """

    def __init__(
        self,
        lmcache_controller_port: int,
        session_key: str,
        kv_aware_threshold: int = 2000,
    ):
        self.lmcache_controller_port = lmcache_controller_port
        logger.info(
            f"Initializing KvawareRouter with port: {self.lmcache_controller_port}"
        )
        self.kv_manager = controller_manager.LMCacheControllerManager(
            f"0.0.0.0:{self.lmcache_controller_port}"
        )
        self.req_id = 0
        self.instance_id_to_ip = {}
        self.session_key = session_key
        self.hash_ring = HashRing()
        self.tokenizer = None
        self.threshold = kv_aware_threshold

    def start_kv_manager(self):
        """
        Start the kv manager
        """
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.kv_manager.start_all(), self.loop)

    def query_manager(self, msg) -> str:
        """
        Get the instance id for the given message
        """
        instance_id = self.kv_manager.handle_orchestration_message(msg)
        return instance_id

    async def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to the appropriate engine URL by where the KV cache
        of the longest prefix match is found.
        If there is no session id in the request header, it will pick a server
        with round robin.

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
               the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
               indicating the request-level performance of each engine
            request (Request): The incoming request
            request_json (Dict): The request body (needed for finding the
            longest prefix match)
        """
        if self.tokenizer is None:
            self.tokenizer = AutoTokenizer.from_pretrained(endpoints[0].model_names[0])
        url = endpoints[0].url + "/tokenize"
        # TODO (Yuhan): Handle chat completions
        token_ids = self.tokenizer.encode(request_json["prompt"])
        msg = LookupMsg(tokens=token_ids)
        instance_id = await self.query_manager(msg)
        matched_tokens = math.inf
        if len(list(instance_id.layout_info.keys())) > 0:
            matched_instance_id = list(instance_id.layout_info.keys())[
                0
            ]  # Get the first key
            matched_tokens = instance_id.layout_info[matched_instance_id][1]

        if (
            instance_id is None
            or len(instance_id.layout_info) == 0
            or matched_tokens < max(len(token_ids) - self.threshold, 0)
        ):

            session_id = request.headers.get(self.session_key, None)
            logger.debug(f"Got session id: {session_id}")

            # Update the hash ring with the current list of endpoints
            self._update_hash_ring(endpoints)

            if session_id is None:
                # Route based on QPS if no session ID is present
                url = self._qps_routing(endpoints, request_stats)
            else:
                # Use the hash ring to get the endpoint for the session ID
                url = self.hash_ring.get_node(session_id)
            return url
        else:
            queried_instance_ids = [info for info in instance_id.layout_info]
            if queried_instance_ids[0] not in self.instance_id_to_ip:
                for endpoint in endpoints:
                    query_message = QueryInstMsg(
                        ip=endpoint.url.split(f":{endpoint.url.split(':')[-1]}")[
                            0
                        ].split("//")[1]
                    )
                    endpoint_instance_id = await self.query_manager(query_message)

                    self.instance_id_to_ip[endpoint_instance_id.instance_id] = (
                        endpoint.url
                    )
                logger.info(f"Instance id to ip: {self.instance_id_to_ip}")
            logger.info(
                f"Routing request to {queried_instance_ids[0]} found by kvaware router"
            )
            return self.instance_id_to_ip[queried_instance_ids[0]]


class PrefixAwareRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL by where the longest
    prefix match is found.

    In this class, we assume that there is no eviction of prefix cache.
    """

    def __init__(self: int):
        if hasattr(self, "_initialized"):
            return
        from vllm_router.prefix.hashtrie import HashTrie

        self.hashtrie = HashTrie()
        self._initialized = True

    async def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to the appropriate engine URL by where the longest
        prefix match is found.

        In this routing logic, we do not consider the eviction of prefix cache.

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
               the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
               indicating the request-level performance of each engine
            request (Request): The incoming request
            request_json (Dict): The request body (needed for finding the
            longest prefix match)
        """

        # Handle chat completions
        if "messages" in request_json:
            # Get the last message from the messages array
            messages = request_json["messages"]
            if messages:
                # Concatenate all message content
                prompt_parts = []
                for message in messages:
                    content = message.get("content", "")
                    if isinstance(content, list):
                        # Handle multimodal messages
                        text_content = " ".join(
                            part.get("text", "")
                            for part in content
                            if part.get("type") == "text"
                        )
                        prompt_parts.append(text_content)
                    elif content is not None:
                        prompt_parts.append(content)
                prompt = "\n".join(prompt_parts)
            else:
                prompt = ""
        else:
            # Handle regular completions
            prompt = request_json["prompt"]

        available_endpoints = set(endpoint.url for endpoint in endpoints)
        _, matched_endpoint = await self.hashtrie.longest_prefix_match(
            prompt, available_endpoints
        )

        selected_endpoint = random.choice(list(matched_endpoint))

        await self.hashtrie.insert(prompt, selected_endpoint)

        return selected_endpoint


class DisaggregatedPrefillRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL by handling prefill and decode operations sequentially.
    First request goes to prefill endpoint, then second request goes to decode endpoint.
    """

    def __init__(self, prefill_model_labels: List[str], decode_model_labels: List[str]):
        self.prefill_model_labels = prefill_model_labels
        self.decode_model_labels = decode_model_labels
        self.request_cache = {}  # Cache to store prefill results

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to appropriate endpoints for prefill and decode operations.
        First request goes to prefill endpoint, then second request goes to decode endpoint.
        """
        # Find prefill and decode endpoints
        is_prefill = request_json.get("max_tokens", 0) == 1
        if is_prefill:
            logger.info("Prefill request")
        else:
            logger.info("Decode request")

        # Find endpoints with matching model labels
        prefiller_endpoints = [
            e for e in endpoints if e.model_label in self.prefill_model_labels
        ]
        decoder_endpoints = [
            e for e in endpoints if e.model_label in self.decode_model_labels
        ]
        if is_prefill:
            return prefiller_endpoints[0].url
        else:
            return decoder_endpoints[0].url


# Instead of managing a global _global_router, we can define the initialization functions as:
def initialize_routing_logic(
    routing_logic: RoutingLogic, *args, **kwargs
) -> RoutingInterface:
    if routing_logic == RoutingLogic.ROUND_ROBIN:
        logger.info("Initializing round-robin routing logic")
        return RoundRobinRouter()
    elif routing_logic == RoutingLogic.SESSION_BASED:
        logger.info(f"Initializing session-based routing logic with kwargs: {kwargs}")
        return SessionRouter(kwargs.get("session_key"))
    elif routing_logic == RoutingLogic.CACHE_AWARE_LOAD_BALANCING:
        logger.info(
            f"Initializing cache-aware load balancing routing logic with kwargs: {kwargs}"
        )
        # Pass only the keys that were supplied so the defaults live in exactly
        # one place: the CacheAwareLoadBalancingRouter.__init__ signature.
        optional = (
            "tolerate_waiting_requests",
            "p50_ttft_threshold",
            "p99_ttft_threshold",
            "p50_e2e_threshold",
            "p99_e2e_threshold",
            "stats_window",
            "inflight_decay",
            "tie_tolerance",
            "engine_max_concurrency",
            "hash_vnodes",
            "returning_session_ttl",
        )
        router_kwargs = {k: kwargs[k] for k in optional if k in kwargs}
        # Build the returning-session store from the *_store kwargs so the
        # router itself stays agnostic of the backend choice.
        if "returning_session_store" not in router_kwargs:
            router_kwargs["returning_session_store"] = create_returning_session_store(
                kwargs.get("returning_session_store_type", "memory"),
                redis_url=kwargs.get("returning_session_redis_url"),
                redis_key_prefix=kwargs.get(
                    "returning_session_redis_key_prefix",
                    "vllm:returning-session:",
                ),
                redis_timeout=kwargs.get("returning_session_redis_timeout", 0.05),
                max_size=kwargs.get("returning_session_max_size", 0),
                local_cache_size=kwargs.get("returning_session_local_cache_size", 0),
            )
        return CacheAwareLoadBalancingRouter(
            session_key=kwargs.get("session_key"), **router_kwargs
        )
    elif routing_logic == RoutingLogic.KVAWARE:
        logger.info("Initializing kvaware routing logic")
        router = KvawareRouter(
            kwargs.get("lmcache_controller_port"),
            kwargs.get("session_key"),
            kwargs.get("kv_aware_threshold"),
        )
        router.start_kv_manager()
        return router
    elif routing_logic == RoutingLogic.PREFIXAWARE:
        logger.info("Initializing prefix-aware routing logic")
        return PrefixAwareRouter()
    elif routing_logic == RoutingLogic.DISAGGREGATED_PREFILL:
        logger.info("Initializing disaggregated prefill routing logic")
        return DisaggregatedPrefillRouter(
            kwargs.get("prefill_model_labels"), kwargs.get("decode_model_labels")
        )
    else:
        raise ValueError(f"Invalid routing logic {routing_logic}")


def reconfigure_routing_logic(
    routing_logic: RoutingLogic, *args, **kwargs
) -> RoutingInterface:
    # Remove the existing routers from the singleton registry
    for cls in (
        SessionRouter,
        RoundRobinRouter,
        CacheAwareLoadBalancingRouter,
        KvawareRouter,
        DisaggregatedPrefillRouter,
    ):
        if cls in SingletonABCMeta._instances:
            del SingletonABCMeta._instances[cls]
    return initialize_routing_logic(routing_logic, *args, **kwargs)


def get_routing_logic() -> RoutingInterface:
    # Look up in our singleton registry which router (if any) has been created.
    for cls in (
        SessionRouter,
        RoundRobinRouter,
        CacheAwareLoadBalancingRouter,
        KvawareRouter,
        PrefixAwareRouter,
        DisaggregatedPrefillRouter,
    ):
        if cls in SingletonABCMeta._instances:
            return cls()
    raise ValueError("The global router has not been initialized")
