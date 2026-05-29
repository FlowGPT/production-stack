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
import math
import random
import threading
import time
from collections import Counter, deque
from typing import Deque, Dict, List, Tuple

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
from vllm_router.service_discovery import EndpointInfo
from vllm_router.services.metrics_service import (
    cache_aware_fallback_rate,
    cache_aware_fallback_reason_rate,
    cache_aware_stickiness_rate,
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
    threshold (``tolerate_waiting_requests``). Latency percentile thresholds are
    added on top of the same ``_violated_reasons`` seam.
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
    ):
        if hasattr(self, "_initialized"):
            return
        if session_key is None:
            raise ValueError(
                "CacheAwareLoadBalancingRouter must be initialized with a session_key"
            )
        self.session_key = session_key
        self.tolerate_waiting_requests = tolerate_waiting_requests
        # Latency percentile thresholds in seconds; <= 0 disables that check.
        # (snapshot_key, threshold) pairs evaluated in _violated_reasons.
        self.latency_thresholds = {
            "p50_ttft": p50_ttft_threshold,
            "p99_ttft": p99_ttft_threshold,
            "p50_e2e": p50_e2e_threshold,
            "p99_e2e": p99_e2e_threshold,
        }
        self.hash_ring = HashRing()

        # Sliding-window tally of routing decisions for stickiness / fallback
        # rate metrics. Running counters are kept in sync with the deque so the
        # request path stays O(1) amortized.
        self.stats_window = stats_window
        self._events: Deque[Tuple[float, bool, Tuple[str, ...]]] = deque()
        self._win_total = 0
        self._win_fallback = 0
        self._win_reason: Counter = Counter()
        self._initialized = True

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
    ) -> List[str]:
        """Return the threshold names ``url`` currently violates (empty if OK)."""
        reasons: List[str] = []
        stats = engine_stats.get(url)
        if (
            stats is not None
            and self.tolerate_waiting_requests > 0
            and stats.num_queuing_requests >= self.tolerate_waiting_requests
        ):
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
    ):
        stats = engine_stats.get(url)
        queue = stats.num_queuing_requests if stats is not None else 0
        p50_e2e = snapshot.get(url, {}).get("p50_e2e", -1)
        # Unknown latency (-1) is deprioritised in the tie-break.
        p50_e2e = p50_e2e if p50_e2e >= 0 else float("inf")
        return (queue, p50_e2e)

    def _select_fallback(
        self,
        initial_url: str,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        snapshot: Dict[str, Dict[str, float]],
    ) -> str:
        candidates = [
            e.url
            for e in endpoints
            if not self._violated_reasons(e.url, engine_stats, snapshot)
        ]
        if not candidates:
            # Every engine is overloaded: keep KV cache affinity.
            return initial_url
        return min(
            candidates,
            key=lambda u: self._fallback_sort_key(u, engine_stats, snapshot),
        )

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
    ) -> str:
        self._update_hash_ring(endpoints)
        session_id = request.headers.get(self.session_key, None)

        if session_id is None:
            # Production traffic always carries a session id; this defensive
            # branch simply picks the least-loaded engine.
            return min(
                (e.url for e in endpoints),
                key=lambda u: self._fallback_sort_key(u, engine_stats, snapshot),
            )

        initial_url = self.hash_ring.get_node(session_id)
        reasons = self._violated_reasons(initial_url, engine_stats, snapshot)
        if not reasons:
            self._record(time.time(), False, [])
            return initial_url
        self._record(time.time(), True, reasons)
        return self._select_fallback(initial_url, endpoints, engine_stats, snapshot)

    def _evict(self, now: float) -> None:
        cutoff = now - self.stats_window
        while self._events and self._events[0][0] < cutoff:
            _, was_fallback, reasons = self._events.popleft()
            self._win_total -= 1
            if was_fallback:
                self._win_fallback -= 1
            for reason in reasons:
                self._win_reason[reason] -= 1

    def _record(self, now: float, is_fallback: bool, reasons: List[str]) -> None:
        """Record one session routing decision and refresh the rate gauges."""
        reasons = tuple(reasons)
        self._events.append((now, is_fallback, reasons))
        self._win_total += 1
        if is_fallback:
            self._win_fallback += 1
        for reason in reasons:
            self._win_reason[reason] += 1
        self._evict(now)
        self._publish_gauges()

    def get_window_stats(self, now: float = None) -> Tuple[int, int, Dict[str, int]]:
        """Return (total, fallback, reason_counts) within the current window."""
        if now is not None:
            self._evict(now)
        return self._win_total, self._win_fallback, dict(self._win_reason)

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
        else:
            cache_aware_stickiness_rate.set(0)
            cache_aware_fallback_rate.set(0)
            for reason in self.REASONS:
                cache_aware_fallback_reason_rate.labels(reason=reason).set(0)


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
        return CacheAwareLoadBalancingRouter(
            session_key=kwargs.get("session_key"),
            tolerate_waiting_requests=kwargs.get("tolerate_waiting_requests", 20),
            p50_ttft_threshold=kwargs.get("p50_ttft_threshold", 0.0),
            p99_ttft_threshold=kwargs.get("p99_ttft_threshold", 0.0),
            p50_e2e_threshold=kwargs.get("p50_e2e_threshold", 0.0),
            p99_e2e_threshold=kwargs.get("p99_e2e_threshold", 0.0),
            stats_window=kwargs.get("stats_window", 30.0),
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
