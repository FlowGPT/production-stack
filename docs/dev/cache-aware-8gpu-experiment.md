# Cache-Aware 路由器 八卡机完整验证手册

适用于一台全新的 8 GPU 机器（无任何相关环境）。**全程只需要你改一处：第 1 步的 `MODEL`。** 其余命令按顺序整段复制执行即可。

- 路由器镜像：`ssadds/production-stack-router:v1.0`
- vLLM 引擎镜像：`ssadds/vllm:260528-patch`

> 全文命令默认在 bash 下、以能使用 docker 的用户执行。每一节末尾给出“预期输出 / 通过标准”。

---

## 0. 前置环境检查

```bash
# 0.1 GPU 可见（应列出 8 张卡）
nvidia-smi --query-gpu=index,name,memory.total --format=csv

# 0.2 docker 可用
docker version | head -5

# 0.3 docker 能用 GPU（关键。无输出或报错说明缺 nvidia-container-toolkit）
docker run --rm --gpus all ssadds/vllm:260528-patch nvidia-smi -L || \
  echo "若失败：需安装 nvidia-container-toolkit 并重启 docker"

# 0.4 磁盘（模型权重 + 镜像，建议 ≥ 100G 空闲）
df -h /var/lib/docker
```

若 0.3 失败，先装 NVIDIA Container Toolkit：
```bash
# Ubuntu 示例
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

---

## 1. 设置变量（**只改这里**）

```bash
# ★ 唯一需要你决定的：用哪个模型（HF 仓库 id 或本地路径）
export MODEL="protoLabsAI/gemma-4-31B-it-FP8"

# 若是 HF 上的 gated/私有模型，填访问 token；公开模型可留空
export HF_TOKEN=""

# 单引擎占用的 GPU 数（模型能放进 1 张卡填 1；放不下填 2/4/8）
export TP=1

# 其余无需改动
export VLLM_IMAGE="ssadds/vllm:260528-patch"
export ROUTER_IMAGE="ssadds/production-stack-router:v1.0"
export HF_CACHE="$HOME/.cache/huggingface"
export N=$((8 / TP))          # 引擎数量 = 8卡 / 每引擎卡数
export SESSION_KEY="x-session-id"
mkdir -p "$HF_CACHE"
echo "将启动 $N 个引擎，每个用 $TP 张卡，模型=$MODEL"
```

---

## 2. 拉取镜像

```bash
docker pull "$ROUTER_IMAGE"
docker pull "$VLLM_IMAGE"
```

---

## 3. 启动 N 个 vLLM 引擎（每引擎独占 TP 张卡）

```bash
for i in $(seq 0 $((N-1))); do
  # 为第 i 个引擎分配 GPU：i*TP .. i*TP+TP-1
  gpus=$(seq $((i*TP)) $((i*TP+TP-1)) | paste -sd, -)
  docker run -d --name vllm-$i \
    --gpus "\"device=$gpus\"" --ipc=host \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    -e HF_TOKEN="$HF_TOKEN" \
    -v "$HF_CACHE":/root/.cache/huggingface \
    -p $((8000+i)):8000 \
    --entrypoint vllm "$VLLM_IMAGE" \
    serve "$MODEL" --host 0.0.0.0 --port 8000 \
    --tensor-parallel-size $TP \
    --max-model-len 8192 --gpu-memory-utilization 0.9 --trust-remote-code
  echo "started vllm-$i on GPU $gpus, port $((8000+i))"
done
```

等待全部就绪（首次会下载权重，可能较久）：
```bash
for i in $(seq 0 $((N-1))); do
  echo -n "waiting vllm-$i ..."
  until curl -sf http://127.0.0.1:$((8000+i))/v1/models >/dev/null 2>&1; do
    if ! docker ps -q --filter name=vllm-$i | grep -q .; then
      echo " CONTAINER DIED:"; docker logs --tail 30 vllm-$i; break
    fi
    sleep 5
  done
  echo " ready"
