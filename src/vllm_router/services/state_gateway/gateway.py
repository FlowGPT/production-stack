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

"""
ELRAR State Gateway - 纯UDP状态聚合服务

支持两种模式：
1. UDP 广播模式：接收来自vLLM Engine的UDP广播状态
2. UDP 点播模式：接收来自vLLM Engine的UDP点播状态（推荐用于K8s环境）

负责：
1. 通过UDP接收各引擎的状态流
2. 状态聚合与时间同步
3. 为Router提供内存状态访问
"""

import json
import logging
import os
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class EngineState:
    """引擎状态向量（与engine_agent.py保持一致）"""
    engine_id: str
    timestamp_ms: int
    latency_pred_ms: float
    scheduling_mode: str
    pending_tokens_total: int
    kv_cache_free_blocks: int
    kv_cache_total_blocks: int
    engine_capacity: float


class StateGateway:
    """
    ELRAR State Gateway (纯UDP模式)
    
    环境变量控制：
    - VLLM_STATE_GATEWAY_UDP_PORT: UDP监听端口
    - VLLM_STATE_GATEWAY_STALE_THRESHOLD: 状态陈旧阈值(ms)
    """
    
    def __init__(self):
        self.udp_port = int(os.getenv('VLLM_STATE_GATEWAY_UDP_PORT', '9999'))
        self.stale_threshold_ms = int(os.getenv('VLLM_STATE_GATEWAY_STALE_THRESHOLD', '2000'))
        
        # 网络模式配置
        self.network_mode = os.getenv('VLLM_STATE_GATEWAY_NETWORK_MODE', 'unicast')
        self.bind_address = os.getenv('VLLM_STATE_GATEWAY_BIND_ADDRESS', '0.0.0.0')
        
        # 状态存储
        self._engine_states: Dict[str, EngineState] = {}
        self._state_history: Dict[str, deque] = defaultdict(lambda: deque())
        self._last_cleanup = time.time()
        
        # UDP Socket
        self._udp_socket = None
        self._udp_listener_thread = None
        self._udp_running = False
        
        # 统计信息
        self._stats = {
            'total_received': 0,
            'valid_states': 0,
            'invalid_states': 0,
            'unicast_received': 0,
            'broadcast_received': 0,
            'last_received': 0,
        }
        
        # 启动UDP监听
        self._start_udp_listener()
        
        logger.info(f"State Gateway initialized: mode={self.network_mode}, "
                   f"UDP={self.bind_address}:{self.udp_port}")
    
    def _start_udp_listener(self):
        """启动UDP监听器"""
        try:
            self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # 根据网络模式配置Socket
            if self.network_mode == "broadcast":
                # 广播模式：绑定到所有接口
                self._udp_socket.bind(('0.0.0.0', self.udp_port))
                logger.info(f"UDP listener started in broadcast mode on port {self.udp_port}")
            else:
                # 点播模式：绑定到指定地址
                self._udp_socket.bind((self.bind_address, self.udp_port))
                logger.info(f"UDP listener started in unicast mode on {self.bind_address}:{self.udp_port}")
            
            self._udp_running = True
            self._udp_listener_thread = threading.Thread(target=self._udp_listener_worker, daemon=True)
            self._udp_listener_thread.start()
        except Exception as e:
            logger.error(f"Failed to start UDP listener: {e}")
            self._udp_running = False
    
    def _udp_listener_worker(self):
        """UDP监听工作线程"""
        while self._udp_running:
            try:
                # 非阻塞接收
                self._udp_socket.settimeout(1.0)
                try:
                    data, addr = self._udp_socket.recvfrom(4096)
                    
                    # 更新统计
                    self._stats['total_received'] += 1
                    self._stats['last_received'] = time.time()
                    
                    # 根据网络模式更新统计
                    if self.network_mode == "unicast":
                        self._stats['unicast_received'] += 1
                        logger.info(f"✅ Received unicast UDP from {addr}: {addr[0]}:{addr[1]}")
                    else:
                        self._stats['broadcast_received'] += 1
                        logger.info(f"✅ Received broadcast UDP from {addr}: {addr[0]}:{addr[1]}")
                    
                    # 解析JSON数据
                    try:
                        state_data = json.loads(data.decode('utf-8'))
                        engine_id = state_data.get('engine_id', 'unknown')
                        logger.debug(f"Processing UDP state from {addr}: {engine_id}")
                        
                        # 验证并存储状态
                        if self._process_udp_state(state_data):
                            self._stats['valid_states'] += 1
                        else:
                            self._stats['invalid_states'] += 1
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON from {addr}: {e}")
                        self._stats['invalid_states'] += 1
                    except Exception as e:
                        logger.warning(f"Failed to process UDP state from {addr}: {e}")
                        self._stats['invalid_states'] += 1
                        
                except socket.timeout:
                    continue  # 超时继续循环
                    
            except Exception as e:
                logger.error(f"UDP listener error: {e}")
                time.sleep(0.1)  # 短暂等待后继续
        
        logger.info("UDP listener stopped")
    
    def _process_udp_state(self, state_data: dict) -> bool:
        """处理UDP接收到的状态数据"""
        try:
            # 验证数据格式
            required_fields = [
                'engine_id', 'timestamp_ms', 'latency_pred_ms',
                'scheduling_mode', 'pending_tokens_total',
                'kv_cache_free_blocks', 'kv_cache_total_blocks',
                'engine_capacity'
            ]
            
            for field in required_fields:
                if field not in state_data:
                    logger.warning(f"Missing required field in UDP state: {field}")
                    return False
            
            # 创建状态对象
            state = EngineState(**state_data)
            
            # 存储状态
            self._store_engine_state(state)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to process UDP state: {e}")
            return False
    
    def _store_engine_state(self, state: EngineState) -> None:
        """存储引擎状态"""
        # 更新当前状态
        self._engine_states[state.engine_id] = state
        
        # 添加到历史记录
        self._state_history[state.engine_id].append(state)
        
        # 限制历史记录大小（兼容老版本Python）
        if len(self._state_history[state.engine_id]) > 100:
            self._state_history[state.engine_id].popleft()
        
        # 定期清理陈旧状态
        current_time = time.time()
        if current_time - self._last_cleanup > 60:  # 每分钟清理一次
            self._cleanup_stale_states()
            self._last_cleanup = current_time
        
        logger.debug(f"Stored state for engine {state.engine_id}")
    
    def _is_state_stale(self, state: EngineState) -> bool:
        """检查状态是否陈旧"""
        current_time = int(time.time() * 1000)
        return (current_time - state.timestamp_ms) > self.stale_threshold_ms
    
    def _cleanup_stale_states(self) -> None:
        """清理陈旧状态"""
        stale_engines = []
        for engine_id, state in self._engine_states.items():
            if self._is_state_stale(state):
                stale_engines.append(engine_id)
        
        # 移除超过5分钟的陈旧状态
        stale_threshold = 5 * 60 * 1000  # 5分钟
        current_time = int(time.time() * 1000)
        
        for engine_id in stale_engines:
            state = self._engine_states[engine_id]
            if (current_time - state.timestamp_ms) > stale_threshold:
                del self._engine_states[engine_id]
                if engine_id in self._state_history:
                    del self._state_history[engine_id]
                logger.info(f"Removed stale engine state: {engine_id}")
    
    def get_engine_states_dict(self) -> Dict[str, dict]:
        """获取引擎状态字典（供Router使用）"""
        return {
            engine_id: {
                'engine_id': state.engine_id,
                'timestamp_ms': state.timestamp_ms,
                'latency_pred_ms': state.latency_pred_ms,
                'scheduling_mode': state.scheduling_mode,
                'pending_tokens_total': state.pending_tokens_total,
                'kv_cache_free_blocks': state.kv_cache_free_blocks,
                'kv_cache_total_blocks': state.kv_cache_total_blocks,
                'engine_capacity': state.engine_capacity,
            }
            for engine_id, state in self._engine_states.items()
        }
    
    def get_engine_state(self, engine_id: str) -> Optional[EngineState]:
        """获取特定引擎状态"""
        return self._engine_states.get(engine_id)
    
    def get_active_engines_count(self) -> int:
        """获取活跃引擎数量"""
        current_time = int(time.time() * 1000)
        active_count = 0
        for state in self._engine_states.values():
            if not self._is_state_stale(state):
                active_count += 1
        return active_count
    
    def get_total_engines_count(self) -> int:
        """获取总引擎数量"""
        return len(self._engine_states)
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        stats = self._stats.copy()
        
        # 添加网络模式信息
        stats['network_mode'] = self.network_mode
        stats['bind_address'] = self.bind_address
        stats['udp_port'] = self.udp_port
        stats['stale_threshold_ms'] = self.stale_threshold_ms
        
        # 添加引擎状态信息
        stats['active_engines'] = self.get_active_engines_count()
        stats['total_engines'] = self.get_total_engines_count()
        
        # 计算接收率
        if stats['total_received'] > 0:
            stats['valid_rate'] = f"{stats['valid_states'] / stats['total_received'] * 100:.2f}%"
        else:
            stats['valid_rate'] = "0.00%"
        
        return stats
    
    def shutdown(self):
        """关闭Gateway服务"""
        logger.info("Shutting down State Gateway...")
        
        # 停止UDP监听
        self._udp_running = False
        if self._udp_listener_thread:
            self._udp_listener_thread.join(timeout=2.0)
        
        # 关闭UDP Socket
        if self._udp_socket:
            self._udp_socket.close()
        
        logger.info("State Gateway shutdown complete")


