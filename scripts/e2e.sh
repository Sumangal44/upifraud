#!/usr/bin/env bash
# ============================================================================
# e2e.sh — full pipeline: BUILD → UP → SEED → TRAFFIC → VERIFY
#
#   ./scripts/e2e.sh               build images, start stack, run all checks
#   ./scripts/e2e.sh --skip-build  reuse existing images
#   ./scripts/e2e.sh --down        tear the stack down at the end
#
# Exit code 0 = every check passed. Prints a PASS/FAIL summary table.
# ============================================================================
set -uo pipefail

cd "$(dirname "$0")/.."

SKIP_BUILD=0
TEARDOWN=0
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    --down)       TEARDOWN=1 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

COMPOSE="docker compose"
# Docker Desktop VMs are often memory-constrained; parallel image builds
# (3x cargo release + next build) can OOM-kill steps. Build serially by default.
export COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"
GATEWAY=http://localhost:8080
STREAM_API=http://localhost:8081
INFERENCE=http://localhost:8001
XAI=http://localhost:8002
PORTAL=http://localhost:3000

PASS=0; FAIL=0; FAILED_NAMES=()

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

check() {  # check <name> <ok:0/1> [detail]
  local name="$1" ok="$2" detail="${3:-}"
  if [[ "$ok" == "0" ]]; then
    green "  PASS  $name${detail:+ — $detail}"
    PASS=$((PASS + 1))
  else
    red   "  FAIL  $name${detail:+ — $detail}"
    FAIL=$((FAIL + 1)); FAILED_NAMES+=("$name")
  fi
}

wait_http() {  # wait_http <url> <grep_pattern> <timeout_s>
  local url="$1" pattern="$2" timeout="${3:-120}" i=0
  while (( i < timeout )); do
    if curl -fsS --max-time 3 "$url" 2>/dev/null | grep -q "$pattern"; then return 0; fi
    sleep 2; i=$((i + 2))
  done
  return 1
}