done
```

确认每个引擎对外的模型 id（应等于 `$MODEL`）：
```bash
curl -s http://127.0.0.1:8000/v1/models | python3 -c "import sys,json;print(json.load(sys.stdin)['data'][0]['id'])"
```

**通过标准**：N 个引擎全部 ready，`/v1/models` 返回的 id 与 `$MODEL` 一致。

---

## 4. 启动路由器

```bash
BACKENDS=$(for i in $(seq 0 $((N-1))); do echo -n "http://127.0.0.1:$((8000+i)),"; done | sed 's/,$//')
MODELS=$(for i in $(seq 0 $((N-1))); do echo -n "$MODEL,"; done | sed 's/,$//')

docker run -d --name car-router --network host "$ROUTER_IMAGE" \
  --host 0.0.0.0 --port 8001 \
  --service-discovery static \
  --static-backends "$BACKENDS" \
  --static-models "$MODELS" \
  --routing-logic cache_aware_load_balancing \
  --session-key "$SESSION_KEY" \
  --cache-aware-tolerate-waiting-requests 20 \
  --cache-aware-p50-e2e-threshold 0 \
  --cache-aware-p99-e2e-threshold 0 \
  --cache-aware-stats-window 30 \
  --cache-aware-inflight-decay 300 \
  --engine-stats-interval 3

sleep 5
docker logs car-router 2>&1 | grep -i "Initializing cache-aware" || docker logs --tail 20 car-router
```

**通过标准**：日志出现 `Initializing cache-aware load balancing routing logic ...`，容器保持 Up。

---

## 5. 通路冒烟（确认能正常代理）

```bash
curl -s http://127.0.0.1:8001/v1/chat/completions \
  -H "Content-Type: application/json" -H "$SESSION_KEY: smoke" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say hi\"}],\"max_tokens\":16}" \
  | python3 -m json.tool | head -20
```

**通过标准**：返回正常的 chat completion JSON。若返回 400 “Model ... not found”，说明请求里的 `model` 与 `--static-models` 不一致——确认两处都用 `$MODEL`。

---

## 6. 路由归属的查看方式（贯穿所有验证）

路由器对**每个请求**都会打一条 INFO 日志，包含会话与目标引擎：

```bash
# 实时看每个请求路由到哪台
docker logs -f car-router 2>&1 | grep "Routing request"
# 只看 fallback（含原因与两端负载）
docker logs -f car-router 2>&1 | grep "cache_aware fallback"
```

每台引擎的实时负载：
```bash
for i in $(seq 0 $((N-1))); do
  echo -n "vllm-$i: "
  curl -s http://127.0.0.1:$((8000+i))/metrics | \
    grep -E '^vllm:num_requests_(running|waiting)' | tr '\n' ' '; echo
done
```

路由器聚合指标：
```bash
curl -s http://127.0.0.1:8001/metrics | grep cache_aware
```

---

## 7. 会话感知压测脚本

保存为 `sessloader.py`：

```python
import json, os, sys, threading, time, urllib.request
ROUTER = "http://127.0.0.1:8001/v1/chat/completions"
MODEL = os.environ["MODEL"]
KEY = os.environ.get("SESSION_KEY", "x-session-id")
SESSIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 50
ROUNDS   = int(sys.argv[2]) if len(sys.argv) > 2 else 10
CONC     = int(sys.argv[3]) if len(sys.argv) > 3 else 20
MAXTOK   = int(sys.argv[4]) if len(sys.argv) > 4 else 32
sem = threading.Semaphore(CONC); lat = []; err = [0]; lock = threading.Lock()
def run(sid):
    for r in range(ROUNDS):
        with sem:
            body = json.dumps({"model": MODEL,
                "messages": [{"role": "user", "content": f"round {r} of session {sid}"}],
                "stream": True, "max_tokens": MAXTOK}).encode()
            req = urllib.request.Request(ROUTER, data=body,
                headers={"Content-Type": "application/json", KEY: f"sess-{sid}"})
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=120) as resp: resp.read()
                with lock: lat.append(time.time() - t0)
            except Exception:
                with lock: err[0] += 1
ths = [threading.Thread(target=run, args=(s,)) for s in range(SESSIONS)]
t0 = time.time(); [t.start() for t in ths]; [t.join() for t in ths]; wall = time.time() - t0
lat.sort(); n = len(lat)
print(f"reqs_ok={n} err={err[0]} wall={wall:.1f}s qps={n/wall:.1f} "
      f"p50={lat[n//2]:.3f}s p99={lat[min(n-1,int(n*0.99))]:.3f}s" if n else f"all failed err={err[0]}")
