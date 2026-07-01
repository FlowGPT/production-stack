# load_balanced_affinity 真机验证步骤（8×H100，8 个真实 vLLM，线上回放）

目的：在 8 张 H100 上各起一个真实 vLLM（**gemma4-31B-it-FP8**，一卡一份），用**线上对话回放日志**对比 `load_balanced_affinity` / `roundrobin` / `session` 三种路由，证明新策略**同时拿到**：

- 哈希那样的**亲和命中**（同一对话回到同一引擎 → 5k token 前缀命中 KV 缓存 → TTFT 低），
- 轮询那样的**QPS 均衡 + 短长尾**（负载感知、过载自卸）。

> 离线理论复现见 `scripts/routing_sim.py`。本文用真实数据 `/mnt/shared/sss/data/replay-logs-conv-avg5k.json` 端到端跑。

## 这份负载长什么样

实测该日志（JSONL，每行 `{ts, conv_id, body.prompt}`）：

- **74,389 条请求 / 14,423 个对话**，平均 **5.16 轮/对话**（中位 3，p90 13，p99 24，最长 104 轮）——真·多轮。
- 每条 `prompt` 是**已套好模板的完整上下文**（`<|im_start|>system…assistant…user…`），均长 ~20K 字符 ≈ **5–5.7K tokens**；其中 ~400 token 的 system 角色卡 + 历史**整段重发**。
- 已验证：同一 `conv_id` 的后一轮 prompt **以前一轮为前缀**，历史逐轮变长。

含义：**亲和的收益就是 KV 前缀缓存**。同一对话的某轮若落到「已缓存它那 5k 前缀」的引擎，prefill 几乎免费、TTFT 极低；落到别的引擎就得把 5k token 重新 prefill。所以这份负载里区分路由优劣的关键指标是 **TTFT** 和引擎的 **prefix-cache 命中率**，不只是端到端时延。

## 发送方式（为什么这么发）

驱动器 `scripts/replay_conv_ab.py`：

1. **对话内串行、对话间并发**：一轮是上一轮回复的延续（因果有序），所以每个 `conv_id` 由一个 worker 顺序播放它的各轮；`--concurrency` = 同时活跃的对话数（这就是压力旋钮）。这复现了 router 看到的「回访会话」模式。
2. **`conv_id` 当 session header**（`X-Flow-Conversation-Id`，线上同款）——路由亲和键。
3. **`body.prompt` 原样发** `/v1/completions`（已套模板，不能再过 chat 模板）。
4. **回复长度日志里没有**，按角色扮演实际分布采样（80% 64–256，15% 256–512，5% 512–1024，可 `--max-tokens` 固定）。
5. **流式读，量 TTFT**：`stream=true`，记第一段非空 token 的时刻 = TTFT；读完 = E2E。
6. 跑完抓 router `/metrics`：per-engine `current_qps`（均衡）、`gpu_prefix_cache_hit_rate`（亲和是否真命中）、`load_balanced_*`（命中/卸载计数）。

## 成功判据（A/B 同一份回放）

1. **无错误**：三种模式 5xx/异常均为 0（重点：新模式无裸 500）。
2. **TTFT（亲和命中）**：`load_balanced_affinity` 的 TTFT p50/p95 **接近 `session`**、**远低于 `roundrobin`**——证明亲和被保住、前缀缓存吃到。
3. **prefix-cache 命中率**：`load_balanced_affinity` 高、接近 `session`，**远高于 `roundrobin`**。
4. **QPS 均衡**：`load_balanced_affinity` 的 per-engine `max/min`/CV **接近 `roundrobin`**、**显著低于 `session`**。
5. **长尾**：`load_balanced_affinity` 的 E2E p99 **≤ `session`**（过载自卸的功劳），且不差于 `roundrobin`。
6. **亲和不倒贴**：`load_balanced_affinity` 不比 `--no-lb-affinity` 差（QPS 均衡相当，TTFT 更低）。

> 一句话预期：**`roundrobin` 输在 TTFT/缓存（每轮重 prefill）；`session` 输在均衡/长尾（热点对话压垮单卡）；`load_balanced_affinity` 两头都接近各自最优。**

## 0. 前置

