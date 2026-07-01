#!/usr/bin/env bash
# One-shot, time-compressed real-engine validation for load_balanced_affinity's
# multi-replica aggregation / capacity signals / redis overhead / redis failure.
# Companion to docs/dev/router-load-balanced-affinity-multireplica-redis-validation.md.
#
# It assumes the N vLLM engines are ALREADY UP (this doc does not test routing
# quality, so no model reload / cache-drain sleeps). Time cuts vs the doc:
#   * dataset sliced ONCE to local disk (no repeated NFS large-row reads),
#   * NO between-run cache drain (we watch router-side counters/lag, not KV cache),
#   * capacity ladder trimmed to 3 points, correctness runs use a 5k slice.
# Real engines, ~1h total. Outputs + PASS/FAIL land in $OUT.
#
# Usage:
#   APIKEY=... [DS=...] [N_ENGINES=8] [REDIS_HOST=127.0.0.1] \
#     bash scripts/validate_multireplica_redis.sh
set -u
cd "$(dirname "$0")/.."

IMAGE=${IMAGE:-ssadds/production-stack-router:v1.3.1}
N_ENGINES=${N_ENGINES:-8}
APIKEY=${APIKEY:?set APIKEY (engine --api-key)}
DS=${DS:-/mnt/shared/sss/data/replay-logs-conv-avg5k.json}
MODEL=${MODEL:-gemma4-31B-it-FP8}
REDIS_HOST=${REDIS_HOST:-127.0.0.1}
REDIS=redis://${REDIS_HOST}:6379/0
OUT=${OUT:-/tmp/mrv_$(date +%m%d_%H%M)}; mkdir -p "$OUT"
RESULTS=$OUT/RESULTS.txt; : >"$RESULTS"

BACKENDS=$(for i in $(seq 0 $((N_ENGINES-1))); do echo -n "http://127.0.0.1:$((8000+i)),"; done | sed 's/,$//')
MODELS=$(for i in $(seq 0 $((N_ENGINES-1))); do echo -n "$MODEL,"; done | sed 's/,$//')
DS_SMALL=$OUT/ds_small.json
DRV="python scripts/replay_conv_ab.py --model $MODEL --session-header X-Flow-Conversation-Id --api-key $APIKEY --seed 2026"

say(){ echo -e "\n=== $* ===" | tee -a "$RESULTS"; }
pf(){ echo "[$1] $2" | tee -a "$RESULTS"; }   # $1=PASS/FAIL/INFO  $2=msg
csum(){ curl -s localhost:$1/metrics | awk '/^load_balanced_(placement|affinity_hit|affinity_shed)_total/{c+=$2} END{printf "%d",c}'; }
metric(){ curl -s localhost:$1/metrics | awk -v k="^$2" '$0~k{print $2; exit}'; }
ok_of(){ awk '/^turns=/{for(i=1;i<=NF;i++)if($i~/^ok=/){sub(/ok=/,"",$i);print $i}}' "$1"; }
codes_of(){ grep -o "codes={[^}]*}" "$1" | tail -1; }

# ---- setup: local dataset slice, redis, nginx, 2 replicas -------------------
say "SETUP"
[ -f "$DS_SMALL" ] || head -n 20000 "$DS" > "$DS_SMALL"
pf INFO "dataset slice -> $DS_SMALL ($(wc -l <"$DS_SMALL") lines)"

docker rm -f mrv-redis mrv-nginx router-a router-b >/dev/null 2>&1
# Redis on a bridge with a published port (NOT host net) so §4c can apply netem
# to the container's eth0 and delay ONLY redis traffic, not all loopback.
docker run -d --name mrv-redis -p ${REDIS_HOST}:6379:6379 --cap-add NET_ADMIN redis:7-alpine \
  redis-server --save "" --appendonly no --maxmemory 1gb --maxmemory-policy allkeys-lru >/dev/null
until docker exec mrv-redis redis-cli ping >/dev/null 2>&1; do sleep 1; done

