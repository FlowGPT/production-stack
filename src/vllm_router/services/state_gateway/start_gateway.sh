#!/bin/bash

# ELRAR State Gateway 启动脚本（纯UDP模式）

# 设置环境变量
export VLLM_STATE_GATEWAY_UDP_PORT=${VLLM_STATE_GATEWAY_UDP_PORT:-9999}
export VLLM_STATE_GATEWAY_STALE_THRESHOLD=${VLLM_STATE_GATEWAY_STALE_THRESHOLD:-1000}

echo "Starting ELRAR State Gateway (UDP only)..."
echo "UDP Port: $VLLM_STATE_GATEWAY_UDP_PORT"
echo "Stale Threshold: ${VLLM_STATE_GATEWAY_STALE_THRESHOLD}ms"

# 启动Gateway服务
python gateway.py 