- 8×H100 单机；Docker 可见全部 GPU；已挂载 `/mnt/shared`（含数据集）。
- 路由镜像 `ssadds/production-stack-router:v1.3.1`，vLLM 镜像 `ssadds/vllm:kaon-0.22-v1.3`（实测 vllm 0.22.1 + lmcache 0.4.6）。
- 主模型 **gemma4-31B-it-FP8**（FP8/compressed-tensors 量化，H100 能跑）。HF 仓库名（已核实存在性 + 权限）：
  - ✅ **`RedHatAI/gemma-4-31B-it-FP8-Dynamic`** —— **公开、可直接下**（tags：`fp8 / vllm / compressed-tensors`，base=`google/gemma-4-31B-it`）。真机没有就下这个，**默认用它**。
  - ⚠️ `RedHatAI/gemma-4-31B-it-FP8`（静态版）/ `google/gemma-4-31B-it-FP8` —— 均 **gated（HTTP 401，需申请权限 + HF token）**，离线现下会卡，除非你已有 token。
  - 投机 draft **`google/gemma-4-31B-it-assistant`** —— 公开 ✓，与 `--speculative-config` 一致。
- `served-model-name` 统一用 **`gemma4-31B-it-FP8`**（与用哪个 HF 仓库无关；三处一致：引擎 / router `--static-models` / 驱动 `--model`）。本地权重目录如 `/data/models/gemma4-31B-it-FP8`。
- API key：引擎带 `--api-key`，所以**客户端请求要带 `Authorization: Bearer <key>`**，router 透传给引擎；驱动器用 `--api-key` 传。

## 本验证有两个维度

1. **路由策略**：`load_balanced_affinity` vs `roundrobin` vs `session`（第 3 节）。
2. **KV offload**：每个 vLLM 用 `SimpleCPUOffloadConnector` 给 100G CPU 做 KV offload，**同一路由策略下** off vs on，看同 QPS 是否更快 / 能否扛更高 QPS（第 3B 节）。

> offload 与本工作负载强相关：被 GPU 显存挤掉的 5k 对话前缀能从 100G CPU 池捞回，省掉重 prefill——**和亲和是叠加增益**（亲和把同对话送回同引擎，offload 让那个引擎更不容易丢它的前缀）。

## 1. 起 8 个 vLLM 引擎（一卡一份，线上同款配置）

用你线上的真实配置（compressed-tensors 量化 + fp8 KV + MTP 投机 + cutlass MoE）。**显式开前缀缓存**——它既是亲和收益来源，也是 offload 的前置：

```bash
MODEL=/data/models/gemma4-31B-it-FP8
NAME=gemma4-31B-it-FP8            # 三处一致：引擎 / router --static-models / 驱动 --model
APIKEY=kaon-myTTpAVQsE06ogbKfBA9bZePR3pQ1QPy
HFCACHE=$HOME/.cache/huggingface  # draft 模型按 HF id 拉，挂进容器复用缓存

# 0) 模型不在本地就先下（公开仓库，无需 token）
MAIN_HF=RedHatAI/gemma-4-31B-it-FP8-Dynamic
DRAFT_HF=google/gemma-4-31B-it-assistant
pip install -q -U "huggingface_hub[cli]" 2>/dev/null
[ -d "$MODEL" ] || huggingface-cli download "$MAIN_HF" --local-dir "$MODEL"
huggingface-cli download "$DRAFT_HF" >/dev/null   # 进 $HFCACHE，供投机解码用

run_engine() {   # $1=gpu索引  其余=额外参数(offload时用)
  local i=$1; shift
  docker run -d --name vllm-$i --gpus "device=$i" \
    -v $MODEL:/model -v $HFCACHE:/root/.cache/huggingface \
    -p $((8000+i)):8000 --shm-size 16g \
    ssadds/vllm:kaon-0.22-v1.3 \
    --model /model --served-model-name $NAME --port 8000 \
    --max-model-len 16384 --max-num-seqs 64 --max-num-batched-tokens 8192 \
    --gpu-memory-utilization 0.95 \
    --enable-prefix-caching \
    --language-model-only --trust-remote-code \
    --kv-cache-dtype fp8 --quantization compressed-tensors \
    --moe-backend cutlass --stream-interval 5 -O3 \
    --speculative-config '{"method":"mtp","model":"google/gemma-4-31B-it-assistant","num_speculative_tokens":3}' \
    --api-key $APIKEY \
    "$@"
}

# 基线（无 offload）
for i in $(seq 0 7); do run_engine $i; done
for i in $(seq 0 7); do until curl -sf localhost:$((8000+i))/health; do sleep 3; done; done
echo; echo "8 engines up (baseline, prefix caching on)"
```

