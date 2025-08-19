# ELRAR State Gateway UDP 点播模式使用指南

## 🎯 概述

ELRAR State Gateway 现在支持 UDP 点播模式，这是专门为 Kubernetes 环境设计的网络通信方式。相比传统的 UDP 广播，UDP 点播具有更好的网络兼容性和可靠性。

## 🔧 功能特性

### 1. **网络模式支持**
- **UDP 点播模式** (`network_mode: "unicast"`)：直接接收来自 vLLM Engine 的状态推送
- **UDP 广播模式** (`network_mode: "broadcast"`)：向后兼容的广播接收方式

### 2. **配置方式**
- **环境变量**：灵活的环境变量配置
- **启动脚本**：便捷的启动脚本支持
- **Kubernetes**：完整的 K8s 部署配置

### 3. **Kubernetes 友好**
- 支持跨 Pod、跨节点通信
- 网络策略友好，不会被阻止
- 自动服务发现和负载均衡

## 📋 配置参数

### 环境变量配置

```bash
# 基本配置
export VLLM_STATE_GATEWAY_UDP_PORT="9999"                    # UDP 监听端口
export VLLM_STATE_GATEWAY_STALE_THRESHOLD="2000"             # 状态陈旧阈值(ms)

# 网络模式配置
export VLLM_STATE_GATEWAY_NETWORK_MODE="unicast"             # "unicast" | "broadcast"
export VLLM_STATE_GATEWAY_BIND_ADDRESS="0.0.0.0"             # 绑定地址

# 日志配置
export VLLM_STATE_GATEWAY_ENABLE_LOG="true"                  # 启用状态日志
export VLLM_STATE_GATEWAY_LOG_FILE="engine_states.jsonl"     # 日志文件路径
```

### 配置说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `VLLM_STATE_GATEWAY_NETWORK_MODE` | `unicast` | 网络模式：`unicast`（点播）或 `broadcast`（广播） |
| `VLLM_STATE_GATEWAY_BIND_ADDRESS` | `0.0.0.0` | 绑定地址：`0.0.0.0` 表示所有接口 |
| `VLLM_STATE_GATEWAY_UDP_PORT` | `9999` | UDP 监听端口 |
| `VLLM_STATE_GATEWAY_STALE_THRESHOLD` | `2000` | 状态陈旧阈值（毫秒） |
| `VLLM_STATE_GATEWAY_ENABLE_LOG` | `true` | 是否启用状态日志 |
| `VLLM_STATE_GATEWAY_LOG_FILE` | `engine_states.jsonl` | 状态日志文件路径 |

## 🚀 使用方法

### 1. **使用启动脚本（推荐）**

```bash
# 使用默认配置启动
./start_gateway_unicast.sh

# 自定义配置启动
export VLLM_STATE_GATEWAY_UDP_PORT="8888"
export VLLM_STATE_GATEWAY_BIND_ADDRESS="127.0.0.1"
./start_gateway_unicast.sh
```

### 2. **直接使用 Python**

```bash
# 设置环境变量
export VLLM_STATE_GATEWAY_NETWORK_MODE="unicast"
export VLLM_STATE_GATEWAY_UDP_PORT="9999"
export VLLM_STATE_GATEWAY_BIND_ADDRESS="0.0.0.0"

# 启动服务
python3 gateway.py
```

### 3. **Docker 运行**

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

### 4. **Kubernetes 部署**

```bash
# 创建命名空间
kubectl create namespace vllm

# 部署 State Gateway
kubectl apply -f config_examples

# 查看部署状态
kubectl get pods -n vllm
kubectl get services -n vllm
```

## 🌐 网络架构

### UDP 点播模式

```
vLLM Engine 1 ──UDP──→ State Gateway
vLLM Engine 2 ──UDP──→ State Gateway  
vLLM Engine 3 ──UDP──→ State Gateway
```

### UDP 广播模式（向后兼容）

```
vLLM Engine 1 ──UDP Broadcast──→ Network Segment
vLLM Engine 2 ──UDP Broadcast──→ Network Segment
vLLM Engine 3 ──UDP Broadcast──→ Network Segment
                                    ↓
                              State Gateway
```

