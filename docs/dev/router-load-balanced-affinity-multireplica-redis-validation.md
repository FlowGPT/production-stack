# load_balanced_affinity 真机验证：多副本聚合 / 容量信号 / Redis 开销与故障

这份**不测路由质量**（那在 `router-load-balanced-affinity-h100-validation.md`），只压四件线上必须心里有底的事：

1. **多副本 / 多 worker 的指标聚合是否正确**（你之前踩的"总 QPS 接近 10 但面板显示 5.57"）。
2. **"router 扛不住"的信号是否可靠**（`router_event_loop_lag_seconds` / `router_active_requests` 能不能当扩容触发）。
3. **Redis 开销真实值**（打真实跨网 RTT，不是 localhost）。
4. **Redis 挂掉 / 变慢 / 启动不可达的影响 + 自愈**（软依赖是否真软）。

外加我补的两项：**跨副本亲和一致性**（redis 的正价值证明）和 **redis 硬依赖 opt-in 反证**。

> 镜像：router `ssadds/production-stack-router:v1.3.1`。引擎沿用 H100 文档 §1 起法（8×H100 gemma4-31B-it-FP8）。若 GPU 紧张，本文的**聚合/redis/开销**几项对引擎延迟不敏感，可只起 2–3 个引擎；只有 §2「容量信号」想看真实 event-loop-lag 拐点时，引擎越多越能把并发顶上去。

## 快速路径（一键脚本，真引擎，~1h）

引擎已起好后，直接：

```bash
APIKEY=<engine-api-key> DS=/mnt/shared/sss/data/replay-logs-conv-avg5k.json \
  bash scripts/validate_multireplica_redis.sh
# 结果 + PASS/FAIL 落在 /tmp/mrv_<时间>/RESULTS.txt
```

脚本自动起 redis+nginx+2 副本×4worker，串行跑完 §1–§5 并收集数据、能算的直接判 PASS/FAIL。相对下面手册流程的**提速点**（本文档只看 router 侧行为，与引擎缓存质量无关，故可砍）：

- **数据只切一次到本地**（`head` 出 2 万行到 `/tmp`），之后所有 run 读本地，不再每次啃 NFS 大文件；
- **不排空缓存**（不比路由质量 → 省掉所有 `sleep 90`）；
- **§2 爬坡砍到 3 档**（96/384/768），correctness 项用 5k 样本。

代价：**没有真实 TTFT/prefill 绝对数字**（那归 H100 文档）。想看某一项细节或调参数，仍按下面手册章节单独跑。收尾 `docker rm -f mrv-redis mrv-nginx router-a router-b`。

---

## 0. 拓扑（关键：必须多副本 + 多 worker + 前置 LB）

线上是**多副本、每副本 4 worker、前面一层 LB**。要复现聚合与跨副本亲和，本地就得搭同构的最小版：

```
                     ┌──────────── nginx (RR, :8080) ───────────┐
   driver ─────────► │  round-robin，故意不 sticky              │
                     └──────┬───────────────────────┬───────────┘
                            ▼                        ▼
                  router-A (:8100, 4 worker)  router-B (:8101, 4 worker)
                            └───────────┬────────────┘
                                        ▼
                                  redis (:6379)         ← 共享亲和表
                                        │
                            8× vLLM 引擎 (:8000..8007)
```

- **nginx 用 round-robin 而非 sticky**：这是有意的。只有让同一 `conv_id` 随机打到 A/B 两副本，才能证明 **redis 让亲和跨副本一致**（memory 则会稀释）。线上真部署会在 ingress 上加 sticky，这里为了测试反其道而行。
- **每副本 4 worker**：复现线上，暴露 worker 间聚合问题。

### 0.1 起 redis + nginx