> 模型下载：FP8 主模型约 30+GB、draft 数 GB，走 `/data` 大盘；想用 gated 的 `google/gemma-4-31B-it-FP8` 静态版则先 `huggingface-cli login` 或给容器 `-e HF_TOKEN=...`，把 `MAIN_HF` 换掉即可（`served-model-name` 不变）。
> `--max-model-len 16384`（线上实际值）：prompt p95≈7k + 输出 256 装得很宽，基本不会上下文溢出；`--max-tokens 256` 是为公平比较而固定，非容量所迫。`--max-num-seqs 64`：8 卡共 512 并发槽，压测并发要往上顶才看得到排队/长尾分化。

## 2. 起 router —— 先 load_balanced_affinity

> router 用 **8100**，别用 8001——引擎占了 8000–8007，8001 会和 `vllm-1` 撞。

```bash
BACKENDS=$(for i in $(seq 0 7); do echo -n "http://127.0.0.1:$((8000+i)),"; done | sed 's/,$//')
MODELS=$(for i in $(seq 0 7); do echo -n "gemma4-31B-it-FP8,"; done | sed 's/,$//')

start_router() {  # $1 = 额外的路由参数
  docker rm -f router >/dev/null 2>&1
  docker run -d --name router --network host --entrypoint vllm-router \
    ssadds/production-stack-router:v1.3.1 --host 0.0.0.0 --port 8100 \
    --service-discovery static --static-backends "$BACKENDS" --static-models "$MODELS" \
    --engine-stats-interval 2 $1
  until curl -sf localhost:8100/health; do sleep 1; done; echo " router up: $1"
}

start_router "--routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id --lb-affinity --lb-d-choices 2"
docker logs router 2>&1 | grep -i "load-balanced affinity"   # 确认模式
```

## 3. 跑 A/B（固定回放，逐模式切换）

`--seed`、`--max-convs`、`--max-records` 不变 → 四次跑**同一批对话同一序列**，可直接比。`--concurrency` 调到能把 p99 压出区别为止（先 96，不够就 192）。

```bash
DS=/mnt/shared/sss/data/replay-logs-conv-avg5k.json
COMMON="--dataset $DS --router http://127.0.0.1:8100 --model gemma4-31B-it-FP8 \
        --session-header X-Flow-Conversation-Id --api-key $APIKEY \
        --max-convs 3000 --max-records 30000 --concurrency 96 \
        --max-tokens 256 --seed 2026"

# (A) 新策略（已在跑）
python scripts/replay_conv_ab.py $COMMON --label load_balanced_affinity | tee /tmp/ab_lb.txt

# (B) 纯负载均衡：验证亲和不倒贴
start_router "--routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id --no-lb-affinity --lb-d-choices 2"
python scripts/replay_conv_ab.py $COMMON --label load_balanced_noaffinity | tee /tmp/ab_lbna.txt

# (C) 轮询：每轮重 prefill，TTFT/缓存最差
start_router "--routing-logic roundrobin"
python scripts/replay_conv_ab.py $COMMON --label roundrobin | tee /tmp/ab_rr.txt

# (D) 一致性哈希（现状）：缓存好但不均衡/长尾差
start_router "--routing-logic session --session-key X-Flow-Conversation-Id"
python scripts/replay_conv_ab.py $COMMON --label session | tee /tmp/ab_session.txt
```

> 引擎不必重启（路由是无状态切换）。但前缀缓存会跨轮残留，**为公平**：每切一次模式前 `sleep 90` 让引擎 KV 排空，或重启 8 个 vLLM；否则后跑的模式白蹭前一轮缓存。数据加载较慢（NFS + 大行，~30k 行约 1–2 分钟），属正常。

## 3B. KV offload 对比（SimpleCPUOffloadConnector，100G/实例）

**问题**：同 QPS 下整体更快吗？或能扛更高 QPS？**做法**：选定**胜出的路由策略**（一般是 `load_balanced_affinity`），只改引擎——off vs on——其余全相同。

