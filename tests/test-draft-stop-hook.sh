#!/usr/bin/env bash
set -euo pipefail

# Stop-hook safety net tests (validator-free; no SKIP gate).
# The Stop hook (check-draft-stop.sh) blocks the model's turn-end when a
# surgical-edit draft is left unfinished (marker still present, or marker
# removed via Bash so validation never ran). It re-prompts via JSON
# {"decision":"block","reason":"..."} and caps at 2 nudges.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/check-draft-stop.sh"

STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"
mkdir -p "$STATE_DIR"

WORK_DIR="$(mktemp -d)"

# Track state files we create so the trap can clean them up.
SIDS=()
cleanup() {
  rm -rf "$WORK_DIR"
  for sid in "${SIDS[@]:-}"; do
    [ -n "$sid" ] && rm -f "$STATE_DIR/$sid.json"
  done
}
trap cleanup EXIT

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected '$expected', got '$actual')"; FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -Fq "$needle"; then
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"; FAIL=$((FAIL + 1))
  fi
}

# Write a JSON state file for a session id.
write_state() {
  # $1 = sid, $2 = python-dict-as-json string
  printf '%s' "$2" > "$STATE_DIR/$1.json"
  SIDS+=("$1")
}

# Read draft_nudges from a session state file.
read_nudges() {
  python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('draft_nudges',0))" "$STATE_DIR/$1.json" 2>/dev/null
}

run_hook() {
  # $1 = sid; remaining args = env assignments via caller. Returns stdout.
  printf '{"session_id":"%s","stop_hook_active":false}' "$1"
}

echo "=== draft stop hook tests ==="

# --- Test 1: option disabled (env unset) + pending state + marker file -> no output ---
SID1="draftstop-disabled-$$"
MARKER_FILE1="$WORK_DIR/disabled.workflow.json"
printf '!!DRAFT!!\n{"nodes": [], "connections": {}}\n' > "$MARKER_FILE1"
write_state "$SID1" "{\"calls\": 1, \"draft_pending\": \"$MARKER_FILE1\"}"
OUT=$(run_hook "$SID1" | bash "$HOOK")
assert_eq "option disabled -> no output" "" "$OUT"

# --- Test 2: option enabled, no state file -> no output ---
SID2="draftstop-nostate-$$"
rm -f "$STATE_DIR/$SID2.json"
SIDS+=("$SID2")
OUT=$(run_hook "$SID2" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_eq "no state file -> no output" "" "$OUT"

# --- Test 3: state without draft_pending -> no output ---
SID3="draftstop-nopending-$$"
write_state "$SID3" '{"calls": 2}'
OUT=$(run_hook "$SID3" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_eq "state without draft_pending -> no output" "" "$OUT"

# --- Test 4: draft_pending + file WITH marker -> block (Edit-tool nudge), nudges==1 ---
SID4="draftstop-marker-$$"
MARKER_FILE4="$WORK_DIR/marker.workflow.json"
printf '!!DRAFT!!\n{"nodes": [], "connections": {}}\n' > "$MARKER_FILE4"
write_state "$SID4" "{\"calls\": 1, \"draft_pending\": \"$MARKER_FILE4\"}"
OUT=$(run_hook "$SID4" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_contains "marker present -> output has decision" '"decision"' "$OUT"
assert_contains "marker present -> output blocks" '"block"' "$OUT"
assert_contains "marker present -> reason names the marker" '!!DRAFT!!' "$OUT"
assert_contains "marker present -> reason mentions Edit tool" 'Edit tool' "$OUT"
assert_eq "marker present -> draft_nudges incremented to 1" "1" "$(read_nudges "$SID4")"

# --- Test 5: same again -> blocks again, nudges==2 ---
OUT=$(run_hook "$SID4" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_contains "marker present 2nd -> output blocks" '"block"' "$OUT"
assert_eq "marker present 2nd -> draft_nudges incremented to 2" "2" "$(read_nudges "$SID4")"

# --- Test 6: third time -> no output (cap) ---
OUT=$(run_hook "$SID4" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_eq "marker present 3rd -> capped, no output" "" "$OUT"
assert_eq "marker present 3rd -> draft_nudges stays 2" "2" "$(read_nudges "$SID4")"

# --- Test 7: fresh session, draft_pending + file WITHOUT marker -> block (Write nudge) ---
SID7="draftstop-nomarker-$$"
NOMARKER_FILE7="$WORK_DIR/nomarker.workflow.json"
printf '{"nodes": [], "connections": {}}\n' > "$NOMARKER_FILE7"
write_state "$SID7" "{\"calls\": 1, \"draft_pending\": \"$NOMARKER_FILE7\"}"
OUT=$(run_hook "$SID7" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_contains "marker gone -> output blocks" '"block"' "$OUT"
assert_contains "marker gone -> reason mentions never re-validated" 'never re-validated' "$OUT"
assert_contains "marker gone -> reason mentions Write" 'Write' "$OUT"
assert_eq "marker gone -> draft_nudges incremented to 1" "1" "$(read_nudges "$SID7")"

# --- Test 8: fresh session, draft_pending + file deleted -> no output ---
SID8="draftstop-deleted-$$"
DELETED_FILE8="$WORK_DIR/deleted.workflow.json"
write_state "$SID8" "{\"calls\": 1, \"draft_pending\": \"$DELETED_FILE8\"}"
# (file never created)
OUT=$(run_hook "$SID8" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_eq "draft_pending file missing -> no output" "" "$OUT"

echo ""
echo "draft-stop-hook: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