```bash
docker run -d --name redis --network host redis:7-alpine \
  redis-server --save "" --appendonly no --maxmemory 512mb --maxmemory-policy allkeys-lru

cat >/tmp/nginx-router.conf <<'EOF'
events {}
http {
  upstream routers { server 127.0.0.1:8100; server 127.0.0.1:8101; }  # 默认 RR
  server {
    listen 8080;
    location / {
      proxy_pass http://routers;
      proxy_read_timeout 300s; proxy_buffering off;   # 别缓冲 SSE 流
      proxy_set_header X-Flow-Conversation-Id $http_x_flow_conversation_id;
    }
  }
}
EOF
docker run -d --name nginx-router --network host -v /tmp/nginx-router.conf:/etc/nginx/nginx.conf:ro nginx:alpine
```

### 0.2 起两个 router 副本（redis 共享 + 4 worker）

```bash
REDIS=redis://127.0.0.1:6379/0
BACKENDS=$(for i in $(seq 0 7); do echo -n "http://127.0.0.1:$((8000+i)),"; done | sed 's/,$//')
MODELS=$(for i in $(seq 0 7); do echo -n "gemma4-31B-it-FP8,"; done | sed 's/,$//')
APIKEY=kaon-myTTpAVQsE06ogbKfBA9bZePR3pQ1QPy

start_replica() {   # $1=name $2=port  $3=额外参数
  docker rm -f "$1" >/dev/null 2>&1
  docker run -d --name "$1" --network host --entrypoint vllm-router \
    ssadds/production-stack-router:v1.3.1 --host 0.0.0.0 --port "$2" \
    --service-discovery static --static-backends "$BACKENDS" --static-models "$MODELS" \
    --engine-stats-interval 2 --router-workers 4 \
    --routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id \
    --lb-affinity --lb-d-choices 2 \
    --lb-affinity-store redis --lb-affinity-redis-url "$REDIS" $3
  until curl -sf localhost:$2/health; do sleep 1; done; echo " $1 up on $2"
}

start_replica router-a 8100
start_replica router-b 8101
docker logs router-a 2>&1 | grep -iE "load_balanced_affinity store: redis"   # 确认接上 redis
```

### 0.3 驱动器指向 nginx（:8080）

沿用 `scripts/replay_conv_ab.py`，只把 `--router` 换成 nginx：

```bash
DS=/mnt/shared/sss/data/replay-logs-conv-avg5k.json
COMMON="--dataset $DS --router http://127.0.0.1:8080 --model gemma4-31B-it-FP8 \
        --session-header X-Flow-Conversation-Id --api-key $APIKEY \
        --max-convs 3000 --max-records 30000 --max-tokens 256 --seed 2026"
```

---

## 1. 验证一：多副本 / 多 worker 指标聚合正确性

**核心事实**（先记住，否则会看错数）：
- **Counter**（`*_total`）在多 worker 下**自动求和**（`/metrics` 走 `MultiProcessCollector`）。跨副本再靠 Prometheus `sum()`。
- **Gauge 分三种聚合模式**（代码里写死）：
  - `router_active_requests` = **livesum**（跨 worker 求和）→ 直接是本副本真实并发。
  - `router_event_loop_lag_seconds` = **livemax**（取最差 worker）→ 本副本最坏调度延迟。
  - `vllm:current_qps` / `num_requests_*` 等窗口 gauge = **mostrecent**（只保留某一个 worker 最后写的值）→ **多 worker 下会欠计**，不能直接当集群量。

**结论先行**：集群 QPS **不要**用 `sum(vllm:current_qps)`（欠计 + mostrecent 陷阱），要用 **counter 速率**：

```promql
# 集群总 QPS（正确）= 三个分区计数器速率之和，Prometheus 自动跨 worker+副本聚合
sum(rate(load_balanced_placement_total[1m])
  + rate(load_balanced_affinity_hit_total[1m])
  + rate(load_balanced_affinity_shed_total[1m]))
```

### 1.1 对拍：counter 聚合 == client 实发

跑一轮，跑完分别抓两副本的 `/metrics`，把三个 counter 加起来，和 driver 打印的成功请求数对齐：