100G offload 用 vLLM 内置 `SimpleCPUOffloadConnector`（实测此镜像已注册）。单卡实例 `world_size=1`，故 `cpu_bytes_to_use` 即每实例容量；`100*1024^3 = 107374182400`。它**要求前缀缓存已开**（上面已开）。重起 8 个引擎，在第 1 节 `run_engine` 基础上追加一个参数即可：

```bash
docker rm -f $(for i in $(seq 0 7); do echo vllm-$i; done)
# 用数组传，避免 JSON 里的空格被 bash 拆词（run_engine 末尾已是 "$@"）
OFFLOAD=(--kv-transfer-config '{"kv_connector":"SimpleCPUOffloadConnector","kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":107374182400}}')
for i in $(seq 0 7); do run_engine $i "${OFFLOAD[@]}"; done
for i in $(seq 0 7); do until curl -sf localhost:$((8000+i))/health; do sleep 3; done; done
docker logs vllm-0 2>&1 | grep -iE "SimpleCPUOffload|cpu_bytes|offload"   # 确认连接器起来了、容量=100G
```

**两个子实验**（路由策略固定不变，只换引擎 off/on）：

```bash
# 路由保持胜出策略（示例：新策略 + 亲和）
start_router "--routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id --lb-affinity --lb-d-choices 2"

# (1) 同 QPS 更快？—— 用与第 3 节相同的 COMMON（同并发=同 offered load）
python scripts/replay_conv_ab.py $COMMON --label lb_offload_on | tee /tmp/ab_off_on.txt
# 对比 /tmp/ab_lb.txt（off）：同并发下 off_on 的 TTFT/E2E 应更低、prefix-cache hit 更高、turns/s 更高

# (2) 能扛更高 QPS？—— off 和 on 各自爬并发，找 p99 TTFT 撞 SLA（例 1.5s）前的最大吞吐
for C in 96 192 288 384; do
  python scripts/replay_conv_ab.py $COMMON --concurrency $C --label lb_off_c$C   | tee /tmp/ramp_off_$C.txt
done
# 重起为 offload 引擎后再爬一遍
for C in 96 192 288 384; do
  python scripts/replay_conv_ab.py $COMMON --concurrency $C --label lb_on_c$C    | tee /tmp/ramp_on_$C.txt
done
```

**判读 offload 增益**：

```
同并发(子实验1)：  on 的 TTFT p95/p99 ↓、prefix-cache hit ↑、turns/s ↑  → offload 有正收益
撞 SLA 的最大并发(子实验2)： on 能在更高并发仍满足 p99≤SLA → 同 SLA 下扛更高 QPS
```

> 机理：100G CPU 池兜住被 GPU 显存挤掉的对话前缀，cache-miss 时从 CPU 拉回而非重 prefill（本负载 prefill 占 TTFT 大头），所以**热点对话多 / 显存被投机解码和大 batch 吃紧时增益最明显**。若 on 反而变慢，多半是 offload 的拷贝开销在你的前缀复用率下盖过了省下的 prefill——记录下来即是结论。注意 offload 与 MTP 投机解码同开属未充分验证组合，若 on 出现异常，先单独跑「offload 开、投机关」隔离。

## 4. 判读

四份输出对比这几行：

```
TTFT(s):  p50=.. p95=.. p99=..      <- 亲和命中：lb ≈ session ≪ roundrobin
E2E(s):   p50=.. p95=.. p99=..      <- 长尾：lb ≤ session，且不差于 roundrobin
per-engine QPS: max/min=.. CV=..    <- 均衡：lb ≈ roundrobin ≪ session
prefix-cache hit rate: mean=..      <- lb 高、近 session，≫ roundrobin
turns=.. ok=.. codes={..}           <- 5xx/异常必须 0（16384 上下文下基本不会有 400）
```

新模式额外抓（趁压测**进行中**，QPS 窗口未衰减）：

```bash
curl -s localhost:8100/metrics | grep -E \
 "load_balanced_(placement|affinity_hit_total|affinity_shed_total|affinity_hit_rate)|gpu_prefix_cache_hit_rate"
# 不变式：placement_total + affinity_hit_total + affinity_shed_total == 成功请求数
# affinity_hit_rate 高 = 大多回访都贴回了原引擎；shed_total 随负载升高而升 = 在卸载
```

判定：满足上面「成功判据」6 条即通过。典型结论——`load_balanced_affinity` 的 TTFT/缓存命中贴近 `session`，QPS 均衡贴近 `roundrobin`，E2E p99 是三者最低或并列最低。