```

运行（50 会话 × 10 轮，并发 20）：
```bash
python3 sessloader.py 50 10 20
```

---

## 8. 功能验证

### 8.1 会话粘滞（无过载时同会话固定一台）
```bash
# 单会话连发 10 次，统计落到了哪台
docker logs --since 1s car-router >/dev/null 2>&1
for r in $(seq 1 10); do
  curl -s -o /dev/null http://127.0.0.1:8001/v1/chat/completions \
    -H "Content-Type: application/json" -H "$SESSION_KEY: stick-test" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$r\"}],\"max_tokens\":8}"
done
echo "该会话命中的引擎（应只有一台）:"
docker logs car-router 2>&1 | grep "stick-test" | grep -oE 'to http://[^ ]+' | sort | uniq -c
```
**通过标准**：只出现一台引擎。

### 8.2 排队过载转发
```bash
# 高并发压满，制造排队
python3 sessloader.py 200 5 200 &
sleep 8
echo "fallback 日志（应出现 reasons=['queue']）:"
docker logs car-router 2>&1 | grep "cache_aware fallback" | tail -5
echo "指标:"
curl -s http://127.0.0.1:8001/metrics | grep -E 'cache_aware_(stickiness|fallback)_rate |reason="queue"'
wait
```
**通过标准**：出现 `cache_aware fallback ... reasons=['queue']`；`fallback_rate>0`，`fallback_reason_rate{reason="queue"}>0`。

### 8.3 延迟阈值转发（可选，按 SLO）
```bash
# 重启路由器，加上 e2e 阈值（示例 2s，按你的 SLO 改）
docker rm -f car-router
docker run -d --name car-router --network host "$ROUTER_IMAGE" \
  --host 0.0.0.0 --port 8001 --service-discovery static \
  --static-backends "$BACKENDS" --static-models "$MODELS" \
  --routing-logic cache_aware_load_balancing --session-key "$SESSION_KEY" \
  --cache-aware-tolerate-waiting-requests 1000 \
  --cache-aware-p50-e2e-threshold 2 \
  --engine-stats-interval 3
sleep 5
python3 sessloader.py 100 8 150
docker logs car-router 2>&1 | grep "cache_aware fallback" | grep -oE "reasons=\[[^]]*\]" | sort | uniq -c
```
**通过标准**：fallback 原因里出现 `p50_e2e`（队列阈值已调到很大以排除 queue 干扰）。

### 8.4 指标自洽
```bash
curl -s http://127.0.0.1:8001/metrics | grep -E 'cache_aware_(stickiness|fallback)_rate '
```
**通过标准**：`stickiness_rate + fallback_rate ≈ 1`。

---

## 9. 稳定性验证

### 9.1 长稳 soak（≥30min，查内存）
```bash
( python3 sessloader.py 100 100000 30 ) &   # 长时间持续压
LOADPID=$!
for t in $(seq 1 30); do
  sleep 60
  mem=$(docker stats --no-stream --format '{{.MemUsage}}' car-router)
  echo "[$t min] router mem=$mem"
done
kill $LOADPID 2>/dev/null
```
**通过标准**：路由器内存基本平稳，无持续线性增长；期间无路由器崩溃。

### 9.2 引擎故障（在途不泄漏）
```bash
python3 sessloader.py 80 50 80 &
sleep 5
docker stop vllm-3                      # 杀掉一台
sleep 5
echo "该引擎在途计数（应趋于 0，不泄漏）:"
curl -s http://127.0.0.1:8001/metrics | grep 'cache_aware_inflight_requests' | grep 8003
docker start vllm-3                      # 恢复
wait
```
**通过标准**：被杀引擎的 `cache_aware_inflight_requests` 最终归零；其余引擎继续服务；压测整体不全失败。

### 9.3 引擎增删 / 滚动重启
```bash
docker restart vllm-5
# 观察会话重新分布、被重启引擎恢复后重新接流
docker logs -f car-router 2>&1 | grep "Routing request" | head -20
```
**通过标准**：重启期间请求不大面积失败；恢复后该引擎重新被路由。

### 9.4 高并发过载（负载均衡）
```bash
python3 sessloader.py 500 5 400
echo "各引擎在途分布（应相对均衡，无单点被压爆）:"
curl -s http://127.0.0.1:8001/metrics | grep 'cache_aware_inflight_requests'
```
**通过标准**：0 或极少错误；各引擎负载相对均衡。

### 9.5 多路由器副本（跨副本一致性）
```bash
# 第二个路由器，端口 8011，同一组后端
docker run -d --name car-router2 --network host "$ROUTER_IMAGE" \
  --host 0.0.0.0 --port 8011 --service-discovery static \
  --static-backends "$BACKENDS" --static-models "$MODELS" \
  --routing-logic cache_aware_load_balancing --session-key "$SESSION_KEY" \
  --cache-aware-tolerate-waiting-requests 20 --engine-stats-interval 3