```bash
python scripts/replay_conv_ab.py $COMMON --concurrency 96 --label agg_check | tee /tmp/agg.txt

sum_counter() {   # $1=port
  curl -s localhost:$1/metrics | awk '
    /^load_balanced_placement_total/      {p=$2}
    /^load_balanced_affinity_hit_total/   {h=$2}
    /^load_balanced_affinity_shed_total/  {s=$2}
    END{printf "  port%s placement=%d hit=%d shed=%d  SUM=%d\n",'$1',p,h,s,p+h+s}'
}
sum_counter 8100; sum_counter 8101
```

**判据**：
- 每副本内 `placement+hit+shed` 已是 4 个 worker 之和（不是单 worker 的 1/4）。
- 两副本 `SUM` 相加 ≈ driver 的 `ok`（成功请求数）。**这条不成立 = 聚合坏了**（多半 `PROMETHEUS_MULTIPROC_DIR` 没生效，见排错）。
- RR 下两副本 `SUM` 应大致相当（各接一半流量）。

### 1.2 对拍：集群 QPS（counter 速率）== driver 实发 turns/s

压测**进行中**，多次采样上面的 PromQL（或没接 Prometheus 时手算 counter 差值 / 时间窗）：

```bash
# 无 Prometheus 时的手算版：15s 内两副本 counter 增量 / 15
q() { curl -s localhost:$1/metrics | awk '/^load_balanced_(placement|affinity_hit|affinity_shed)_total/{c+=$2} END{print c}'; }
a=$(( $(q 8100)+$(q 8101) )); sleep 15; b=$(( $(q 8100)+$(q 8101) ))
echo "cluster QPS = $(( (b-a)/15 ))"
```

**判据**：这个值 ≈ driver 的 `throughput=.. turns/s`。**对比反例**：同时抓 `sum(vllm:current_qps)`，它会**明显偏低**（就是你上次看到的 10→5.57）——把两个数一起回传，坐实"gauge 欠计、counter 正确"。

### 1.3 livesum / livemax gauge 正确性

压测中抓本副本 `router_active_requests`：

```bash
curl -s localhost:8100/metrics | grep -E '^router_active_requests|^router_event_loop_lag_seconds'
```

**判据**：
- `router_active_requests`（单副本）≈ 打到该副本的活跃并发（RR 下约为总并发的一半）。**若它只有真实值的 ~1/4，说明 livesum 没生效**（退化成单 worker）。
- `router_event_loop_lag_seconds` 是一个数（最差 worker），不是 4 条 per-pid series。空载应 < 5ms。

---

## 2. 验证二：router 扛不住信号可靠性

目标：证明 `router_event_loop_lag_seconds` / `router_active_requests` **随 router 饱和单调上升、且领先于用户可感知的 TTFT 劣化**，可当扩容触发。

### 2.1 单副本阶梯加压，记录信号曲线

先只留一个副本（把 nginx upstream 改成只有 8100，或 driver 直连 8100），阶梯加并发：

```bash
mon() { ( while true; do ts=$(date +%s)
    lag=$(curl -s localhost:8100/metrics | awk '/^router_event_loop_lag_seconds/{print $2}')
    act=$(curl -s localhost:8100/metrics | awk '/^router_active_requests/{print $2}')
    cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' router-a)
    echo "$ts lag=$lag active=$act cpu=$cpu"; sleep 5
  done ) >> /tmp/cap_$1.log & echo $!; }

for C in 48 96 192 384 768 1152; do
  P=$(mon c$C)
  python scripts/replay_conv_ab.py $COMMON --router http://127.0.0.1:8100 --concurrency $C --label cap_c$C | tee /tmp/cap_c$C.txt
  kill $P
done
```

**判据 / 怎么定阈值**：
- 画 `lag` vs 并发。空载几 ms；某并发后 `lag` 拐头（进入几十 ms～100ms+），**且该点 driver 的 TTFT p95 开始明显抬升** → 这个 lag 拐点就是"该加副本"的阈值。经验建议：**`router_event_loop_lag_seconds` p95 持续 > 50ms 报警、> 100ms 扩容**（按你实测拐点微调）。
- `router_active_requests` 长期贴近你设定的软上限（如接近 `max-num-seqs × 引擎数`，说明引擎已满、router 在堆积）→ 辅助信号。
- `cpu` 若单 worker 打满（多 worker 下看 4 核是否都满）也印证饱和。