## 5. 要收集并回传的数据（**请严格按此清单**）

数据分三层：**客户端**（端到端体验）、**router /metrics**（均衡 + 亲和行为）、**引擎 /metrics + 系统**（缓存命中、KV 占用、offload 命中、GPU/内存）。下面每项都注明「取哪、为什么、什么格式」。

**两条铁律**：
1. **跑中抓快照**：`current_qps`、`num_requests_*`、GPU 利用率都是瞬时/窗口量，跑完就衰减。务必在压测**进行中**周期性快照（脚本见 5.2）。
2. **计数器要取差值**：引擎的 `*_total` 是累计值，**每个 run 前后各记一次**，用「后 − 前」算这一轮的命中率/抢占数（5.3）。

### 5.0 命名约定（便于我对齐）

每个 run 一个 label：`<策略>_<offload>_c<并发>`，例 `lb_offoff_c96`、`lb_offon_c96`、`rr_offoff_c96`、`session_offoff_c96`。driver 输出 `tee` 到 `/tmp/<label>.txt`，快照存 `/tmp/snap_<label>/`。

### 5.1 客户端（driver stdout）—— 每个 run 必留

`replay_conv_ab.py` 跑完会打印以下行，**整段原样回传**（别只截一部分）：

```
turns=.. ok=.. errors=.. codes={200:..,400:..,'exc':..}   # 失败分布（5xx/exc 必须 0）
throughput=.. turns/s over ..s, concurrency=..            # 实测吞吐 + offered load
TTFT(s):  p50=.. p90=.. p95=.. p99=..                     # 首 token，亲和/缓存最敏感
E2E(s):   p50=.. p90=.. p95=.. p99=..                     # 端到端
per-engine QPS: n=.. max/min=.. CV=..                     # 注：driver 跑完才抓、可能已衰减，以 5.2 为准
prefix-cache hit rate: mean=.. min=.. max=..              # router 侧聚合值
```

**汇总表**（一行一个 run，最后回传这张表）：

| label | 策略 | offload | 并发 | wall(s) | turns/ok/err | throughput(turns/s) | TTFT p50/p95/p99 | E2E p50/p95/p99 | QPS max-min/CV | prefix-hit mean |
|---|---|---|---|---|---|---|---|---|---|---|
| lb_offoff_c96 | lb | off | 96 | | | | | | | |
| … | | | | | | | | | | |

### 5.2 router `/metrics`（跑中快照，QPS 均衡的权威来源）

**每个 run 开跑前**在后台起快照循环，跑完 `kill` 它。它每 15s 抓 router + 8 个引擎 + GPU + 内存，带时间戳追加：

```bash
snap() {            # 用法：S=$(snap lb_offoff_c96); python replay_conv_ab.py ... ; kill $S
  local out=/tmp/snap_$1; mkdir -p "$out"
  ( while true; do ts=$(date +%s)
      curl -s localhost:8100/metrics | grep -E \
        'vllm:current_qps|vllm:num_requests_(running|waiting)|vllm:gpu_prefix_cache_hit_rate|load_balanced_' \
        | sed "s/^/$ts /" >> "$out/router.log"
      for i in $(seq 0 7); do
        curl -s localhost:$((8000+i))/metrics | grep -E \
          'vllm:(external_)?prefix_cache_(hits|queries)_total|vllm:kv_cache_usage_perc|vllm:num_requests_(running|waiting)|vllm:num_preemptions_total' \
          | sed "s/^/$ts e$i /" >> "$out/engine.log"
      done
      nvidia-smi --query-gpu=index,utilization.gpu,memory.used,power.draw \
        --format=csv,noheader,nounits | sed "s/^/$ts /" >> "$out/gpu.log"
      free -g | awk -v ts=$ts 'NR==2{print ts,"used_G="$3,"free_G="$4}' >> "$out/host_mem.log"
      sleep 15
    done ) & echo $!     # 打印快照循环 PID，run 结束后 kill 它
}
```

router 侧关键行（`router.log`）：

