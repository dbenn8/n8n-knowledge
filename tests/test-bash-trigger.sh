#!/usr/bin/env bash
set -euo pipefail

# Bash-event hash-based validation trigger tests (Task 4c).
# The surgical-edit recipe tells models to delete the !!DRAFT!! marker with the
# Edit tool because only Edit/Write trigger validation. Models sometimes remove
# the marker with Bash instead — validation never fires, the repair loop desyncs.
# The fix: trigger validation on STATE CHANGE, not tool choice. On a Bash event,
# look up the pending draft file recorded in session state; if it exists, is
# markerless, and its content hash changed since the last validation -> run the
# normal validation flow on it (normal budget rules).
#
# Part 1 (validator-free): pre-budget gating — unchanged / marker-bearing /
# missing-state Bash events cost nothing and produce no output.
# Part 2 (needs local n8n-mcp validator, SKIPs otherwise): the actual fix fires
# validation, records the hash, and the desync rescue is idempotent.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/validate-workflow.sh"
STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"

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

echo "=== bash-trigger tests ==="

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"; rm -f "$STATE_DIR"/bash-trigger-*.json' EXIT

mkdir -p "$STATE_DIR"

HOOK_INPUT_FILE="$WORK_DIR/hook-input.json"

build_hook_input() {
  # $1 = session id, $2 = tool name, $3 = file path ('' for Bash)
  # Writes the hook stdin JSON to $HOOK_INPUT_FILE (avoids piping into a
  # fast-exiting hook, which would SIGPIPE the producer under pipefail).
  python3 - "$1" "$2" "$3" "$WORK_DIR" "$HOOK_INPUT_FILE" << 'PYEOF'
import json, sys
d = {"session_id": sys.argv[1], "tool_name": sys.argv[2], "cwd": sys.argv[4], "tool_input": {}}
if sys.argv[3]:
    d["tool_input"]["file_path"] = sys.argv[3]
else:
    d["tool_input"]["command"] = "python3 fix.py"
with open(sys.argv[5], "w") as f:
    json.dump(d, f)
PYEOF
}

sha256_of() {
  python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"
}

read_state_field() {
  # $1 = state file, $2 = key
  python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2" 2>/dev/null
}

INVALID_JSON='{
  "nodes": [
    {
      "id": "1",
      "name": "Slack",
      "type": "n8n-nodes-base.slack",
      "typeVersion": 2.2,
      "position": [0, 0],
      "parameters": {"resource": "message", "operation": "bogusOperation"}
    }
  ],
  "connections": {}
}'

VALID_JSON='{"nodes": [{"id": "1", "name": "When clicking Test workflow", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 0], "parameters": {}}, {"id": "2", "name": "Edit Fields", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [200, 0], "parameters": {}}], "connections": {"When clicking Test workflow": {"main": [[{"node": "Edit Fields", "type": "main", "index": 0}]]}}}'

# --- Part 1: pre-budget gating (validator-free) ---

# 1. Bash event, no state file -> no output.
SID1="bash-trigger-nostate-$$"
rm -f "$STATE_DIR/$SID1.json"
build_hook_input "$SID1" "Bash" ""
OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" < "$HOOK_INPUT_FILE")
assert_eq "Bash event with no state file produces no output" "" "$OUT"

# 2. Bash event, state without draft_pending -> no output.
SID2="bash-trigger-nopending-$$"
python3 -c "import json,sys;json.dump({'calls':0}, open(sys.argv[1],'w'))" "$STATE_DIR/$SID2.json"
build_hook_input "$SID2" "Bash" ""
OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" < "$HOOK_INPUT_FILE")
assert_eq "Bash event with no draft_pending produces no output" "" "$OUT"

# 3. Bash event, draft_pending file WITH !!DRAFT!! marker -> no output, calls unchanged.
SID3="bash-trigger-marker-$$"
MARKER_FILE="$WORK_DIR/marker.workflow.json"
printf '!!DRAFT!!\n%s\n' "$INVALID_JSON" > "$MARKER_FILE"
python3 -c "import json,sys;json.dump({'calls':2,'draft_pending':sys.argv[1]}, open(sys.argv[2],'w'))" "$MARKER_FILE" "$STATE_DIR/$SID3.json"
build_hook_input "$SID3" "Bash" ""
OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" < "$HOOK_INPUT_FILE")
assert_eq "Bash event on marker-bearing draft produces no output" "" "$OUT"
assert_eq "Bash event on marker-bearing draft does not charge budget" "2" "$(read_state_field "$STATE_DIR/$SID3.json" calls)"

# 4. Bash event, markerless file, last_validated_hash == current hash -> no output, calls unchanged.
SID4="bash-trigger-unchanged-$$"
UNCHANGED_FILE="$WORK_DIR/unchanged.workflow.json"
printf '%s\n' "$INVALID_JSON" > "$UNCHANGED_FILE"
CUR_HASH=$(sha256_of "$UNCHANGED_FILE")
python3 -c "import json,sys;json.dump({'calls':1,'draft_pending':sys.argv[1],'last_validated_hash':sys.argv[2]}, open(sys.argv[3],'w'))" "$UNCHANGED_FILE" "$CUR_HASH" "$STATE_DIR/$SID4.json"
build_hook_input "$SID4" "Bash" ""
OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" < "$HOOK_INPUT_FILE")
assert_eq "Bash event on unchanged file produces no output" "" "$OUT"
assert_eq "Bash event on unchanged file does not charge budget" "1" "$(read_state_field "$STATE_DIR/$SID4.json" calls)"