### 2.2 反证：加副本后同并发信号回落

在把单副本压到 lag 明显抬高的那个并发（例 768），恢复双副本（nginx 两台）再跑一次：

```bash
python scripts/replay_conv_ab.py $COMMON --concurrency 768 --label cap_2rep_c768 | tee /tmp/cap_2rep.txt
# 分别抓 router-a / router-b 的 lag
```

**判据**：双副本时**每副本的 `lag` 和 `active_requests` 明显低于**单副本同并发的值，TTFT 也回落 → 证明这两个信号确实反映"router 层"容量、加副本能解，可靠地当扩容依据。

---

## 3. 验证三：Redis 开销真实值（打真实 RTT）

### 3.1 微基准（在 router 所在节点跑，指向真实 redis）

```bash
# 关键：BENCH_REDIS_URL 指向真实（跨节点）redis，别用 localhost
BENCH_REDIS_URL=redis://<真实redis_host>:6379/0 \
  docker run --rm --network host --entrypoint python \
  ssadds/production-stack-router:v1.3.1 -m scripts.bench_affinity_store
# 若脚本不在镜像里，就在装了本仓库的 venv 里：
# BENCH_REDIS_URL=redis://<host>:6379/0 .venv/bin/python scripts/bench_affinity_store.py
```

**要回传的行**：`memory store (get+put)`、`redis store (get+put, 2 RTT)`、`raw redis GET (1 RTT)`、`redis adds ~Xus`、以及 `_choose w/ memory` vs `_choose w/ redis`。

**判读**：
- localhost 基线约：memory get+put ~1µs、redis get+put ~75µs、`_choose` ~83µs。**跨网** redis 会加上真实 RTT（每 RTT 常见 0.2–1ms）——这就是要真机测的原因。
- **命中路径其实是 1 个 GET**（写节流跳过 TTL 刷新 SET），所以稳态开销 ≈ 1×RTT，不是 2×。3.3 验证节流。

### 3.2 端到端：memory vs redis 同负载对比

同一份回放，路由参数只切 store（其余全同）：

```bash
# redis 版（已在 §0.2 起）
python scripts/replay_conv_ab.py $COMMON --concurrency 192 --label e2e_redis | tee /tmp/e2e_redis.txt
# memory 版：重起两副本，把 --lb-affinity-store 换成 memory（去掉 redis-url）
# start_replica 里改 store=memory 后重跑
python scripts/replay_conv_ab.py $COMMON --concurrency 192 --label e2e_mem | tee /tmp/e2e_mem.txt
```

**判据**：
- redis 版的 **TTFT / E2E / turns-s 与 memory 版差异应在噪声级**（redis 调用几十µs～1ms 相对 prefill 秒级可忽略）。若 redis 版明显变慢，看是不是 redis RTT 异常大或超时频繁（查 `load_balanced_affinity_store_errors_total`）。
- 压测中抓 `router_event_loop_lag_seconds`：redis 版**不应比 memory 版显著高**。redis 客户端是同步调用跑在事件循环上，若 RTT 大会抬 lag——这是"redis 是否拖慢 router"的直接证据，务必回传两版 lag 对比。

### 3.3 写节流有效性（refresh_fraction）

redis 侧看 SET 次数被节流掉多少：

```bash
docker exec redis redis-cli config resetstat
python scripts/replay_conv_ab.py $COMMON --concurrency 96 --label throttle | tee /tmp/throttle.txt
docker exec redis redis-cli info commandstats | grep -E 'cmdstat_(get|set):'
```

**判据**：`cmdstat_set` 的 calls **明显少于** `cmdstat_get`（回访请求大多只刷 TTL，被节流）。对照组：两副本重起时加 `--lb-affinity-redis-refresh-fraction 0`（关节流），SET≈GET → 证明节流在起作用、省掉的正是无谓的 TTL 刷新写。

---

## 4. 验证四：Redis 故障影响 + 自愈（软依赖是否真软）