cat >"$OUT/nginx.conf" <<'EOF'
events {}
http {
  upstream routers { server 127.0.0.1:8100; server 127.0.0.1:8101; }
  server { listen 8080;
    location / { proxy_pass http://routers; proxy_read_timeout 300s; proxy_buffering off;
      proxy_set_header X-Flow-Conversation-Id $http_x_flow_conversation_id; } }
}
EOF
docker run -d --name mrv-nginx --network host -v "$OUT/nginx.conf":/etc/nginx/nginx.conf:ro nginx:alpine >/dev/null

start_replicas(){  # $1 = memory|redis
  local store_args="--lb-affinity-store $1"
  [ "$1" = redis ] && store_args="$store_args --lb-affinity-redis-url $REDIS"
  for r in "router-a 8100" "router-b 8101"; do set -- $r
    docker rm -f "$1" >/dev/null 2>&1
    docker run -d --name "$1" --network host --entrypoint vllm-router "$IMAGE" \
      --host 0.0.0.0 --port "$2" --service-discovery static \
      --static-backends "$BACKENDS" --static-models "$MODELS" \
      --engine-stats-interval 2 --router-workers 4 \
      --routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id \
      --lb-affinity --lb-d-choices 2 $store_args >/dev/null
  done
  for p in 8100 8101; do until curl -sf localhost:$p/health >/dev/null; do sleep 1; done; done
}
start_replicas redis
docker logs router-a 2>&1 | grep -iq "store: redis" && pf PASS "replicas up, redis store attached" || pf FAIL "redis store not attached"

# ---- §1 multi-worker / multi-replica aggregation ---------------------------
say "§1 AGGREGATION (2 replicas x 4 workers via nginx RR)"
c0a=$(csum 8100); c0b=$(csum 8101)
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 5000 --max-convs 1000 \
     --concurrency 96 --max-tokens 128 --label agg | tee "$OUT/agg.txt"
OKN=$(ok_of "$OUT/agg.txt"); SA=$(( $(csum 8100)-c0a )); SB=$(( $(csum 8101)-c0b )); TOT=$((SA+SB))
pf INFO "counter delta: a=$SA b=$SB sum=$TOT  driver ok=$OKN  codes=$(codes_of "$OUT/agg.txt")"
awk -v t=$TOT -v o=$OKN 'BEGIN{d=(t>o?t-o:o-t); exit !(o>0 && d<=o*0.05)}' \
  && pf PASS "counters sum≈driver ok (<=5%): partition invariant holds across workers+replicas" \
  || pf FAIL "counter sum $TOT vs ok $OKN diverges >5% -> aggregation broken (check PROMETHEUS_MULTIPROC_DIR)"
ACT=$(metric 8100 router_active_requests); pf INFO "router-a active_requests=$ACT (livesum; expect ~half of 96)"
[ -n "$ACT" ] && pf PASS "router_active_requests present" || pf FAIL "router_active_requests missing"

# ---- §2 capacity signals: single-replica ladder, then 2-replica relief -----
say "§2 CAPACITY SIGNAL (single replica :8100, ladder)"
for C in 96 384 768; do
  ( while true; do echo "$(date +%s) lag=$(metric 8100 router_event_loop_lag_seconds) act=$(metric 8100 router_active_requests) cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' router-a)"; sleep 5; done ) >"$OUT/cap_c$C.log" & MON=$!
  $DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8100 --max-records 8000 --max-convs 1500 \
       --concurrency $C --max-tokens 128 --label cap_c$C | tee "$OUT/cap_c$C.txt"
  kill $MON 2>/dev/null
  pf INFO "c=$C peak lag=$(awk -F'lag=' '{print $2+0}' "$OUT/cap_c$C.log" | sort -n | tail -1)s"
done
say "§2b 2-replica relief @768 (compare per-replica lag vs single-replica @768)"
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 8000 --max-convs 1500 \
     --concurrency 768 --max-tokens 128 --label cap_2rep | tee "$OUT/cap_2rep.txt"
