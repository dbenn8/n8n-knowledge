#!/usr/bin/env bash
set -euo pipefail

# Pins the silent-failure-resistance contracts of the recall path:
#   recall_common.sh : hard curl timeouts, failure logging, ALWAYS return 0
#   format_results.py: returns None (exit 0) on empty / non-JSON payloads
# A regression in any of these re-introduces the "dead endpoint kills the whole
# hook (no recall, no build instructions)" silent failure.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
RECALL_COMMON="$REPO/hooks/lib/recall_common.sh"
FORMAT_PY="$REPO/hooks/lib/format_results.py"
STUB="$SCRIPT_DIR/fixtures/stub_recall_server.py"
# Hermetic runtime dir so C3 reads/writes an isolated debug log (not the user's real
# ~/.cache/n8n-knowledge/debug.log). recall_common.sh resolves NK_DEBUG_LOG from
# N8N_KNOWLEDGE_RUNTIME_DIR via runtime_dirs.sh.
RUNTIME_DIR="$(mktemp -d)"
DEBUG_LOG="$RUNTIME_DIR/debug.log"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# Launch the stub on an ephemeral OS-assigned port (PORT=0) and learn the real
# port from the "PORT=<n>" line it prints to stdout once bound. Polling that
# file for the line is ALSO the readiness probe (no fixed-port races, no blind
# `sleep 1`). Sets STUB_PID and the global STUB_PORT.
# Usage: start_stub <mode> <stdout_file> <body_log>
start_stub() {
  local mode="$1" out_file="$2" body_log="$3"
  : > "$out_file"
  python3 "$STUB" "$mode" 0 "$body_log" > "$out_file" 2>/dev/null &
  STUB_PID=$!
  disown "$STUB_PID" 2>/dev/null || true  # silence job-control "Terminated" notice on kill
  STUB_PORT=""
  local waited=0
  # Poll up to ~5s (50 * 0.1s) for the readiness line.
  while [ "$waited" -lt 50 ]; do
    STUB_PORT="$(sed -n 's/^PORT=\([0-9][0-9]*\)$/\1/p' "$out_file" 2>/dev/null | head -n1)"
    [ -n "$STUB_PORT" ] && break
    sleep 0.1
    waited=$((waited + 1))
  done
}