统一判据：**router 不崩、client 无 5xx、路由继续（退化为纯 P2C）**，redis 恢复后**无需重启**自愈。

### 4.1 运行中 redis 挂掉 → fail-open

压测进行中（§0.3 的 COMMON，并发 192），拔掉 redis：

```bash
# 另一终端压测跑着
sleep 30; docker stop redis
sleep 60
curl -s localhost:8100/metrics | grep -E 'load_balanced_affinity_store_errors_total|load_balanced_affinity_hit_rate'
docker start redis; sleep 90    # 自愈
curl -s localhost:8100/metrics | grep -E 'load_balanced_affinity_store_errors_total|load_balanced_affinity_hit_rate'
```

**判据**：
- redis 停后：driver **无 5xx/exc**（路由继续，走 P2C）；`load_balanced_affinity_store_errors_total{operation="get"/"put"}` **开始上升**；`affinity_hit_rate` 掉（拿不到共享记忆）；`docker logs router-a` 是 warning 级 `redis get/put failed`，**无未捕获 traceback**。
- redis 恢复后：error 计数**停止增长**，`affinity_hit_rate` **回升**——**router 没重启**就自愈。这条是"软依赖 + 自愈"的核心证据。

### 4.2 启动时 redis 不可达（软依赖）→ 照常起

```bash
docker stop redis
start_replica router-c 8102     # redis 不在，仍应起得来
curl -sf localhost:8102/health && echo " started WITHOUT redis (soft dep OK)"
docker logs router-c 2>&1 | grep -i "redis .* unreachable at startup"   # 应有 warning
docker start redis; sleep 90
# 此后 router-c 的请求应自动开始命中 redis（affinity_hit_rate 从 0 回升），无需重启
```

**判据**：redis 不在时 router-c **正常 `/health` 200**、日志有"unreachable at startup … starting fail-open"warning；redis 起来后自动开始用（hit_rate 回升）。**这直接对应你的要求"redis 不能是强依赖"。**

### 4.3 redis 变慢（超时）→ 紧超时兜住，不阻塞路由

给 redis 注入 200ms 网络延迟（远超默认 50ms 超时），验证每次调用 fail-open 而非把事件循环挂住：

```bash
docker run -d --name redis-slow --network host --cap-add NET_ADMIN redis:7-alpine \
  redis-server --save "" --appendonly no
docker exec redis-slow sh -c 'apk add -q iproute2; tc qdisc add dev eth0 root netem delay 200ms'
# 把两副本 redis-url 指到这个慢 redis（同 6379，先停原 redis 或换端口），压测
python scripts/replay_conv_ab.py $COMMON --concurrency 96 --label redis_slow | tee /tmp/redis_slow.txt
curl -s localhost:8100/metrics | grep -E 'router_event_loop_lag_seconds|load_balanced_affinity_store_errors_total'
```

**判据**：慢 redis 下 **TTFT/E2E 不被拖成 200ms×N**（每次调用 50ms 超时即 fail-open）；`store_errors_total` 快速上升；`router_event_loop_lag_seconds` **不应飙到几百 ms**（超时紧、且是每次单发）。若 lag 明显被抬高，记录下来——说明同步 redis 客户端在慢 redis 下会污染事件循环，是要不要改异步 redis 的依据。

### 4.4 opt-in 硬依赖反证（`--lb-affinity-redis-required`）

证明"想要强一致的人仍可选硬失败"：

```bash
docker stop redis
docker run --rm --network host --entrypoint vllm-router \
  ssadds/production-stack-router:v1.3.1 --host 0.0.0.0 --port 8103 \
  --service-discovery static --static-backends "$BACKENDS" --static-models "$MODELS" \
  --routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id \
  --lb-affinity-store redis --lb-affinity-redis-url "$REDIS" --lb-affinity-redis-required \
  2>&1 | tail -5    # 应直接报错退出（非 0），K8s 下即 crash-loop
```

**判据**：**非 0 退出 + 明确报错**（不是静默起来）。这是默认软依赖的对照，证明硬失败仍可 opt-in。