# 5. Bash event, option disabled (env unset) -> no output.
SID5="bash-trigger-disabled-$$"
DISABLED_FILE="$WORK_DIR/disabled.workflow.json"
printf '%s\n' "$INVALID_JSON" > "$DISABLED_FILE"
python3 -c "import json,sys;json.dump({'calls':0,'draft_pending':sys.argv[1],'last_validated_hash':'stale0000'}, open(sys.argv[2],'w'))" "$DISABLED_FILE" "$STATE_DIR/$SID5.json"
build_hook_input "$SID5" "Bash" ""
OUT=$(bash "$HOOK" < "$HOOK_INPUT_FILE")
assert_eq "Bash event with validation disabled produces no output" "" "$OUT"

# 6. Non-Bash non-Edit non-Write tool (Read) -> no output.
SID6="bash-trigger-readtool-$$"
READ_FILE="$WORK_DIR/read.workflow.json"
printf '%s\n' "$INVALID_JSON" > "$READ_FILE"
python3 -c "import json,sys;json.dump({'calls':0,'draft_pending':sys.argv[1],'last_validated_hash':'stale0000'}, open(sys.argv[2],'w'))" "$READ_FILE" "$STATE_DIR/$SID6.json"
build_hook_input "$SID6" "Read" "$READ_FILE"
OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" < "$HOOK_INPUT_FILE")
assert_eq "Read tool event produces no output" "" "$OUT"

# --- Part 2: actual fix (needs local validator) ---

if ! ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db >/dev/null 2>&1; then
  echo "  SKIP: no local n8n-mcp install — validator-gated tests not exercised"
else
  # 7. THE FIX: markerless invalid draft, stale hash -> Bash event fires validation.
  SID7="bash-trigger-fix-$$"
  FIX_FILE="$WORK_DIR/fix.workflow.json"
  printf '%s\n' "$INVALID_JSON" > "$FIX_FILE"
  python3 -c "import json,sys;json.dump({'calls':0,'draft_pending':sys.argv[1],'last_validated_hash':'stale0000'}, open(sys.argv[2],'w'))" "$FIX_FILE" "$STATE_DIR/$SID7.json"
  build_hook_input "$SID7" "Bash" ""
  FIX_OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK" < "$HOOK_INPUT_FILE")
  assert_contains "Bash event on changed invalid draft fires validation" "n8n Workflow Validator" "$FIX_OUT"
  assert_eq "Bash-triggered validation charges budget" "1" "$(read_state_field "$STATE_DIR/$SID7.json" calls)"
  NEW_HASH7=$(read_state_field "$STATE_DIR/$SID7.json" last_validated_hash)
  if [ -n "$NEW_HASH7" ] && [ "$NEW_HASH7" != "stale0000" ]; then
    echo "  PASS: Bash-triggered validation records a fresh hash"; PASS=$((PASS + 1))
  else
    echo "  FAIL: Bash-triggered validation records a fresh hash (got '$NEW_HASH7')"; FAIL=$((FAIL + 1))
  fi

  # 8. Desync rescue: invoke again WITHOUT changing the file -> no output, calls still 1.
  build_hook_input "$SID7" "Bash" ""
  OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK" < "$HOOK_INPUT_FILE")
  assert_eq "second Bash event on unchanged file produces no output" "" "$OUT"
  assert_eq "second Bash event does not re-charge budget" "1" "$(read_state_field "$STATE_DIR/$SID7.json" calls)"

  # 9. Edit-path hash recording: fresh session, Write-event on invalid workflow.
  SID9="bash-trigger-editpath-$$"
  EDIT_FILE="$WORK_DIR/editpath.workflow.json"
  printf '%s\n' "$INVALID_JSON" > "$EDIT_FILE"
  rm -f "$STATE_DIR/$SID9.json"
  build_hook_input "$SID9" "Write" "$EDIT_FILE"
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" < "$HOOK_INPUT_FILE" > /dev/null || true
  HASH9=$(read_state_field "$STATE_DIR/$SID9.json" last_validated_hash)
  if [ -n "$HASH9" ]; then
    echo "  PASS: Write-event validation records last_validated_hash"; PASS=$((PASS + 1))
  else
    echo "  FAIL: Write-event validation records last_validated_hash (got empty)"; FAIL=$((FAIL + 1))
  fi

  # 10. VALID clears: seed draft_pending for a VALID workflow, stale hash -> Bash event
  #     fires VALID validation, clears draft_pending, records hash.
  SID10="bash-trigger-valid-$$"
  VALID_FILE="$WORK_DIR/valid.workflow.json"
  printf '%s\n' "$VALID_JSON" > "$VALID_FILE"
  python3 -c "import json,sys;json.dump({'calls':0,'draft_pending':sys.argv[1],'last_validated_hash':'stale0000'}, open(sys.argv[2],'w'))" "$VALID_FILE" "$STATE_DIR/$SID10.json"
  build_hook_input "$SID10" "Bash" ""
  VALID_OUT=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK" < "$HOOK_INPUT_FILE")
  assert_contains "Bash event on valid draft fires validation" "n8n Workflow Validator" "$VALID_OUT"
  assert_eq "VALID Bash-triggered validation clears draft_pending" "" "$(read_state_field "$STATE_DIR/$SID10.json" draft_pending)"
  HASH10=$(read_state_field "$STATE_DIR/$SID10.json" last_validated_hash)
  if [ -n "$HASH10" ] && [ "$HASH10" != "stale0000" ]; then
    echo "  PASS: VALID Bash-triggered validation records a fresh hash"; PASS=$((PASS + 1))
  else
    echo "  FAIL: VALID Bash-triggered validation records a fresh hash (got '$HASH10')"; FAIL=$((FAIL + 1))
  fi
fi

echo ""
echo "bash-trigger: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