sleep 5
for s in s1 s2 s3 s4 s5; do
  r1=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/v1/chat/completions -H "Content-Type: application/json" -H "$SESSION_KEY: $s" -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}">/dev/null; \
       docker logs car-router  2>&1 | grep " $s " | grep -oE 'to http://[^ ]+' | tail -1)
  r2=$(curl -s -o /dev/null http://127.0.0.1:8011/v1/chat/completions -H "Content-Type: application/json" -H "$SESSION_KEY: $s" -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}"; \
       docker logs car-router2 2>&1 | grep " $s " | grep -oE 'to http://[^ ]+' | tail -1)
  echo "$s: router1=$r1 router2=$r2"
done
docker rm -f car-router2
```
**通过标准**：每个会话在两个路由器上路由到**同一引擎**。

---

## 10. 清理

```bash
docker rm -f car-router car-router2 2>/dev/null
for i in $(seq 0 $((N-1))); do docker rm -f vllm-$i; done
nvidia-smi   # 确认显存释放
```

---

## 11. 常见问题排查

| 现象 | 原因 / 处理 |
|---|---|
| 请求 400 “Model ... not found” | 请求体 `model` 与 `--static-models` 不一致；两处都用 `$MODEL` |
| 引擎容器启动即退出 | `docker logs vllm-$i`：常见为显存不足（调小 `--gpu-memory-utilization` 或增大 `TP`）、模型需 token（设 `HF_TOKEN`）、`--max-model-len` 过大 |
| `--gpus` 报错 | 缺 nvidia-container-toolkit，见第 0 步 |
| 引擎卡死不返回 | 路由器对后端无读超时（见第 12 节边界 1）；给 vLLM 配请求超时 |
| fallback 一直为 0 | 引擎没真正过载，或阈值过大；提高并发或调小 `--cache-aware-tolerate-waiting-requests` |

---

## 12. 已知边界（实验时留意）

1. 路由器对后端无读超时（`timeout=None`）：引擎卡死但 TCP 未断时连接被占；在途计数由 `--cache-aware-inflight-decay` 回收，建议 vLLM 侧配超时/健康探针。
2. 静态服务发现不自动剔除挂掉的后端、不支持运行时增删；增删引擎需重启路由器（或改用 K8s 服务发现）。
3. 多副本同一抓取周期内同时突发，跨副本负载感知有滞后；务必将 `--engine-stats-interval` 调小（3–5s）。单副本不受影响。

---

## 13. 参数速查

| 参数 | 建议 | 说明 |
|---|---|---|
| `--routing-logic` | `cache_aware_load_balancing` | 启用本模式 |
| `--session-key` | 你的会话头名 | 必填 |
| `--cache-aware-tolerate-waiting-requests` | 单引擎可接受最大排队数 | 排队阈值 |
| `--cache-aware-p50-e2e-threshold` / `--cache-aware-p99-e2e-threshold` | 按 SLO（秒），0=关 | 端到端延迟阈值 |
| `--cache-aware-p50-ttft-threshold` / `--cache-aware-p99-ttft-threshold` | 按 SLO（秒），0=关 | 仅流式请求可信 |
| `--cache-aware-stats-window` | 30 | 指标窗口（秒） |
| `--cache-aware-inflight-decay` | 300 | 在途计数安全上限（秒） |
| `--engine-stats-interval` | 3–5 | 引擎指标抓取间隔；多副本必调小 |

设计细节见 `docs/dev/cache-aware-worklog.md`。