# 全局Gateway实例
_gateway_instance: Optional[StateGateway] = None


def get_state_gateway() -> StateGateway:
    """获取Gateway实例（单例模式）"""
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = StateGateway()
    return _gateway_instance


# 独立运行Gateway服务
if __name__ == "__main__":
    import signal
    import sys
    
    def signal_handler(signum, frame):
        logger.info("Received shutdown signal")
        if _gateway_instance:
            _gateway_instance.shutdown()
        sys.exit(0)
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 创建并启动Gateway
    gateway = get_state_gateway()
    
    if gateway._udp_running:
        logger.info("ELRAR State Gateway running. Press Ctrl+C to stop.")
        
        # 主循环：定期输出统计信息
        try:
            while True:
                time.sleep(10)  # 每10秒输出一次统计
                stats = gateway.get_stats()
                logger.info(f"Gateway Stats: "
                           f"mode={stats['network_mode']}, "
                           f"bind={stats['bind_address']}:{stats['udp_port']}, "
                           f"engines={stats['active_engines']}/{stats['total_engines']}, "
                           f"received={stats['total_received']}, "
                           f"valid_rate={stats['valid_rate']}")
        except KeyboardInterrupt:
            pass
        finally:
            gateway.shutdown()
    else:
        logger.error("Failed to start UDP listener")
        sys.exit(1) 