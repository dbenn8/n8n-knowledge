#!/usr/bin/env bash
set -euo pipefail

# E2E pins for auto-recall.sh resilience, driven against the stub recall server.
# Two contracts the multi-stream merge must keep:
#   E1: a DEAD semantic recall must NOT discard gotcha results — the per-stream
#       loads are independent (old all-or-nothing try block lost everything when
#       the semantic call failed under endpoint load).
#   E2: gotcha recall fans out over EVERY detected node (capped), not just the
#       first — querying only the first detection missed the Merge row-loss
#       gotcha when ZeroBounce was detected first (eval prompt 122).
#
# The repo worktree root qualifies as an n8n codebase (README mentions n8n), so
# should_recall fires and node detection sees both zerobounce and merge.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO/hooks/auto-recall.sh"
STUB="$SCRIPT_DIR/fixtures/stub_recall_server.py"

PROMPT='read a google sheet and merge results with zerobounce'

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

STUB_PID=""
TMP_FILES=()
cleanup() {
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null || true
  for f in "${TMP_FILES[@]}"; do rm -rf "$f" 2>/dev/null || true; done
}
trap cleanup EXIT

# Launch the stub on an ephemeral OS-assigned port (PORT=0) and learn the real
# port from the "PORT=<n>" line it prints to its stdout once bound. Polling that
# stdout file for the line is ALSO the readiness probe — no fixed ports, no
# blind `sleep 1`. The stub's request-body log (argv[3]) is a SEPARATE file from
# its stdout, so PORT discovery never collides with body capture. Sets STUB_PID
# and the global STUB_PORT.
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

echo "=== auto-recall resilience (E2E) tests ==="

# Mental models were removed (replaced by node-tagged + engagement-ranked gotcha
# recall). Every detected node now goes through gotcha recall unconditionally —
# no cache/API isolation needed anymore.

# --- E1: gotcha results survive a dead semantic recall (sem-fail mode) ---
E1_STDOUT="$(mktemp)"
E1_BODY="$(mktemp)"
E1_OUT="$(mktemp)"
TMP_FILES+=("$E1_STDOUT" "$E1_BODY" "$E1_OUT")
start_stub sem-fail "$E1_STDOUT" "$E1_BODY"
printf '{"prompt": "%s", "cwd": "%s"}' "$PROMPT" "$REPO" |
  RECALL_URL="http://127.0.0.1:$STUB_PORT/recall" bash "$HOOK" > "$E1_OUT" 2>/dev/null || true
kill "$STUB_PID" 2>/dev/null || true
STUB_PID=""
if grep -Fq "gotcha result" "$E1_OUT"; then
  pass "E1: gotcha results reach stdout despite a 500'd semantic recall"
else
  fail "E1: no 'gotcha result' in hook output — dead semantic recall dropped everything"
fi

# --- E2: gotcha recall fans out over BOTH detected nodes (zerobounce + merge) ---
E2_STDOUT="$(mktemp)"
E2_BODY="$(mktemp)"
TMP_FILES+=("$E2_STDOUT" "$E2_BODY")
start_stub ok "$E2_STDOUT" "$E2_BODY"
printf '{"prompt": "%s", "cwd": "%s"}' "$PROMPT" "$REPO" |
  RECALL_URL="http://127.0.0.1:$STUB_PORT/recall" bash "$HOOK" > /dev/null 2>&1 || true
kill "$STUB_PID" 2>/dev/null || true
STUB_PID=""
GOTCHA_REQS=$(grep -c '"max_tokens": 2000' "$E2_BODY" 2>/dev/null || echo 0)
# Fixture-specific: this prompt + repo detects EXACTLY 2 nodes (zerobounce,
# merge), so the gotcha fan-out must fire exactly 2 requests — no more, no
# fewer. (A looser >=2 would mask an over-firing regression.)
if [ "$GOTCHA_REQS" -eq 2 ]; then
  pass "E2: gotcha fan-out fired $GOTCHA_REQS requests (exactly 2 — zerobounce + merge)"
else
  fail "E2: $GOTCHA_REQS gotcha request(s) (expected exactly 2 for zerobounce + merge)"
fi

