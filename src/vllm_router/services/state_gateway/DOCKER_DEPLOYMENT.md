# ELRAR State Gateway Docker 部署指南

## 🎯 概述

本文档详细说明如何在 Docker 环境中正确部署 ELRAR State Gateway，特别关注网络配置和 `bind_address` 设置。

## ⚠️ 重要提醒：bind_address 配置

### **关键原则**
在 Docker 环境中，**绝对不要**将 `bind_address` 设置为具体的 Pod IP 或容器 IP！

### **为什么不能绑定到具体 IP？**

1. **网络命名空间隔离**
   - 容器有自己的网络命名空间
   - 容器内的 `0.0.0.0` 对应宿主机的所有网络接口
   - 绑定到具体 IP 会限制网络访问

2. **IP 地址动态性**
   - Pod IP 在重启后可能变化
   - 容器 IP 在重新创建后可能变化
   - 固定 IP 配置会导致服务不可用

3. **网络策略限制**
   - 绑定到具体 IP 会阻止来自其他网络的连接
   - 在 K8s 环境中无法实现跨 Pod 通信

## 🐳 Docker 部署方式

### **方式 1：Host 网络模式（推荐用于单机部署）**

```bash
docker run -d \
  --name elrar-state-gateway \
  --network host \
  -e VLLM_STATE_GATEWAY_NETWORK_MODE=unicast \
  -e VLLM_STATE_GATEWAY_UDP_PORT=9999 \
  -e VLLM_STATE_GATEWAY_BIND_ADDRESS=0.0.0.0 \
  -e VLLM_STATE_GATEWAY_STALE_THRESHOLD=2000 \
  -e VLLM_STATE_GATEWAY_ENABLE_LOG=true \
  -e VLLM_STATE_GATEWAY_LOG_FILE=/data/engine_states.jsonl \
  -v /tmp/elrar:/data \
  elrar/state-gateway:latest
```

**特点：**
- 容器直接使用宿主机网络
- 性能最好，网络延迟最低
- 适合单机部署和性能测试

**注意事项：**
- 容器可以访问宿主机的所有网络接口
- 需要确保宿主机端口不被占用

### **方式 2：桥接网络模式（推荐用于容器化部署）**

```bash
docker run -d \
  --name elrar-state-gateway \
  --network bridge \
  -p 9999:9999/udp \
  -e VLLM_STATE_GATEWAY_NETWORK_MODE=unicast \
  -e VLLM_STATE_GATEWAY_UDP_PORT=9999 \
  -e VLLM_STATE_GATEWAY_BIND_ADDRESS=0.0.0.0 \
  -e VLLM_STATE_GATEWAY_STALE_THRESHOLD=2000 \
  -e VLLM_STATE_GATEWAY_ENABLE_LOG=true \
  -e VLLM_STATE_GATEWAY_LOG_FILE=/data/engine_states.jsonl \
  -v /tmp/elrar:/data \
  elrar/state-gateway:latest
```

**特点：**
- 容器有独立的网络命名空间
- 通过端口映射暴露服务
- 适合生产环境和多容器部署

**注意事项：**
- 需要正确配置端口映射
- 容器间通信需要通过 Docker 网络

### **方式 3：自定义网络模式**

```bash
# 创建自定义网络
docker network create elrar-network

# 运行容器
docker run -d \
  --name elrar-state-gateway \
  --network elrar-network \
  -p 9999:9999/udp \
  -e VLLM_STATE_GATEWAY_NETWORK_MODE=unicast \
  -e VLLM_STATE_GATEWAY_UDP_PORT=9999 \
  -e VLLM_STATE_GATEWAY_BIND_ADDRESS=0.0.0.0 \
  -e VLLM_STATE_GATEWAY_STALE_THRESHOLD=2000 \
  -e VLLM_STATE_GATEWAY_ENABLE_LOG=true \
  -e VLLM_STATE_GATEWAY_LOG_FILE=/data/engine_states.jsonl \
  -v /tmp/elrar:/data \
  elrar/state-gateway:latest
```

**特点：**
- 完全隔离的网络环境
- 可以配置自定义网络策略
- 适合复杂的网络架构

## 🔧 环境变量配置详解

### **必需配置**

```bash
# 网络模式：unicast（点播）或 broadcast（广播）
export VLLM_STATE_GATEWAY_NETWORK_MODE="unicast"

# UDP 端口
export VLLM_STATE_GATEWAY_UDP_PORT="9999"

# 绑定地址：Docker 环境必须设置为 0.0.0.0
export VLLM_STATE_GATEWAY_BIND_ADDRESS="0.0.0.0"
```

### **可选配置**

```bash
# 状态陈旧阈值（毫秒）
export VLLM_STATE_GATEWAY_STALE_THRESHOLD="2000"

# 启用状态日志
export VLLM_STATE_GATEWAY_ENABLE_LOG="true"

# 日志文件路径
export VLLM_STATE_GATEWAY_LOG_FILE="/data/engine_states.jsonl"
```

## 🌐 网络配置最佳实践

### **1. 绑定地址选择**

| 环境 | 推荐值 | 原因 |
|------|--------|------|
| Docker 容器 | `0.0.0.0` | 绑定到所有网络接口，支持跨容器通信 |
| Kubernetes Pod | `0.0.0.0` | 绑定到所有网络接口，支持跨 Pod 通信 |
| 本地开发 | `127.0.0.1` | 仅允许本机访问，网络隔离 |
| 传统部署 | 具体 IP | 绑定到特定网络接口 |

### **2. 端口配置**