---

## 5. 验证五（补充）：跨副本亲和一致性（redis 的正价值）

前面都在证"redis 挂了不坏"，这条证"redis 存在时值得"。**RR 让同 conv 打到两副本**，对比 redis vs memory 的亲和命中：

```bash
# A) redis 版（§0.2 已起），经 nginx :8080（RR 两副本）
python scripts/replay_conv_ab.py $COMMON --router http://127.0.0.1:8080 --concurrency 96 --label xrep_redis | tee /tmp/xrep_redis.txt
Rr=$(( $(curl -s localhost:8100/metrics|awk '/^load_balanced_affinity_hit_total/{print $2}') \
      + $(curl -s localhost:8101/metrics|awk '/^load_balanced_affinity_hit_total/{print $2}') ))

# B) memory 版：两副本改 --lb-affinity-store memory 重起，同样经 nginx RR
python scripts/replay_conv_ab.py $COMMON --router http://127.0.0.1:8080 --concurrency 96 --label xrep_mem | tee /tmp/xrep_mem.txt
```

**判据**：**redis 版跨副本 `affinity_hit_rate` 明显高于 memory 版**（memory 下同 conv 被 RR 分到两副本、各记各的 → 命中稀释）。这就是引入 redis 的量化收益：**多副本 + 无 ingress sticky 时仍保住亲和**。同时对照 TTFT——redis 版应更低（前缀缓存跨副本也吃得到）。

---

## 6. 回传清单

1. **§1 聚合**：`sum_counter` 两副本输出 + driver `ok`；集群 QPS（counter 速率）对照 `sum(vllm:current_qps)`（坐实 gauge 欠计）；`router_active_requests` 单副本值 vs 真实并发。
2. **§2 容量信号**：`/tmp/cap_*.log`（lag/active/cpu vs 并发曲线）+ 各 `/tmp/cap_c*.txt` 的 TTFT；单副本 vs 双副本同并发的 lag 对比；你据此定的报警/扩容阈值。
3. **§3 redis 开销**：`bench_affinity_store` 全文（真实 redis URL）；memory vs redis 端到端 TTFT/E2E/turns-s + 两版 `event_loop_lag`；节流 SET/GET commandstats（fraction=0.5 vs 0）。
4. **§4 故障**：4.1 挂 redis 前后的 `store_errors_total`/`hit_rate` + `docker logs`（证无 traceback、恢复自愈）；4.2 无 redis 启动的 `/health` + warning 日志；4.3 慢 redis 的 TTFT + lag + error；4.4 硬依赖退出码与报错。
5. **§5 跨副本**：redis vs memory 的 `affinity_hit_rate` 与 TTFT 对比。
6. 每项一句 pass/fail 结论。

---

## 7. 排错

- **counter 两副本相加 ≠ driver ok**：多半多 worker 的 `PROMETHEUS_MULTIPROC_DIR` 没生效——确认 `--router-workers 4` 生效（`docker logs` 有 4 个 worker 启动），且 `/metrics` 走了 MultiProcessCollector（镜像 v1.3.1 已内置）。也可能 driver 侧有重试导致 counter 偏多。
- **`router_active_requests` 只有真实值 ~1/4**：livesum 没聚合，同上排查 multiproc 目录。
- **集群 QPS 用 `sum(vllm:current_qps)` 偏低**：这是预期（mostrecent gauge 欠计），换成 §1.2 的 counter 速率。
- **redis 版 event-loop-lag 明显高于 memory**：redis RTT 太大或超时频繁；查 `store_errors_total`、redis 是否跨机房、`--lb-affinity-redis-timeout` 是否被调大。
- **挂 redis 后出现 5xx**：不应发生（fail-open）；若有，看 `docker logs router` traceback，确认镜像是 v1.3.1（含软依赖 + fail-open）。
- **4.3 tc 报错**：容器需 `--cap-add NET_ADMIN` 且装了 `iproute2`；或改用 toxiproxy 注延迟。
- 收尾：
```bash
docker rm -f nginx-router redis redis-slow router-a router-b router-c
```