| metric | 含义 / 怎么用 |
|---|---|
| `vllm:current_qps{server=...}` | 每引擎实时 QPS → 取一个稳定时段算 **max/min 与 CV**（均衡核心证据） |
| `vllm:num_requests_running{server=...}` | 每引擎在跑数 → 负载分布 |
| `vllm:num_requests_waiting{server=...}` | 每引擎排队数 → 长尾/过载来源 |
| `vllm:gpu_prefix_cache_hit_rate{server=...}` | router 聚合的前缀命中率（亲和是否真命中） |
| `load_balanced_placement_total` | P2C 新放置次数 |
| `load_balanced_affinity_hit_total` | 回访贴回原引擎次数 |
| `load_balanced_affinity_shed_total` | 因原引擎过载而改投次数（负载升高应上升） |
| `load_balanced_affinity_hit_rate` | 亲和命中率（贴回 / 回访总数） |

> 不变式：`placement_total + affinity_hit_total + affinity_shed_total` ≈ 成功请求数（用来自检计数没漏）。

### 5.3 引擎 `/metrics`（每卡 8000+i，缓存与 offload 的一手数据）

这些是**累计计数器**，每个 run **前后各抓一次**（`engine.log` 里也有时序）。用差值算这一轮：

| metric | 含义 / 怎么用 |
|---|---|
| `vllm:prefix_cache_queries_total` / `vllm:prefix_cache_hits_total` | **GPU** 前缀缓存查询/命中 → 命中率 = Δhits/Δqueries（亲和把同对话送回同卡的直接收益） |
| `vllm:external_prefix_cache_queries_total` / `vllm:external_prefix_cache_hits_total` | **offload（CPU 池）命中** → offload off 时≈0，on 时>0。**这是 offload 是否生效的直接证据** |
| `vllm:kv_cache_usage_perc` | GPU KV 占用 → 是否被打满（接近 1 = 显存吃紧，offload 收益场景） |
| `vllm:num_preemptions_total` | 抢占次数 → KV 压力/过载的硬信号；offload on 应降低（前缀从 CPU 拉回，少抢占） |
| `vllm:num_requests_running` / `_waiting` | 引擎侧在跑/排队 |

**每卡 8 个值都要**（不是只取一卡）——均衡和命中都是分布问题。

### 5.4 系统（GPU + 主机内存）

- `gpu.log`：每卡 `utilization.gpu`、`memory.used`、`power.draw` 时序 → 8 卡利用率是否均匀、有没有谁空着；offload on 的 `memory.used` 应与 off 接近（KV 在 CPU，不占显存）。
- `host_mem.log`：主机 `used_G` → **确认 offload 池真的在填**（on 比 off 高，最多接近 8×100=800G）。
- ★**跑 offload 前必查**：`free -g` 主机可用内存要 ≥ ~**900G**（8×100G 池 + 模型/激活余量）。不够会 OOM/swap 反而更慢——这时把 `cpu_bytes_to_use` 调小（如每实例 50G = `53687091200`）并在回传里注明实际值。

### 5.5 offload 专项（off vs on，路由策略固定）

跑前确认连接器起来了、容量对：

```bash
docker logs vllm-0 2>&1 | grep "SimpleCPUOffloadConnector"
# 期望类似：SimpleCPUOffloadConnector: role=..., per_rank=100.00 GB, world_size=1, mode=...
```

off vs on 同并发要回传的 delta（我据此判 offload 有没有正收益）：

| 维度 | 数据来源 | 期望（offload 有效时） |
|---|---|---|
| external 前缀命中 | 5.3 `external_prefix_cache_hits/queries`（off≈0→on>0） | on 出现可观命中 |
| TTFT p95/p99 | 5.1 driver | on ↓（省了重 prefill） |
| 吞吐 turns/s | 5.1 driver | 同并发 on ↑ |
| 抢占 | 5.3 `num_preemptions_total` | on ↓ |
| GPU KV 占用 | 5.3 `kv_cache_usage_perc` | on 不增甚至降 |
| 主机内存 | 5.4 `host_mem.log` | on 明显上升 = 池在用 |

> 若 on 的 external 命中很低或 TTFT 没降，多半是**前缀复用没溢出到 CPU**（GPU 缓存就够装，offload 用不上）或拷贝开销盖过收益——**如实回传即结论**，不用强行调成正收益。

### 5.6 回传清单（打个包给我）

1. 每个 run 的 `/tmp/<label>.txt`（driver stdout 全文）。
2. 每个 run 的 `/tmp/snap_<label>/`（router.log / engine.log / gpu.log / host_mem.log）。
3. 填好的 **5.1 汇总表** + **5.5 offload delta 表**。
4. 引擎计数器的 run 前/后两次快照（若没在 snap 里覆盖到，单独 `curl localhost:800i/metrics > before/after`）。
5. 任何异常的 `docker logs router` / `docker logs vllm-<i>` 相关片段。