```bash
# 检查端口占用
netstat -un | grep :9999
lsof -i :9999

# 如果端口被占用，更换端口
export VLLM_STATE_GATEWAY_UDP_PORT="8888"
```

### **3. 网络策略**

```bash
# 允许 UDP 流量
sudo ufw allow 9999/udp

# 或者使用 iptables
sudo iptables -A INPUT -p udp --dport 9999 -j ACCEPT
```

## 🚀 部署步骤

### **步骤 1：准备环境**

```bash
# 检查 Docker 是否运行
docker --version
docker ps

# 检查端口占用
netstat -un | grep :9999
```

### **步骤 2：拉取镜像**

```bash
# 拉取最新镜像
docker pull elrar/state-gateway:latest

# 或者构建本地镜像
docker build -t elrar/state-gateway:latest .
```

### **步骤 3：创建数据目录**

```bash
# 创建数据目录
mkdir -p /tmp/elrar
chmod 755 /tmp/elrar
```

### **步骤 4：启动容器**

```bash
# 使用推荐的配置启动
docker run -d \
  --name elrar-state-gateway \
  --network host \
  -e VLLM_STATE_GATEWAY_NETWORK_MODE=unicast \
  -e VLLM_STATE_GATEWAY_UDP_PORT=9999 \
  -e VLLM_STATE_GATEWAY_BIND_ADDRESS=0.0.0.0 \
  -e VLLM_STATE_GATEWAY_STALE_THRESHOLD=2000 \
  -e VLLM_STATE_GATEWAY_ENABLE_LOG=true \
  -e VLLM_STATE_GATEWAY_LOG_FILE=/data/engine_states.jsonl \
  -v /tmp/elrar:/data \
  elrar/state-gateway:latest
```

### **步骤 5：验证部署**

```bash
# 检查容器状态
docker ps | grep elrar-state-gateway

# 查看容器日志
docker logs elrar-state-gateway

# 测试 UDP 连通性
nc -zu localhost 9999

# 发送测试状态
echo '{"engine_id":"test","timestamp_ms":1234567890,"latency_pred_ms":100.0,"scheduling_mode":"latency_optimized","pending_tokens_total":10,"kv_cache_free_blocks":100,"kv_cache_total_blocks":1000,"engine_capacity":1000.0}' | nc -u localhost 9999
```

## 🛠️ 故障排除

### **常见问题**

#### **1. 容器启动失败**
```bash
# 查看详细错误信息
docker logs elrar-state-gateway

# 检查端口占用
netstat -un | grep :9999

# 检查权限
sudo docker run ...
```

#### **2. 网络不通**
```bash
# 检查容器网络
docker exec elrar-state-gateway netstat -un

# 检查端口映射
docker port elrar-state-gateway

# 测试网络连通性
docker exec elrar-state-gateway ping <target_ip>
```

#### **3. 状态接收失败**
```bash
# 检查日志
docker logs -f elrar-state-gateway

# 检查配置
docker exec elrar-state-gateway env | grep VLLM

# 测试状态接收
echo '{"test":"data"}' | nc -u localhost 9999
```

### **调试技巧**

```bash
# 进入容器调试
docker exec -it elrar-state-gateway /bin/bash

# 查看网络配置
netstat -un
ip addr show

# 查看进程
ps aux | grep gateway
```

## 📊 性能优化

### **1. 网络模式选择**

- **Host 模式**：性能最好，适合单机部署
- **桥接模式**：网络隔离，适合生产环境
- **自定义网络**：完全控制，适合复杂架构

### **2. 资源限制**

```bash
docker run -d \
  --name elrar-state-gateway \
  --network host \
  --memory="256m" \
  --cpus="0.5" \
  -e VLLM_STATE_GATEWAY_NETWORK_MODE=unicast \
  -e VLLM_STATE_GATEWAY_UDP_PORT=9999 \
  -e VLLM_STATE_GATEWAY_BIND_ADDRESS=0.0.0.0 \
  elrar/state-gateway:latest
```

### **3. 日志轮转**

```bash
# 使用 logrotate 管理日志
cat > /etc/logrotate.d/elrar-state-gateway << EOF
/tmp/elrar/engine_states.jsonl {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 644 root root
}
EOF
```

## 🔄 更新和维护

### **更新容器**

```bash
# 停止旧容器
docker stop elrar-state-gateway
docker rm elrar-state-gateway

# 拉取新镜像
docker pull elrar/state-gateway:latest

# 启动新容器
docker run -d \
  --name elrar-state-gateway \
  --network host \
  -e VLLM_STATE_GATEWAY_NETWORK_MODE=unicast \
  -e VLLM_STATE_GATEWAY_UDP_PORT=9999 \
  -e VLLM_STATE_GATEWAY_BIND_ADDRESS=0.0.0.0 \
  elrar/state-gateway:latest
```

### **备份数据**

```bash
# 备份状态日志
cp /tmp/elrar/engine_states.jsonl /backup/engine_states_$(date +%Y%m%d).jsonl

# 备份容器配置
docker inspect elrar-state-gateway > /backup/container_config_$(date +%Y%m%d).json
```

## 📚 参考资源

- [README_UDP_Unicast.md](./README_UDP_Unicast.md) - UDP 点播功能说明
- [config_examples](./config_examples) - 配置示例
- [start_gateway_unicast.sh](./start_gateway_unicast.sh) - 启动脚本

---

**总结**：在 Docker 环境中，始终将 `bind_address` 设置为 `0.0.0.0`，这样可以确保容器能够接收来自任何网络接口的连接，实现跨容器和跨 Pod 的通信。
