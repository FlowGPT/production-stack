#!/bin/bash

# ELRAR UDP State Listener 启动脚本

# 设置环境变量
export VLLM_UDP_LISTENER_PORT=${VLLM_UDP_LISTENER_PORT:-9999}
export VLLM_UDP_LISTENER_LOG_FILE=${VLLM_UDP_LISTENER_LOG_FILE:-engine_states.jsonl}

echo "Starting ELRAR UDP State Listener..."
echo "UDP Port: $VLLM_UDP_LISTENER_PORT"
echo "Log File: $VLLM_UDP_LISTENER_LOG_FILE"

# 启动UDP监听器
python udp_listener.py 