## 6. 稳定性测试（**确保 router 线上不出问题**）

性能对比证明"更优"，稳定性测试证明"敢上线"。下面每项都是线上真会遇到的状况：引擎挂、引擎假死、过载、脏 header、长时间跑。**判据统一**：router 进程**不崩**（容器一直 `Up`、`/health` 200）、不卡死、内存/FD 不无限涨、异常请求不变成裸 500、负载恢复后能自愈。

先起一个 router 资源监控（所有稳定性子项共用，跑前启动、全程开着）：

```bash
mon_router() {        # M=$(mon_router); ... ; kill $M
  ( while true; do ts=$(date +%s)
      mem=$(docker stats --no-stream --format '{{.MemUsage}} cpu={{.CPUPerc}}' router)
      fd=$(docker exec router sh -c 'ls /proc/1/fd 2>/dev/null | wc -l')
      echo "$ts $mem fds=$fd"; sleep 30
    done ) >> /tmp/router_resource.log & echo $!
}
M=$(mon_router)
```

### 6.1 引擎故障注入（**最关键**：线上单卡 OOM/被驱逐时 router 别跟着炸）

压测进行中（用 §3 的 `COMMON`，建议并发拉高些）杀掉一个引擎，再拉起：

```bash
# 压测在另一个终端跑着；这里注入故障
sleep 30; docker stop vllm-3                       # 模拟引擎挂
sleep 60; curl -s localhost:8100/metrics | grep 'vllm:current_qps'   # vllm-3 应≈0，其余接管
docker start vllm-3; sleep 60                      # 恢复后应重新接流
```

**判据**：① router 容器全程 `Up`、`/health` 200；② 已在 `vllm-3` 上的在飞请求会短暂报错（连接断），属正常，**之后错误归零**——新请求被死引擎守卫避开 `vllm-3`；③ 其余 7 卡 QPS 顶上来，client 不出现持续 5xx；④ `docker start` 后 `vllm-3` 的 `current_qps` 重新 > 0。看 `docker logs router` 不应有未捕获 traceback。

### 6.2 引擎假死 / 慢节点（不级联）

`docker pause` 比 `stop` 更阴险——连接在、不响应。验证 P2C 避让而非把整池拖垮：

```bash
docker pause vllm-5; sleep 90
curl -s localhost:8100/metrics | grep -E 'vllm:num_requests_waiting|vllm:current_qps'  # vllm-5 waiting 堆高/qps 掉，别的卡分流
docker unpause vllm-5
```

**判据**：`vllm-5` 排队堆高但**其余引擎 p99 不被拖崩**（对照 §3 无故障基线），router 不卡死；`unpause` 后自愈。这正是"去掉延迟硬门、改分级 P2C"要防的级联。

### 6.3 长时 soak / 资源泄漏

把数据集放全、跑够长（≥1–2h），盯 `router_resource.log`：

```bash
python scripts/replay_conv_ab.py $COMMON --max-records 0 --max-convs 0 --concurrency 128 --label soak | tee /tmp/soak.txt
```

**判据**：router RSS **趋平**（不持续爬升 = 无内存泄漏）、FD 数稳定（不爬升 = 连接/句柄不泄漏）、CPU 不打满到饱和、TTFT/E2E 不随时间单调漂高、`errors`/`exc` = 0。

### 6.4 亲和表有界（14k 对话不撑爆内存）

全量 14,423 个对话会往每副本的亲和 LRU 里塞 key。确认 `--lb-affinity-max-size` 真的封顶：

```bash
# router 起时显式给一个不大的上限，跑全量对话
start_router "--routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id --lb-affinity --lb-affinity-max-size 20000"
python scripts/replay_conv_ab.py $COMMON --max-convs 0 --label affinity_bound | tee /tmp/affbound.txt
```

**判据**：router RSS 在对话数远超 `max-size` 后仍**封顶不涨**（LRU 驱逐生效）；`load_balanced_affinity_hit_rate` 仍合理（小集群下驱逐少）。

### 6.5 多副本（线上是 4 worker/副本）

线上每副本 4 worker，必须验证多 worker 下不崩、计数一致、亲和按副本独立但**均衡不受影响**：

