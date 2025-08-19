#!/bin/bash
# ELRAR State Gateway UDP 点播模式启动脚本
# 适用于 Kubernetes 环境

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查依赖
check_dependencies() {
    print_info "检查依赖..."
    
    if ! command -v python3 &> /dev/null; then
        print_error "Python3 未安装"
        exit 1
    fi
    
    if ! command -v netstat &> /dev/null; then
        print_warning "netstat 未安装，将使用替代命令"
    fi
    
    print_success "依赖检查完成"
}

# 设置环境变量
setup_environment() {
    print_info "设置环境变量..."
    
    # 基本配置
    export VLLM_STATE_GATEWAY_UDP_PORT="${VLLM_STATE_GATEWAY_UDP_PORT:-9999}"
    export VLLM_STATE_GATEWAY_STALE_THRESHOLD="${VLLM_STATE_GATEWAY_STALE_THRESHOLD:-2000}"
    
    # 网络模式配置
    export VLLM_STATE_GATEWAY_NETWORK_MODE="${VLLM_STATE_GATEWAY_NETWORK_MODE:-unicast}"
    export VLLM_STATE_GATEWAY_BIND_ADDRESS="${VLLM_STATE_GATEWAY_BIND_ADDRESS:-0.0.0.0}"
    
    # 日志配置
    export VLLM_STATE_GATEWAY_ENABLE_LOG="${VLLM_STATE_GATEWAY_ENABLE_LOG:-true}"
    export VLLM_STATE_GATEWAY_LOG_FILE="${VLLM_STATE_GATEWAY_LOG_FILE:-engine_states.jsonl}"
    
    print_success "环境变量设置完成"
}

# 显示配置信息
show_configuration() {
    print_info "当前配置："
    echo "  Network Mode:     ${VLLM_STATE_GATEWAY_NETWORK_MODE}"
    echo "  Bind Address:     ${VLLM_STATE_GATEWAY_BIND_ADDRESS}"
    echo "  UDP Port:         ${VLLM_STATE_GATEWAY_UDP_PORT}"
    echo "  Stale Threshold:  ${VLLM_STATE_GATEWAY_STALE_THRESHOLD}ms"
    echo "  Enable Log:       ${VLLM_STATE_GATEWAY_ENABLE_LOG}"
    echo "  Log File:         ${VLLM_STATE_GATEWAY_LOG_FILE}"
    echo ""
}

# 检查端口占用
check_port() {
    local port=$1
    print_info "检查端口 ${port} 占用情况..."
    
    if command -v netstat &> /dev/null; then
        if netstat -un | grep ":${port}" &> /dev/null; then
            print_warning "端口 ${port} 已被占用"
            netstat -un | grep ":${port}"
            return 1
        fi
    else
        # 使用 lsof 作为替代
        if command -v lsof &> /dev/null; then
            if lsof -i :${port} &> /dev/null; then
                print_warning "端口 ${port} 已被占用"
                lsof -i :${port}
                return 1
            fi
        fi
    fi
    
    print_success "端口 ${port} 可用"
    return 0
}

# 创建日志目录
setup_logging() {
    if [[ "${VLLM_STATE_GATEWAY_ENABLE_LOG}" == "true" ]]; then
        local log_dir=$(dirname "${VLLM_STATE_GATEWAY_LOG_FILE}")
        if [[ ! -d "${log_dir}" ]]; then
            print_info "创建日志目录: ${log_dir}"
            mkdir -p "${log_dir}"
        fi
        
        # 创建日志文件（如果不存在）
        if [[ ! -f "${VLLM_STATE_GATEWAY_LOG_FILE}" ]]; then
            print_info "创建日志文件: ${VLLM_STATE_GATEWAY_LOG_FILE}"
            touch "${VLLM_STATE_GATEWAY_LOG_FILE}"
        fi
    fi
}

# 启动 State Gateway
start_gateway() {
    print_info "启动 ELRAR State Gateway..."
    
    # 检查端口
    if ! check_port "${VLLM_STATE_GATEWAY_UDP_PORT}"; then
        print_error "端口 ${VLLM_STATE_GATEWAY_UDP_PORT} 不可用，请检查配置"
        exit 1
    fi
    
    # 设置日志
    setup_logging
    
    # 启动服务
    print_info "正在启动服务..."
    print_info "使用 Ctrl+C 停止服务"
    echo ""
    
    # 启动 Python 服务
    exec python3 gateway.py
}

# 主函数
main() {
    echo "=========================================="
    echo "  ELRAR State Gateway UDP 点播模式启动器"
    echo "=========================================="
    echo ""
    
    # 检查依赖
    check_dependencies
    
    # 设置环境
    setup_environment
    
    # 显示配置
    show_configuration
    
    # 启动服务
    start_gateway
}

# 信号处理
trap 'echo ""; print_info "收到停止信号，正在关闭..."; exit 0' INT TERM

# 运行主函数
main "$@"