pf INFO "2-rep lag a=$(metric 8100 router_event_loop_lag_seconds) b=$(metric 8101 router_event_loop_lag_seconds) (should be < single-replica @768)"

# ---- §3 redis overhead ------------------------------------------------------
say "§3 REDIS OVERHEAD"
BENCH_REDIS_URL=$REDIS docker run --rm --network host --entrypoint python "$IMAGE" \
  -m scripts.bench_affinity_store 2>/dev/null | tee "$OUT/bench.txt" \
  || pf INFO "bench module not in image; run in a venv: BENCH_REDIS_URL=$REDIS python scripts/bench_affinity_store.py"
say "§3b e2e memory vs redis @192 (TTFT/lag should be near-identical)"
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 5000 --max-convs 1000 \
     --concurrency 192 --max-tokens 128 --label e2e_redis | tee "$OUT/e2e_redis.txt"
pf INFO "redis e2e lag a=$(metric 8100 router_event_loop_lag_seconds)"
docker exec mrv-redis redis-cli config resetstat >/dev/null
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 5000 --max-convs 1000 \
     --concurrency 96 --max-tokens 128 --label throttle >/dev/null
docker exec mrv-redis redis-cli info commandstats | grep -E 'cmdstat_(get|set):' | tee "$OUT/throttle_cmdstat.txt"
awk -F'[:,=]' '/cmdstat_get/{g=$3} /cmdstat_set/{s=$3} END{if(s<g)print "[PASS] SET("s")<GET("g"): write-throttle active"; else print "[FAIL] SET>=GET: throttle not working"}' "$OUT/throttle_cmdstat.txt" | tee -a "$RESULTS"
start_replicas memory
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 5000 --max-convs 1000 \
     --concurrency 192 --max-tokens 128 --label e2e_mem | tee "$OUT/e2e_mem.txt"
pf INFO "compare TTFT lines in $OUT/e2e_redis.txt vs e2e_mem.txt (expect ~equal)"
start_replicas redis

# ---- §4 redis failure & self-heal ------------------------------------------
say "§4 REDIS FAILURE"
e0=$(metric 8100 load_balanced_affinity_store_errors_total)
( $DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 6000 --max-convs 1200 \
       --concurrency 96 --max-tokens 128 --label redis_kill >"$OUT/redis_kill.txt" 2>&1 ) & DRVPID=$!
sleep 20; docker stop mrv-redis >/dev/null; pf INFO "redis STOPPED mid-run"; sleep 25
e1=$(metric 8100 load_balanced_affinity_store_errors_total)
docker start mrv-redis >/dev/null; until docker exec mrv-redis redis-cli ping >/dev/null 2>&1; do sleep 1; done
pf INFO "redis RESTARTED"; wait $DRVPID
awk -v a="${e0:-0}" -v b="${e1:-0}" 'BEGIN{exit !(b>a)}' \
  && pf PASS "store_errors_total rose while redis down ($e0->$e1) = fail-open engaged" \
  || pf FAIL "no store errors recorded during outage (unexpected)"
grep -qE "codes=\{[^}]*(500|502|503|'exc'[^0])" "$OUT/redis_kill.txt" \
  && pf FAIL "5xx/exc during redis outage -> NOT fail-open (codes=$(codes_of "$OUT/redis_kill.txt"))" \
  || pf PASS "no 5xx during redis outage: routing continued (codes=$(codes_of "$OUT/redis_kill.txt"))"
docker logs router-a 2>&1 | grep -qi "Traceback" && pf FAIL "router traceback during outage" || pf PASS "no uncaught traceback"

say "§4b startup with redis unreachable (soft dependency)"
docker stop mrv-redis >/dev/null
docker rm -f router-c >/dev/null 2>&1
docker run -d --name router-c --network host --entrypoint vllm-router "$IMAGE" \
  --host 0.0.0.0 --port 8102 --service-discovery static --static-backends "$BACKENDS" --static-models "$MODELS" \
  --routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id \
  --lb-affinity-store redis --lb-affinity-redis-url "$REDIS" >/dev/null