STUB_PID=""
TMP_FILES=()
cleanup() {
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null || true
  for f in "${TMP_FILES[@]}"; do rm -f "$f" 2>/dev/null || true; done
  rm -rf "$RUNTIME_DIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== recall resilience tests ==="

# --- C1: a dead endpoint must NOT kill a `set -euo pipefail` caller ---
# Port 9 is the reserved "discard" port — connect-timeout 2 then graceful return 0.
C1_OUT="$(RECALL_URL="http://127.0.0.1:9" bash -c '
  set -euo pipefail
  source "$1"
  recall_post "{\"query\": \"x\", \"include\": {\"source_facts\": {}}}" >/dev/null
  echo SURVIVED
' _ "$RECALL_COMMON" 2>/dev/null)" && C1_RC=0 || C1_RC=$?
if [ "${C1_RC:-1}" -eq 0 ] && [ "$C1_OUT" = "SURVIVED" ]; then
  pass "C1: recall_post against dead endpoint survives set -e and exits 0"
else
  fail "C1: dead endpoint killed the caller (rc=${C1_RC:-?}, out='$C1_OUT')"
fi

# --- C2: slow endpoint must be bounded by RECALL_CURL_MAX_TIME (no hang) ---
# The stub binds an ephemeral port (0); we learn the real port from its
# "PORT=" readiness line. max-time is 2s, so 2s of slack absorbs scheduler
# jitter — anything above 4s means max-time was not honored.
C2_OUT="$(mktemp)"
C2_LOG="$(mktemp)"
TMP_FILES+=("$C2_OUT" "$C2_LOG")
start_stub slow "$C2_OUT" "$C2_LOG"
START=$(date +%s)
RECALL_URL="http://127.0.0.1:$STUB_PORT/recall" RECALL_CURL_MAX_TIME=2 bash -c '
  source "$1"
  recall_post "{\"query\": \"x\", \"include\": {\"source_facts\": {}}}" >/dev/null
' _ "$RECALL_COMMON" 2>/dev/null || true
END=$(date +%s)
ELAPSED=$((END - START))
kill "$STUB_PID" 2>/dev/null || true
STUB_PID=""
if [ "$ELAPSED" -le 4 ]; then
  pass "C2: slow endpoint bounded to ${ELAPSED}s (<=4s) by RECALL_CURL_MAX_TIME"
else
  fail "C2: slow endpoint took ${ELAPSED}s (expected <=4s) — max-time not honored"
fi

# --- C3: a failed recall_post appends a "recall_post FAIL" line to the debug log ---
BEFORE=$(grep -c "recall_post FAIL" "$DEBUG_LOG" 2>/dev/null || echo 0)
N8N_KNOWLEDGE_RUNTIME_DIR="$RUNTIME_DIR" RECALL_URL="http://127.0.0.1:9" bash -c '
  source "$1"
  recall_post "{\"query\": \"x\"}" >/dev/null
' _ "$RECALL_COMMON" 2>/dev/null || true
AFTER=$(grep -c "recall_post FAIL" "$DEBUG_LOG" 2>/dev/null || echo 0)
if [ "$AFTER" -gt "$BEFORE" ]; then
  pass "C3: failed recall_post logged a 'recall_post FAIL' line ($BEFORE -> $AFTER)"
else
  fail "C3: no 'recall_post FAIL' line appended ($BEFORE -> $AFTER)"
fi

# --- C4: format_results.py degrades to exit 0 on an EMPTY payload ---
EMPTY_FILE="$(mktemp)"
GARBAGE_FILE="$(mktemp)"
TMP_FILES+=("$EMPTY_FILE" "$GARBAGE_FILE")
printf 'this is not json <<<>>> {broken' > "$GARBAGE_FILE"
# (EMPTY_FILE intentionally left empty)

python3 "$FORMAT_PY" "$EMPTY_FILE" "" >/dev/null 2>&1 && E_RC=0 || E_RC=$?
if [ "${E_RC:-1}" -eq 0 ]; then
  pass "C4: format_results.py exits 0 on an empty payload"
else
  fail "C4: format_results.py exited nonzero on empty payload (rc=${E_RC:-?})"
fi

# --- C5: format_results.py degrades to exit 0 on a non-JSON (garbage) payload ---
python3 "$FORMAT_PY" "$GARBAGE_FILE" "" >/dev/null 2>&1 && G_RC=0 || G_RC=$?
if [ "${G_RC:-1}" -eq 0 ]; then
  pass "C5: format_results.py exits 0 on a non-JSON payload"
else
  fail "C5: format_results.py exited nonzero on garbage payload (rc=${G_RC:-?})"
fi

# --- C6: gotcha recall retries on transient failure ---
echo ""
echo "--- C6: gotcha recall retries on empty/failed response ---"

STUB_PORT_C5=""
STUB_PID_C5=""
BODY_LOG_C5="$RUNTIME_DIR/c5-body.log"

launch_stub_c5() {
  local out_file
  out_file="$(mktemp)"
  python3 "$STUB" gotcha-fail-once 0 "$BODY_LOG_C5" > "$out_file" 2>/dev/null &
  STUB_PID_C5=$!
  local tries=0
  while [ "$tries" -lt 40 ]; do
    if grep -q '^PORT=' "$out_file" 2>/dev/null; then
      STUB_PORT_C5=$(grep '^PORT=' "$out_file" | head -1 | cut -d= -f2)
      rm -f "$out_file"
      return 0
    fi
    sleep 0.1
    tries=$((tries + 1))
  done
  rm -f "$out_file"
  return 1
}

launch_stub_c5

c5_result=$(
  N8N_KNOWLEDGE_RUNTIME_DIR="$RUNTIME_DIR" \
  RECALL_URL="http://127.0.0.1:$STUB_PORT_C5" \
  bash -c '
    source "'"$REPO"'/hooks/lib/structured_recall.sh"
    do_gotcha_recall "nodes-base.openAi"
  '
)

kill "$STUB_PID_C5" 2>/dev/null || true
wait "$STUB_PID_C5" 2>/dev/null || true

if echo "$c5_result" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
  pass "gotcha recall returns results after retry on transient failure"
else
  fail "gotcha recall returns results after retry on transient failure"
fi

c5_count=$(wc -l < "$BODY_LOG_C5" 2>/dev/null | tr -d ' ')
if [ "$c5_count" = "2" ]; then
  pass "gotcha recall made exactly 2 requests (initial + retry)"
else
  fail "gotcha recall made exactly 2 requests (expected 2, got ${c5_count:-0})"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