tx() {  # tx <src> <dst> <amount> <device> <ip> [rooted]
  curl -fsS --max-time 10 -X POST "$GATEWAY/v1/transactions" \
    -H 'Content-Type: application/json' -d "{
      \"timestamp_ms\": $(($(date +%s) * 1000)),
      \"src_vpa\": \"$1\", \"dst_vpa\": \"$2\", \"amount_inr\": $3,
      \"device\": {\"device_hash\": \"$4\", \"rooted\": ${5:-false}},
      \"src_ip\": \"$6\", \"geo\": {\"lat\": 19.07, \"lon\": 72.87, \"city\": \"Mumbai\"}
    }" | sed -n 's/.*"tx_id":"\([^"]*\)".*/\1/p'
}

json_field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

# ---------------------------------------------------------------------------
step "1/7  Build images"
if (( SKIP_BUILD )); then
  echo "  (skipped)"
  $COMPOSE --profile tools build >/dev/null 2>&1 || true   # ensure loadgen image exists
else
  $COMPOSE --profile tools build 2>&1 | grep -E "Built|ERROR|error" | sed 's/^/  /'
  check "docker compose build" "${PIPESTATUS[0]}"
fi

# ---------------------------------------------------------------------------
step "2/7  Start platform"
$COMPOSE up -d 2>&1 | sed 's/^/  /'

step "3/7  Wait for services"
for svc in "gateway:$GATEWAY/healthz|kafka\":\"ok" \
           "stream-api:$STREAM_API/healthz|status\":\"ok" \
           "inference:$INFERENCE/healthz|status\":\"ok" \
           "xai:$XAI/healthz|status\":\"ok"; do
  name="${svc%%:*}"; url_pat="${svc#*:}"; url="${url_pat%%|*}"; pat="${url_pat#*|}"
  if wait_http "$url" "$pat" 180; then check "$name healthy" 0
  else check "$name healthy" 1 "timeout waiting for $url"; fi
done
if wait_http "$PORTAL" "UPI" 120; then check "portal healthy" 0
else check "portal healthy" 1 "timeout waiting for $PORTAL"; fi

# ---------------------------------------------------------------------------
step "4/7  Seed demo data (mule ring, devices, IPs)"
if $COMPOSE --profile tools run --rm --no-deps seed 2>&1 | sed 's/^/  /' | tail -6; then
  check "seed completed" 0
else
  check "seed completed" 1
fi

# ---------------------------------------------------------------------------
step "5/7  Fraud scenario: WS probe first, then benign + mule-burst traffic"
WS_CID=$($COMPOSE --profile tools run -d --no-deps --entrypoint python \
           loadgen -m tools.loadgen.ws_probe 2>/dev/null | tail -1)
echo "  ws probe container: ${WS_CID:-none}"
sleep 5

BENIGN_TX=$(tx user01@upidemo user02@upidemo 499 dev-user01 ip-user01)
echo "  benign tx: ${BENIGN_TX:-none}"

# Mule burst: one victim pays the seeded mule ring repeatedly from the shared
# ring device/IP — high velocity + shared device + high amounts + fast paths.
BURST_IDS=""
for m in mule01@muledemo mule02@muledemo mule03@muledemo mule01@muledemo mule02@muledemo; do
  id=$(tx user05@upidemo "$m" 48000 dev-ring-a 10.66.0.1)
  BURST_IDS="$BURST_IDS $id"
  sleep 0.4
done
echo "  burst txs:${BURST_IDS:-none}"

sleep 6   # let the worker score + publish

WS_LOG=$($COMPOSE --profile tools logs "$WS_CID" 2>/dev/null | tail -5 || true)
if [[ -z "$WS_CID" ]]; then
  check "ws alert received" 1 "probe container not started"
elif docker wait "$WS_CID" >/dev/null 2>&1 && \
     docker logs "$WS_CID" 2>&1 | grep -q "WS_ALERT_OK"; then
  check "ws alert received" 0 "$(docker logs "$WS_CID" 2>&1 | grep WS_ALERT_OK)"
else
  check "ws alert received" 1 "$(echo "$WS_LOG" | tail -2)"
fi
docker rm -f "$WS_CID" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
step "6/7  Verify decisions, graph, explanation, stats"
DEC=$($COMPOSE exec -T stream-api wget -qO- "http://localhost:8081/v1/decisions?limit=200" 2>/dev/null \
      || curl -fsS "$STREAM_API/v1/decisions?limit=200")
echo "$DEC" > /tmp/e2e_decisions.json

benign_action=$(echo "$DEC" | python3 -c "
import sys, json
d = json.load(sys.stdin)
want = {'$BENIGN_TX'}
for x in d['decisions']:
    if x['tx_id'] in want: print(x['action']); break
" 2>/dev/null)
check "benign tx ALLOWed" "$([[ "$benign_action" == "ALLOW" ]] && echo 0 || echo 1)" "action=${benign_action:-missing}"

fraud_summary=$(echo "$DEC" | python3 -c "
import sys, json
d = json.load(sys.stdin)
want = set('''$BURST_IDS'''.split())
hits = [(x['action'], round(x['score'], 3)) for x in d['decisions'] if x['tx_id'] in want]
bad = [h for h in hits if h[0] != 'ALLOW']
print(f'{len(bad)}/{len(hits)}', hits[:8])
" 2>/dev/null)
check "fraud burst flagged" "$([[ "$fraud_summary" == 0/* || -z "$fraud_summary" ]] && echo 1 || echo 0)" "$fraud_summary"

# pick the highest-scoring burst decision for graph/explanation checks
FLAGGED_TX=$(echo "$DEC" | python3 -c "
import sys, json
d = json.load(sys.stdin)
want = set('''$BURST_IDS'''.split())
hits = [x for x in d['decisions'] if x['tx_id'] in want and x['action'] != 'ALLOW']
if hits: print(max(hits, key=lambda x: x['score'])['tx_id'])
" 2>/dev/null)
echo "  flagged tx: ${FLAGGED_TX:-none}"

GRAPH=$(curl -fsS "$STREAM_API/v1/decisions/${FLAGGED_TX}/graph" 2>/dev/null || echo '{}')
nodes=$(echo "$GRAPH" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('nodes', [])))" 2>/dev/null || echo 0)
check "2-hop subgraph extracted" "$(( nodes > 1 ? 0 : 1 ))" "$nodes nodes"

EXPL=$(curl -fsS "$STREAM_API/v1/decisions/${FLAGGED_TX}/explanation" 2>/dev/null || echo '{}')
brief=$(echo "$EXPL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('brief','')[:80])" 2>/dev/null || true)
check "XAI explanation generated" "$([[ -n "$brief" ]] && echo 0 || echo 1)" "$brief"

STATS=$(curl -fsS "$STREAM_API/v1/stats" 2>/dev/null || echo '{}')
echo "  stats: $STATS"
total=$(echo "$STATS" | json_field "['ALLOW']+d['CHALLENGE']+d['BLOCK']"); total=${total:-0}
check "stats counters populated" "$(( total > 0 ? 0 : 1 ))" "$STATS"

score_ms=$(echo "$DEC" | python3 -c "
import sys, json
xs = sorted(x['latency']['total_ms'] for x in json.load(sys.stdin)['decisions'])
if xs: print(xs[len(xs)//2], xs[int(len(xs)*0.95)])
" 2>/dev/null)
echo "  decision latency p50/p95 ms: ${score_ms:-n/a}"

PAGE=$(curl -fsS "$PORTAL" 2>/dev/null || true)
check "portal serves dashboard" "$([[ "$PAGE" == *"UPI"* ]] && echo 0 || echo 1)"

# ---------------------------------------------------------------------------
step "7/7  10s load burst @20 TPS"
$COMPOSE --profile tools run --rm --no-deps loadgen --duration 10 --tps 20 \
  2>&1 | tail -3 | sed 's/^/  /'
sleep 3
STATS2=$(curl -fsS "$STREAM_API/v1/stats" 2>/dev/null || echo '{}')
echo "  stats after burst: $STATS2"
check "loadgen traffic processed" \
  "$(( $(echo "$STATS2" | json_field "['ALLOW']+d['CHALLENGE']+d['BLOCK']" || echo 0) > ${total:-0} ? 0 : 1 ))"

# ---------------------------------------------------------------------------
echo
echo "=============================================="
printf " e2e result: %d passed, %d failed\n" "$PASS" "$FAIL"
(( FAIL > 0 )) && printf " failed: %s\n" "${FAILED_NAMES[*]}"
echo "=============================================="

if (( TEARDOWN )); then
  step "Tearing down"
  $COMPOSE --profile tools down
fi

(( FAIL == 0 ))