sleep 8
curl -sf localhost:8102/health >/dev/null && pf PASS "router started WITHOUT redis (soft dep)" || pf FAIL "router failed to start without redis"
docker logs router-c 2>&1 | grep -qi "unreachable at startup" && pf PASS "logged startup warning" || pf INFO "no startup warning found"
docker rm -f router-c >/dev/null; docker start mrv-redis >/dev/null
until docker exec mrv-redis redis-cli ping >/dev/null 2>&1; do sleep 1; done

say "§4c redis SLOW (200ms netem >> 50ms timeout): tight timeout must not stall routing"
docker exec mrv-redis sh -c 'apk add -q iproute2 2>/dev/null; tc qdisc add dev eth0 root netem delay 200ms' 2>/dev/null \
  && { es0=$(metric 8100 load_balanced_affinity_store_errors_total)
       $DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 4000 --max-convs 800 \
            --concurrency 96 --max-tokens 128 --label redis_slow | tee "$OUT/redis_slow.txt"
       docker exec mrv-redis tc qdisc del dev eth0 root 2>/dev/null
       es1=$(metric 8100 load_balanced_affinity_store_errors_total)
       pf INFO "slow-redis: errors $es0->$es1, lag=$(metric 8100 router_event_loop_lag_seconds), codes=$(codes_of "$OUT/redis_slow.txt")"
       grep -qE "codes=\{[^}]*(500|502|503)" "$OUT/redis_slow.txt" && pf FAIL "5xx under slow redis" || pf PASS "no 5xx under slow redis (timeout fail-open)"; } \
  || pf INFO "skipped slow-redis (tc/NET_ADMIN unavailable)"

say "§4d opt-in hard-fail (--lb-affinity-redis-required) must crash when redis down"
docker stop mrv-redis >/dev/null
docker run --rm --network host --entrypoint vllm-router "$IMAGE" \
  --host 0.0.0.0 --port 8103 --service-discovery static --static-backends "$BACKENDS" --static-models "$MODELS" \
  --routing-logic load_balanced_affinity --session-key X-Flow-Conversation-Id \
  --lb-affinity-store redis --lb-affinity-redis-url "$REDIS" --lb-affinity-redis-required >"$OUT/hardfail.txt" 2>&1
[ $? -ne 0 ] && pf PASS "redis-required exited non-zero when redis down (hard-fail opt-in works)" || pf FAIL "redis-required did NOT hard-fail"
docker start mrv-redis >/dev/null; until docker exec mrv-redis redis-cli ping >/dev/null 2>&1; do sleep 1; done

# ---- §5 cross-replica affinity: redis vs memory (redis's value) ------------
say "§5 CROSS-REPLICA AFFINITY (RR spreads a conv across replicas)"
hitrate(){ metric 8100 load_balanced_affinity_hit_rate; }
start_replicas redis
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 6000 --max-convs 1200 \
     --concurrency 96 --max-tokens 128 --label xrep_redis | tee "$OUT/xrep_redis.txt"
HR_REDIS=$(hitrate)
start_replicas memory
$DRV --dataset "$DS_SMALL" --router http://127.0.0.1:8080 --max-records 6000 --max-convs 1200 \
     --concurrency 96 --max-tokens 128 --label xrep_mem | tee "$OUT/xrep_mem.txt"
HR_MEM=$(hitrate)
pf INFO "affinity_hit_rate: redis=$HR_REDIS  memory=$HR_MEM"
awk -v r="${HR_REDIS:-0}" -v m="${HR_MEM:-0}" 'BEGIN{exit !(r>m)}' \
  && pf PASS "redis hit_rate > memory: shared store keeps affinity across replicas" \
  || pf FAIL "redis not better than memory (check nginx RR / redis connectivity)"

say "DONE -> $OUT ; summary:"; grep -E '^\[(PASS|FAIL)\]' "$RESULTS"
echo -e "\ncleanup: docker rm -f mrv-redis mrv-nginx router-a router-b"