# --- E3: gotcha-channel observations carry source-fact provenance end-to-end ---
# The fix: gotcha/structured recalls now send include.source_facts, and BOTH
# auto-recall merges preserve the merged source_facts dict. So a gotcha
# observation's source_fact_ids resolve to real source URLs at render time.
# Without the plumbing fix the observation rendered "sources: unavailable".
E3_STDOUT="$(mktemp)"
E3_BODY="$(mktemp)"
E3_OUT="$(mktemp)"
TMP_FILES+=("$E3_STDOUT" "$E3_BODY" "$E3_OUT")
start_stub ok "$E3_STDOUT" "$E3_BODY"
printf '{"prompt": "%s", "cwd": "%s"}' "$PROMPT" "$REPO" |
  RECALL_URL="http://127.0.0.1:$STUB_PORT/recall" bash "$HOOK" > "$E3_OUT" 2>/dev/null || true
kill "$STUB_PID" 2>/dev/null || true
STUB_PID=""
if grep -Fq 'sources="2"' "$E3_OUT" || grep -Fq 'example.com/sf/' "$E3_OUT"; then
  pass "E3: gotcha observation carries provenance end-to-end (sources=\"2\" / resolved sf link)"
else
  fail "E3: gotcha observation lost provenance — no sources=\"2\" or sf link in hook output"
fi

# --- E4: gotcha recall is node-tagged (all_strict) + task-aware (Layer 2) ---
# Task 5: do_gotcha_recall filters by ["node:X"] with tags_match=all_strict
# ("all"/"any" include UNtagged memories by design and would re-admit the
# cross-node Facebook/Salesforce noise), and folds the user's task keywords into
# the query so dense nodes surface bugs relevant to THIS task. PROMPT mentions
# "google sheet" — task keywords that are NOT node/service names, proving the
# query is task-aware. Mental-model isolation (set above) routes every node
# through gotcha recall so the request bodies are observable.
E4_STDOUT="$(mktemp)"
E4_BODY="$(mktemp)"
TMP_FILES+=("$E4_STDOUT" "$E4_BODY")
start_stub ok "$E4_STDOUT" "$E4_BODY"
printf '{"prompt": "%s", "cwd": "%s"}' "$PROMPT" "$REPO" |
  RECALL_URL="http://127.0.0.1:$STUB_PORT/recall" bash "$HOOK" > "$E4_STDOUT" 2>&1 || true
kill "$STUB_PID" 2>/dev/null || true
STUB_PID=""
# Gotcha requests carry max_tokens 2000; count how many are strict + node-tagged.
GOTCHA_N=$(grep -c '"max_tokens": 2000' "$E4_BODY" 2>/dev/null || echo 0)
GOTCHA_STRICT=$(grep '"max_tokens": 2000' "$E4_BODY" 2>/dev/null | grep -c 'all_strict' || echo 0)
GOTCHA_NODE=$(grep '"max_tokens": 2000' "$E4_BODY" 2>/dev/null | grep -c '"node:' || echo 0)
if [ "$GOTCHA_N" -ge 2 ] && [ "$GOTCHA_STRICT" -eq "$GOTCHA_N" ] && [ "$GOTCHA_NODE" -eq "$GOTCHA_N" ]; then
  pass "E4: every gotcha request is node-tagged + all_strict ($GOTCHA_N gotcha / $GOTCHA_STRICT strict / $GOTCHA_NODE node)"
else
  fail "E4: gotcha not node-tagged/all_strict (gotcha=$GOTCHA_N strict=$GOTCHA_STRICT node=$GOTCHA_NODE)"
fi
if grep '"max_tokens": 2000' "$E4_BODY" 2>/dev/null | grep -q 'sheet\|google'; then
  pass "E4b: gotcha query is task-aware (prompt keyword present in a gotcha request)"
else
  fail "E4b: gotcha query not task-aware (no prompt keyword in gotcha request bodies)"
fi

# --- E5: Tier 2 semantic recall is SKIPPED when Tier 1 has >= 2 results ---
# In ok mode Tier 1 (gotcha + structured) returns results, so the conditional
# semantic pass must NOT fire. A semantic request carries neither the gotcha
# marker ("max_tokens": 2000) nor the structured marker ("tags_match"), but does
# carry a "query". Reuses the body log captured by the E4 run above.
SEM_REQS=$(grep '"query"' "$E4_BODY" 2>/dev/null | grep -cvE '"max_tokens": 2000|"tags_match"' || true)
if [ "${SEM_REQS:-0}" -eq 0 ]; then
  pass "E5: Tier 2 semantic recall skipped when Tier 1 sufficient (0 semantic requests)"
else
  fail "E5: Tier 2 semantic recall fired despite sufficient Tier 1 ($SEM_REQS semantic requests)"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