## 🔍 工作原理

### 1. **初始化阶段**
- 根据配置选择网络模式
- 设置 UDP Socket 参数
- 绑定到指定地址和端口

### 2. **监听阶段**
- 启动 UDP 监听线程
- 接收来自 vLLM Engine 的状态数据
- 根据网络模式更新统计信息

### 3. **状态处理阶段**
- 验证接收到的 JSON 数据
- 创建 EngineState 对象
- 存储到内存和日志文件

### 4. **状态管理阶段**
- 定期清理陈旧状态
- 提供状态查询接口
- 统计信息收集

## 📊 性能特点

### 优势
1. **低延迟**：UDP 协议，无连接开销
2. **高吞吐**：非阻塞接收，不影响性能
3. **网络友好**：点播模式避免广播风暴
4. **跨网络**：支持跨 Pod、跨节点通信

### 注意事项
1. **网络延迟**：根据网络情况调整 `stale_threshold`
2. **丢包处理**：UDP 不保证可靠传输
3. **防火墙**：确保目标端口开放

## 🛠️ 故障排除

### 常见问题

#### 1. **端口被占用**
```bash
# 检查端口占用
netstat -un | grep :9999
lsof -i :9999

# 更换端口
export VLLM_STATE_GATEWAY_UDP_PORT="8888"
```

#### 2. **权限不足**
```bash
# 检查端口权限
sudo netstat -un | grep :9999

# 使用 sudo 运行
sudo ./start_gateway_unicast.sh
```

#### 3. **网络不通**
```bash
# 检查网络连通性
ping <target_ip>
telnet <target_ip> 9999

# 检查防火墙
sudo ufw status
sudo iptables -L
```

#### 4. **状态不更新**
```bash
# 查看日志
tail -f engine_states.jsonl

# 检查 vLLM 引擎配置
# 确保 engine_agent 配置正确
```

### 调试技巧

1. **启用详细日志**
```bash
export VLLM_LOGGING_LEVEL=DEBUG
```

2. **检查 Socket 状态**
```bash
netstat -un | grep :9999
ss -un | grep :9999
```

3. **监控网络流量**
```bash
sudo tcpdump -i any udp port 9999
```

4. **测试状态接收**
```bash
# 发送测试状态
echo '{"engine_id":"test","timestamp_ms":1234567890,"latency_pred_ms":100.0,"scheduling_mode":"latency_optimized","pending_tokens_total":10,"kv_cache_free_blocks":100,"kv_cache_total_blocks":1000,"engine_capacity":1000.0}' | nc -u localhost 9999
```

## 🔄 迁移指南

### 从广播模式迁移到点播模式

1. **更新配置**
```bash
# 旧配置（广播）
export VLLM_STATE_GATEWAY_NETWORK_MODE="broadcast"

# 新配置（点播）
export VLLM_STATE_GATEWAY_NETWORK_MODE="unicast"
export VLLM_STATE_GATEWAY_BIND_ADDRESS="0.0.0.0"
```

2. **重启服务**
```bash
# 停止旧服务
pkill -f gateway.py

# 启动新服务
./start_gateway_unicast.sh
```

3. **验证迁移**
```bash
# 检查日志确认网络模式
grep "network mode" engine_states.jsonl

# 检查统计信息
# 应该看到 unicast_received 计数增加
```

## 📚 参考资源

- [gateway.py](./gateway.py) - 核心实现代码
- [udp_listener.py](./udp_listener.py) - UDP 监听器实现
- [start_gateway_unicast.sh](./start_gateway_unicast.sh) - 启动脚本
- [config_examples](./config_examples) - 配置示例
- [vLLM Engine Agent](../engine_agent.py) - 引擎端状态推送

## 🤝 贡献

如果你发现任何问题或有改进建议，请：

1. 在 GitHub 上创建 Issue
2. 提交 Pull Request
3. 联系维护团队

---

**注意**：UDP 点播模式是 ELRAR State Gateway 的新功能，在生产环境中使用前请充分测试。