```bash
start_router "--routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id --lb-affinity --router-workers 4"
python scripts/replay_conv_ab.py $COMMON --label workers4 | tee /tmp/w4.txt
```

**判据**：无 worker 崩溃/重启（`docker logs router` 无 worker exit）；QPS 均衡与单 worker 相当；亲和命中率可能略低于单 worker（映射按副本独立，§7 已述），但 **QPS 均衡和正确性不受影响**、无 5xx。

### 6.6 过载自愈（不打成 fallback 风暴）

把并发推到远超 8 卡容量（如 512+），再撤回，验证"分级自卸"而非"窗口一超时全 fallback"（这正是你实测延迟硬门会崩的场景）：

```bash
python scripts/replay_conv_ab.py $COMMON --concurrency 768 --label overload | tee /tmp/overload.txt
```

**判据**：过载期 router 不崩、不出裸 500，请求被各引擎排队消化（`num_requests_waiting` 普遍升高而非集中一卡）；`load_balanced_affinity_shed_total` 随负载上升（在卸载）；并发撤回后 TTFT/E2E **自行回落**到基线，无残留 fallback。

### 6.7 脏 header 回归（中文/emoji conv_id —— 非 ASCII header 修过的坑）

线上 `X-Flow-Conversation-Id` 由会话信息生成，**带中文/emoji**。直接验证不再裸 500：

```bash
for cid in "对话-测试-✅" "用户123" "$(printf '\xf0\x9f\x98\x80')-emoji"; do
  curl -s -o /dev/null -w "%{http_code}\n" localhost:8100/v1/completions \
    -H "Content-Type: application/json" -H "Authorization: Bearer $APIKEY" \
    -H "X-Flow-Conversation-Id: $cid" \
    -d '{"model":"gemma4-31B-it-FP8","prompt":"hi","max_tokens":8}'
done
```

**判据**：全部 **200**（不是 500）；`docker logs router` 无 `UnicodeEncodeError`。这条直接对应线上那次"中文 conv_id → 裸 500 → 误 fallback 到 OpenRouter"。

### 6.8 稳定性回传清单

`/tmp/router_resource.log`（RSS/FD/CPU 时序）+ 上述各 `/tmp/*.txt` + 故障注入前后 `docker logs router` 片段 + 一句结论（每项 pass/fail）。

## 7. 多副本 / K8s 注意

- 多 router 副本时亲和映射**按副本独立**（内存），同对话命中别副本会按负载重摆——均衡不受影响，只少一次缓存命中（TTFT 偶发抬升）。跨副本一致亲和需共享存储（后续）。验证多副本时用 `--router-workers 1` 单副本能拿到亲和命中的上界。
- K8s 部署把 `--service-discovery static ...` 换成 `--service-discovery k8s --k8s-namespace .. --k8s-label-selector .. --k8s-port 8000`，其余路由参数不变；`--k8s-reconcile-interval` 保留默认以清 ghost 引擎（与死引擎守卫互补）。

## 8. 排错

- 三种模式 TTFT 没区别：多半**没开 `--enable-prefix-caching`**，或每轮间没排空缓存导致 roundrobin 也蹭到缓存——检查引擎启动参数、加 `sleep 90`/重启引擎。
- 新模式大量 5xx：基本不该出现（v1.3.1 已修非 ASCII header）；若有，`docker logs router` 看 traceback，确认 `--session-key` 拼写与 header 名一致。
- `per-engine QPS: (no current_qps yet)`：跑完才抓、窗口已衰减；压测**进行中**抓，或 `--request-stats-window` 调大。
- 某引擎 0 流量：报高 `num_requests_waiting` = P2C 正确避让；连不通 = 死引擎守卫避让，`docker logs vllm-<i>` 排查。
- prompt 超 `--max-model-len 16384`：会 400/422，但数据集 p95≈7k，几乎不会触发；若大量出现，检查是否误配成更小的 max-model-len。
- offload 版引擎起不来：看 `docker logs vllm-0`——多半是 `$OFFLOAD` JSON 被 bash 拆词，改用数组 `"${EXTRA[@]}"` 传；或镜像未注册 `SimpleCPUOffloadConnector`（本镜像实测已注册）。
- 收尾：
```bash
docker rm -f router $(for i in $(seq 0 7); do echo vllm-$i; done)
```
