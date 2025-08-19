#!/usr/bin/env python3
"""
ELRAR UDP State Listener - 轻量级UDP状态监听器

支持两种模式：
1. UDP 广播模式：接收来自vLLM Engine的UDP广播状态
2. UDP 点播模式：接收来自vLLM Engine的UDP点播状态（推荐用于K8s环境）
"""

import json
import logging
import os
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Optional

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


class UDPStateListener:
    """
    轻量级UDP状态监听器
    
    环境变量控制：
    - VLLM_UDP_LISTENER_PORT: UDP监听端口
    - VLLM_UDP_LISTENER_LOG_FILE: 状态日志文件
    """
    
    def __init__(self):
        self.port = int(os.getenv('VLLM_UDP_LISTENER_PORT', '9999'))
        self.log_file = os.getenv('VLLM_UDP_LISTENER_LOG_FILE', 'engine_states.jsonl')
        self.enable_log = os.getenv('VLLM_UDP_LISTENER_ENABLE_LOG', 'false') == 'true'
        
        # 网络模式配置
        self.network_mode = os.getenv('VLLM_UDP_LISTENER_NETWORK_MODE', 'unicast')
        self.bind_address = os.getenv('VLLM_UDP_LISTENER_BIND_ADDRESS', '0.0.0.0')
        
        # 状态存储
        self._engine_states: Dict[str, EngineState] = {}
        self._state_history: Dict[str, deque] = defaultdict(lambda: deque())
        
        # UDP Socket
        self._udp_socket = None
        self._listener_thread = None
        self._running = False
        
        # 统计信息
        self._stats = {
            'total_received': 0,
            'valid_states': 0,
            'invalid_states': 0,
            'last_received': 0,
            'unicast_received': 0,
            'broadcast_received': 0,
        }
        
        logger.info(f"UDP State Listener initialized: mode={self.network_mode}, "
                   f"bind={self.bind_address}:{self.port}")
    
    def start(self):
        """启动监听器"""
        try:
            self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # 根据网络模式配置Socket
            if self.network_mode == "broadcast":
                # 广播模式：绑定到所有接口
                self._udp_socket.bind(('0.0.0.0', self.port))
                logger.info(f"UDP State Listener started in broadcast mode on port {self.port}")
            else:
                # 点播模式：绑定到指定地址
                self._udp_socket.bind((self.bind_address, self.port))
                logger.info(f"UDP State Listener started in unicast mode on {self.bind_address}:{self.port}")
            
            self._running = True
            self._listener_thread = threading.Thread(target=self._listener_worker, daemon=True)
            self._listener_thread.start()
            
            return True
        except Exception as e:
            logger.error(f"Failed to start UDP listener: {e}")
            return False
    
    def stop(self):
        """停止监听器"""
        logger.info("Stopping UDP State Listener...")
        self._running = False
        
        if self._listener_thread:
            self._listener_thread.join(timeout=2.0)
        
        if self._udp_socket:
            self._udp_socket.close()
        
        logger.info("UDP State Listener stopped")
    
    def _listener_worker(self):
        """监听工作线程"""
        while self._running:
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
                        logger.debug(f"Received unicast from {addr}: {addr[0]}:{addr[1]}")
                    else:
                        self._stats['broadcast_received'] += 1
                        logger.debug(f"Received broadcast from {addr}: {addr[0]}:{addr[1]}")
                    
                    # 解析JSON数据
                    try:
                        state_data = json.loads(data.decode('utf-8'))
                        engine_id = state_data.get('engine_id', 'unknown')
                        logger.debug(f"Processing state from {addr}: {engine_id}")
                        
                        # 验证并存储状态
                        if self._process_state(state_data):
                            self._stats['valid_states'] += 1
                        else:
                            self._stats['invalid_states'] += 1
                            
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON from {addr}: {e}")
                        self._stats['invalid_states'] += 1
                    except Exception as e:
                        logger.warning(f"Failed to process state from {addr}: {e}")
                        self._stats['invalid_states'] += 1
                        
                except socket.timeout:
                    continue  # 超时继续循环
                    
            except Exception as e:
                logger.error(f"UDP listener error: {e}")
                time.sleep(0.1)
        
        logger.info("UDP listener worker stopped")
    
    def _process_state(self, state_data: dict) -> bool:
        """处理接收到的状态数据"""
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
                    logger.warning(f"Missing required field: {field}")
                    return False
            
            # 创建状态对象
            state = EngineState(**state_data)
            
            # 存储状态
            self._engine_states[state.engine_id] = state
            self._state_history[state.engine_id].append(state)
            
            # 限制历史记录大小（兼容老版本Python）
            if len(self._state_history[state.engine_id]) > 100:
                self._state_history[state.engine_id].popleft()
            
            # 写入日志文件
            if self.enable_log:
                self._log_state(state)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to process state: {e}")
            return False
    
    def _log_state(self, state: EngineState):
        """将状态写入日志文件"""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'timestamp': time.time(),
                    'state': {
                        'engine_id': state.engine_id,
                        'timestamp_ms': state.timestamp_ms,
                        'latency_pred_ms': state.latency_pred_ms,
                        'scheduling_mode': state.scheduling_mode,
                        'pending_tokens_total': state.pending_tokens_total,
                        'kv_cache_free_blocks': state.kv_cache_free_blocks,
                        'kv_cache_total_blocks': state.kv_cache_total_blocks,
                        'engine_capacity': state.engine_capacity,
                    }
                }, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.warning(f"Failed to log state: {e}")
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        stats = self._stats.copy()
        
        # 添加网络模式信息
        stats['network_mode'] = self.network_mode
        stats['bind_address'] = self.bind_address
        stats['port'] = self.port
        
        # 计算接收率
        if stats['total_received'] > 0:
            stats['valid_rate'] = f"{stats['valid_states'] / stats['total_received'] * 100:.2f}%"
        else:
            stats['valid_rate'] = "0.00%"
        
        return stats
    
    def get_engine_states(self) -> Dict[str, EngineState]:
        """获取所有引擎状态"""
        return self._engine_states.copy()
    
    def get_engine_state(self, engine_id: str) -> Optional[EngineState]:
        """获取特定引擎状态"""
        return self._engine_states.get(engine_id)


# 独立运行UDP监听器
if __name__ == "__main__":
    import signal
    import sys
    
    def signal_handler(signum, frame):
        logger.info("Received shutdown signal")
        listener.stop()
        sys.exit(0)
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 创建并启动监听器
    listener = UDPStateListener()
    
    if listener.start():
        logger.info("UDP State Listener running. Press Ctrl+C to stop.")
        
        # 主循环：定期输出统计信息
        try:
            while True:
                time.sleep(10)  # 每10秒输出一次统计
                stats = listener.get_stats()
                active_engines = len(listener.get_engine_states())
                logger.info(f"Stats: received={stats['total_received']}, "
                          f"valid={stats['valid_states']}, "
                          f"invalid={stats['invalid_states']}, "
                          f"active_engines={active_engines}")
        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()
    else:
        logger.error("Failed to start UDP State Listener")
        sys.exit(1) 