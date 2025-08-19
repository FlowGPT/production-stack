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
import os
import random
import socket
import threading
from typing import Dict, List, Tuple, Optional, Callable
import heapq
import time

import requests
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
        QueryInstRetMsg,
    )
except ImportError:
    pass
from uhashring import HashRing

from vllm_router.log import init_logger
from vllm_router.service_discovery import EndpointInfo
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.stats.request_stats import RequestStats
from vllm_router.utils import SingletonABCMeta

logger = init_logger(__name__)


class RoutingLogic(str, enum.Enum):
    ROUND_ROBIN = "roundrobin"
    SESSION_BASED = "session"
    CACHE_AWARE_LOAD_BALANCING = "cache_aware_load_balancing"
    KVAWARE = "kvaware"
    PREFIXAWARE = "prefixaware"
    DISAGGREGATED_PREFILL = "disaggregated_prefill"
    ELRAR = "elrar"


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
        # logger.debug(f"Updating hash ring with endpoints: {endpoints}")
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
        routing_method = "round_robin"
        return chosen.url, routing_method


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

            # If the initial engine is not found in engine_stats
            if url not in engine_stats:
                logger.warning(
                    f"Engine {url} not found in engine_stats"
                )

        routing_method = "session_based"
        return url, routing_method


class CacheAwareLoadBalancingRouter(RoutingInterface):
    """
    Routing algorithm that combines load balancing with KV Cache hit rate awareness

    This algorithm considers three key factors:
    1. Engine load (number of queuing requests, number of running requests)
    2. Estimated KV cache hit rate (for specific sessions)
    """

    def __init__(self, session_key: str = None, tolerate_waiting_requests: int = 20):
        if hasattr(self, "_initialized"):
            return

        if session_key is None:
            raise ValueError(
                "CacheAwareLoadBalancingRouter must be initialized with a session_key"
            )

        self.session_key = session_key
        self.tolerate_waiting_requests = tolerate_waiting_requests

        # Initialize hash ring
        self.hash_ring = HashRing()

        self.req_id = 0  # Request ID, used for round-robin selection

        self._initialized = True

    def _calculate_engine_load_score(
        self,
        engine_url: str,
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
    ) -> float:
        """
        Calculate engine load score

        Lower score indicates lighter engine load

        Load factors: load score (running requests * 0.02 + queuing requests * 0.1)
        """
        if engine_url not in engine_stats:
            return 0.0  # No statistics available, assume load is 0

        # Get engine statistics
        stats = engine_stats[engine_url]

        # Basic load factors: running requests and queuing requests
        running_load = stats.num_running_requests * 0.02  # Running requests weight
        queuing_load = (
            stats.num_queuing_requests * 0.1
        )  # Queuing requests weight (slightly higher)

        # Calculate total load score
        total_load_score = running_load + queuing_load

        return total_load_score

    def _select_best_engine(
        self,
        session_id: str,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
    ) -> Tuple[str, str]:
        """
        Select the best engine
        1. First determine which engine the request corresponds to based on hash_ring
        2. Check the queue situation of that engine (num_queuing_requests)
        3. If there are queuing requests (>tolerate_waiting_requests), try to find an engine without queue
        4. If all engines have queues, assign engine based on probability
        5. If the initial engine has no queuing requests, use session-based routing (i.e., hash_ring result)
        """
        # Update hash ring to reflect currently available endpoints
        self._update_hash_ring(endpoints)

        # Use hash_ring to get the initial engine_url
        initial_engine_url = self.hash_ring.get_node(session_id)

        # If the initial engine is not found in engine_stats
        if initial_engine_url not in engine_stats:
            logger.warning(
                f"Engine {initial_engine_url} not found in engine_stats"
            )
            return initial_engine_url, "cache_aware"

        # Check the queuing situation of the initial engine
        if (
            engine_stats[initial_engine_url].num_queuing_requests
            < self.tolerate_waiting_requests
        ):
            # If queuing requests are less than tolerate_waiting_requests, use it directly
            logger.debug(
                f"Session {session_id} initial engine waiting requests < {self.tolerate_waiting_requests}, route to: {initial_engine_url}"
            )
            return initial_engine_url, "cache_aware"

        # Try to find engines without queue
        engines_without_queue = []
        for info in endpoints:
            url = info.url
            # Add boundary check for engine_stats
            if url in engine_stats and engine_stats[url].num_queuing_requests == 0:
                engines_without_queue.append(url)

        # If there are engines without queue, randomly select one
        if engines_without_queue:
            selected_engine = random.choice(engines_without_queue)
            logger.debug(
                f"Session {session_id} redirect to no queue engine: {selected_engine}"
            )
            return selected_engine, "redirect_to_no_queue_engine"

        # All engines have queues, select one based on improved probability calculation
        routing_method = "probability_based"
        
        # Filter endpoints that have engine stats
        valid_endpoints = [info for info in endpoints if info.url in engine_stats]
        if not valid_endpoints:
            # Fallback to initial engine if no valid stats available
            logger.warning("No valid engine stats available, falling back to initial engine")
            return initial_engine_url, "cache_aware_fallback"
        
        # Calculate total queue length from valid endpoints only
        total_queue_length = sum(
            engine_stats[info.url].num_queuing_requests
            for info in valid_endpoints
        )
        
        # Fixed probability calculation: inverse of normalized queue length
        queue_lengths = [engine_stats[info.url].num_queuing_requests for info in valid_endpoints]
        max_queue = max(queue_lengths)
        
        # Calculate inverse weights (higher weight for lower queue length)
        # Add small epsilon to avoid division by zero
        epsilon = 0.1
        inverse_weights = [(max_queue - queue_len + epsilon) for queue_len in queue_lengths]
        total_weight = sum(inverse_weights)
        
        # Normalize to probabilities
        probabilities = [weight / total_weight for weight in inverse_weights]

        selected_engine = random.choices(
            [info.url for info in valid_endpoints], weights=probabilities
        )[0]

        logger.debug(
            f"Session {session_id} probability based routing to: {selected_engine}, "
            f"queue_lengths: {queue_lengths}, probabilities: {[f'{p:.3f}' for p in probabilities]}"
        )
        return selected_engine, routing_method

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Intelligent request routing, combining load awareness and cache hit rate prediction

        For requests with session ID, intelligent selection is made based on KV cache hit rate prediction and load conditions
        For requests without session ID, engine selection is purely based on load balancing
        """
        # Extract session ID
        session_id = request.headers.get(self.session_key, None)
        logger.debug(f"Got session id: {session_id}")

        routing_method = "load_balancing"

        if session_id is None:
            # No session ID, use pure load balancing strategy
            engine_url = min(
                [e.url for e in endpoints],
                key=lambda url: self._calculate_engine_load_score(
                    url, engine_stats, request_stats
                ),
            )
            routing_method = "load_based"
        else:
            # Has session ID, use comprehensive strategy
            engine_url, routing_method = self._select_best_engine(
                session_id, endpoints, engine_stats, request_stats
            )

        return engine_url, routing_method


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


class ELRARRouter(RoutingInterface):
    """
    ELRAR Router: 基于引擎内实时调度状态的智能路由。

    环境变量控制：
      - VLLM_ELRAR_SLO_MS: 请求默认SLO TPOT (默认: 50)
      - VLLM_ELRAR_STALE_MS: 引擎状态新鲜度阈值 (默认: 3s)
      - VLLM_ELRAR_KV_AFFINITY_WINDOW: KV亲和有效窗口 (默认: 300s)
      - VLLM_ELRAR_KV_BETA: KV命中得分 (默认: 1.0)
      - VLLM_ELRAR_LOAD_SCALE_S: 负载归一化秒级缩放 (默认: 1.0)
      - VLLM_ELRAR_W1..W4: 打分权重 (latency, load, mode, kv)
    """

    def __init__(self, session_key: str = None):
        if hasattr(self, "_initialized"):
            return
        # 会话键名
        self.session_key = session_key
        # SLO与亲和配置
        self.slo_ms = float(os.getenv("VLLM_ELRAR_SLO_MS", "50"))
        self.stale_ms = int(os.getenv("VLLM_ELRAR_STALE_MS", "3000"))
        self.kv_affinity_window = int(os.getenv("VLLM_ELRAR_KV_AFFINITY_WINDOW", "300"))
        self.kv_beta = float(os.getenv("VLLM_ELRAR_KV_BETA", "1.0"))
        self.load_scale_s = float(os.getenv("VLLM_ELRAR_LOAD_SCALE_S", "1.0"))
        # 路由打分权重
        def _get_w(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except Exception:
                return default
        self.w1 = _get_w("VLLM_ELRAR_W1", 1.0)   # latency
        self.w2 = _get_w("VLLM_ELRAR_W2", 1.0)   # load
        self.w3 = _get_w("VLLM_ELRAR_W3", 1.0)   # mode match
        self.w4 = _get_w("VLLM_ELRAR_W4", 1.0)   # KV cache

        # HTTP client
        self.http_timeout = float(os.getenv("VLLM_ELRAR_HTTP_TIMEOUT", "1.0"))

        # 会话亲和历史
        self._session_last_engine: Dict[str, str] = {}
        self._session_last_ts_ms: Dict[str, int] = {}
        
        # 低开销会话过期清理：最小堆 (last_ts, session_id)
        self._session_heap: List[Tuple[int, str]] = []
        # 会话保留窗口（默认5分钟）与清理频率（默认每64个请求清理一次）
        try:
            self._session_retention_ms = int(os.getenv("VLLM_ELRAR_SESSION_RETENTION_MS", "300000"))
        except Exception:
            self._session_retention_ms = 300000
        try:
            self._session_clean_every = int(os.getenv("VLLM_ELRAR_SESSION_CLEAN_EVERY", "64"))
        except Exception:
            self._session_clean_every = 64
        self._req_counter = 0

        self._initialized = True

    def _cleanup_sessions_heap(self, now_ms: int) -> None:
        """使用最小堆按需清理过期会话，摊销 O(1)，每次 pop 为 O(log N)。"""
        expire_before = now_ms - max(0, int(self._session_retention_ms))
        heap = self._session_heap
        last_ts_map = self._session_last_ts_ms
        while heap and heap[0][0] < expire_before:
            ts, sid = heapq.heappop(heap)
            # 懒惰删除：仅当堆顶时间戳与当前表一致时，才真正删除映射
            cur_ts = last_ts_map.get(sid)
            if cur_ts is not None and cur_ts == ts:
                self._session_last_ts_ms.pop(sid, None)
                self._session_last_engine.pop(sid, None)

    def _match_state_for_endpoint(self, endpoint: EndpointInfo, states: Dict[str, Dict]) -> Optional[Dict]:
        """根据endpoint的信息尝试匹配对应的引擎状态。优先匹配url，其次Id、pod_name。"""
        if endpoint.url in states:
            return states[endpoint.url]
        if endpoint.Id and endpoint.Id in states:
            return states[endpoint.Id]
        if endpoint.pod_name and endpoint.pod_name in states:
            return states[endpoint.pod_name]
        try:
            url_wo_scheme = endpoint.url.split("://", 1)[-1]
            if url_wo_scheme in states:
                return states[url_wo_scheme]
        except Exception:
            pass
        return None

    def _is_state_fresh(self, state: Dict, now_ms: int) -> bool:
        ts = int(state.get("timestamp_ms", 0) or 0)
        return (now_ms - ts) <= self.stale_ms

    # 简化实现逻辑，默认不进行过滤
    def _filter_candidates(
        self,
        endpoints: List[EndpointInfo],
        states: Dict[str, Dict],
        now_ms: int,
    ) -> List[Tuple[EndpointInfo, Dict]]:
        """候选集过滤：资源充足 + 状态新鲜"""
        logger.info(f"Filtering candidates: {len(endpoints)} endpoints, {len(states)} states")
        candidates: List[Tuple[EndpointInfo, Dict]] = []
        for ep in endpoints:
            st = self._match_state_for_endpoint(ep, states)
            logger.info(f"Matched state for endpoint {ep.url}: {st}")
            if not st:
                continue
            # if not self._is_state_fresh(st, now_ms):
            #     logger.info(f"State for endpoint {ep.url} is not fresh")
            #     continue
            # kv_free = int(st.get("kv_cache_free_blocks", -1) or -1)
            # if kv_free < 0:
            #     continue
            # if kv_free < self.kv_free_min_blocks:
            #     continue
            candidates.append((ep, st))
        logger.info(f"Filtered candidates: {len(candidates)} candidates")
        return candidates

    def _norm_latency(self, predicted_latency_ms: float, slo_ms: float) -> float:
        if slo_ms <= 0:
            return 1.0
        # 归一化：预测延迟/目标SLO，截断到[0, +inf)
        value = max(0.0, float(predicted_latency_ms) / float(slo_ms))
        # 将>1区间压缩以避免过度影响
        return min(value, 5.0)

    def _score_latency(self, predicted_latency_ms: float, slo_ms: float) -> float:
        # 奖励低延迟：max(0, 1 - NormL)
        norm_l = self._norm_latency(predicted_latency_ms, slo_ms)
        return max(0.0, 1.0 - norm_l)

    def _norm_load(self, pending_tokens: int, engine_capacity_tokens_per_s: float) -> float:
        cap = float(engine_capacity_tokens_per_s or 0.0)
        if cap <= 1e-6:
            return 10.0  # 无容量时视为极高负载
        seconds = float(pending_tokens) / cap
        # 可选缩放
        return seconds / max(self.load_scale_s, 1e-6)

    def _score_load(self, pending_tokens: int, engine_capacity_tokens_per_s: float) -> float:
        import math as _math
        norm_w = self._norm_load(pending_tokens, engine_capacity_tokens_per_s)
        return 1.0 - _math.tanh(max(0.0, norm_w))

    def _score_mode_match(self, mode: str) -> float:
        if mode == "latency_optimized":
            return 1.0
        if mode == "throughput_optimized":
            return 0.0
        return 0.5

    def _score_kv_cache(self, session_id: Optional[str], engine_id: str, now_ms: int) -> float:
        if not session_id:
            return 0.0
        last_engine = self._session_last_engine.get(session_id)
        last_ts = self._session_last_ts_ms.get(session_id, 0)
        if last_engine and last_engine == engine_id and (now_ms - last_ts) \
            <= self.kv_affinity_window * 1000:
            return self.kv_beta
        return 0.0

    def _score_engine(
        self,
        state: Dict,
        session_id: Optional[str],
        slo_ms: float,
        now_ms: int,
    ) -> float:
        lat_ms = float(state.get("latency_pred_ms", 0.0) or 0.0)
        mode = state.get("scheduling_mode", "balanced")
        pend_tokens = int(state.get("pending_tokens_total", 0) or 0)
        capacity = float(state.get("engine_capacity", 0.0) or 0.0)
        engine_id = state.get("engine_id") or ""
        s_latency = self._score_latency(lat_ms, slo_ms)
        s_load = self._score_load(pend_tokens, capacity)
        s_mode = self._score_mode_match(mode)
        s_kv = self._score_kv_cache(session_id, engine_id, now_ms)
        logger.debug(f"Scoring engine {engine_id}: latency={s_latency}, load={s_load}, mode={s_mode}, kv={s_kv}")
        score = (
            self.w1 * s_latency +
            self.w2 * s_load +
            self.w3 * s_mode +
            self.w4 * s_kv
        )
        return score

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        engine_states: Optional[Dict[str, Dict]] = None,
    ) -> str:        
        session_id = request.headers.get(self.session_key, None)

        # 优先使用调用方传入的 engine_states
        if isinstance(engine_states, dict) and engine_states:
            states = engine_states
        else:
            logger.info("No engine states provided, using qps routing")
            url = self._qps_routing(endpoints, request_stats)
            return url, "elrar_fallback_qps"

        now_ms = int(time.time() * 1000)
        
        # 周期性地执行低开销会话清理
        self._req_counter = (self._req_counter + 1) % (1 << 30)
        if self._req_counter % max(1, self._session_clean_every) == 0:
            self._cleanup_sessions_heap(now_ms)
            
        candidates = self._filter_candidates(endpoints, states, now_ms)
        if not candidates:
            logger.info("No candidates found, using qps routing")
            url = self._qps_routing(endpoints, request_stats)
            return url, "elrar_no_candidates_qps"

        best_url = None
        best_score = float("-inf")
        for ep, st in candidates:
            score = self._score_engine(st, session_id, self.slo_ms, now_ms)
            if score > best_score:
                best_score = score
                best_url = ep.url

        if best_url is None:
            url = self._qps_routing(endpoints, request_stats)
            return url, "elrar_fallback_qps"

        # 记录会话亲和历史，用于KV得分
        if session_id:
            self._session_last_engine[session_id] = best_url
            self._session_last_ts_ms[session_id] = now_ms
            # 推入最小堆，配合懒惰删除处理重复更新
            heapq.heappush(self._session_heap, (now_ms, session_id))

        routing_method = "elrar_scored"
        return best_url, routing_method


# Instead of managing a global _global_router, we can define the initialization functions as:
def initialize_routing_logic(
    routing_logic: RoutingLogic, *args, **kwargs
) -> RoutingInterface:

    from vllm_router.request_logger import request_logger

    if kwargs.get("enable_request_logging"):
        logger.info("Enabling request logging")
        request_logger.enable_logging(True, kwargs.get("request_log_dir"))

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
        router = CacheAwareLoadBalancingRouter(
            kwargs.get("session_key"), kwargs.get("tolerate_waiting_requests")
        )
        return router
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
    elif routing_logic == RoutingLogic.ELRAR:
        logger.info("Initializing ELRAR routing logic")
        return ELRARRouter(
            kwargs.get("session_key"))
    else:
        raise ValueError(f"Invalid routing logic {routing_logic}")


def reconfigure_routing_logic(
    routing_logic: RoutingLogic, *args, **kwargs
) -> RoutingInterface:
    # Remove the existing routers from the singleton registry
    for cls in (
        SessionRouter,
        RoundRobinRouter,
        KvawareRouter,
        DisaggregatedPrefillRouter,
        ELRARRouter,
    ):
        if cls in SingletonABCMeta._instances:
            del SingletonABCMeta._instances[cls]

    # Re-configure request logging
    from vllm_router.request_logger import request_logger

    if kwargs.get("enable_request_logging"):
        logger.info("Re-enabling request logging with new configuration")
        request_logger.enable_logging(True, kwargs.get("request_log_dir"))
    else:
        # If request logging is not enabled, disable it
        request_logger.enable_logging(False)

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
        ELRARRouter,
    ):
        if cls in SingletonABCMeta._instances:
            return cls()
    raise ValueError("The global router has not been initialized")
