#!/usr/bin/env bash
set -euo pipefail

# Surgical-edit mode tests.
# Part 1 (no validator needed): a workflow file whose first line is the literal
# !!DRAFT!! marker is a work-in-progress draft — the hook must exit silently
# with NO output and NO budget charge, in BOTH edit styles.
# Part 2 (needs local n8n-mcp validator, SKIPs otherwise): the INVALID feedback
# wording branches on CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/validate-workflow.sh"

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

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -Fq "$needle"; then
    echo "  FAIL: $desc (must NOT contain '$needle')"; FAIL=$((FAIL + 1))
  else
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  fi
}

echo "=== surgical edit mode tests ==="

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# Hermetic per-user runtime dir (hook resolves $NK_STATE_DIR/workflow-validation from this)
export N8N_KNOWLEDGE_RUNTIME_DIR="$WORK_DIR/runtime"

hook_input() {
  # $1 = session id, $2 = file path
  python3 - "$1" "$2" "$WORK_DIR" << 'PYEOF'
import json, sys
print(json.dumps({
    "session_id": sys.argv[1],
    "tool_name": "Write",
    "cwd": sys.argv[3],
    "tool_input": {"file_path": sys.argv[2]},
}))
PYEOF
}

# --- Part 1: marker skip (validator-free) ---

DRAFT_FILE="$WORK_DIR/draft.workflow.json"
printf '!!DRAFT!!\n{"nodes": [], "connections": {}}\n' > "$DRAFT_FILE"

SID="surgical-marker-test-$$"
STATE_FILE="$N8N_KNOWLEDGE_RUNTIME_DIR/state/workflow-validation/$SID.json"
rm -f "$STATE_FILE"

OUT=$(hook_input "$SID" "$DRAFT_FILE" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_eq "draft-marker file produces no hook output" "" "$OUT"
assert_eq "draft-marker file does not charge the budget" "absent" "$([ -f "$STATE_FILE" ] && echo present || echo absent)"

OUT=$(hook_input "$SID" "$DRAFT_FILE" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
  CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK")
assert_eq "draft-marker skip also applies in surgical mode" "" "$OUT"

# A normal (markerless) file must still flow past the marker check. We prove it
# by confirming the budget state file IS created for a markerless invalid-JSON
# file (the hook charges budget before the JSON sanity copy).
PLAIN_FILE="$WORK_DIR/plain.workflow.json"
printf '{"nodes": [], "connections": {}}\n' > "$PLAIN_FILE"
SID2="surgical-plain-test-$$"
STATE_FILE2="$N8N_KNOWLEDGE_RUNTIME_DIR/state/workflow-validation/$SID2.json"
rm -f "$STATE_FILE2"
hook_input "$SID2" "$PLAIN_FILE" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" > /dev/null || true
assert_eq "markerless file still reaches budget accounting" "present" "$([ -f "$STATE_FILE2" ] && echo present || echo absent)"
rm -f "$STATE_FILE" "$STATE_FILE2"

# --- Part 2: feedback wording branch (needs local validator) ---

if ! ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db >/dev/null 2>&1; then
  echo "  SKIP: no local n8n-mcp install — wording-branch tests not exercised"
else
  INVALID_FILE="$WORK_DIR/invalid.workflow.json"
  cat > "$INVALID_FILE" << 'JSONEOF'
{
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
}
JSONEOF

  SURGICAL_OUT=$(hook_input "surgical-wording-$$" "$INVALID_FILE" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK")
  assert_contains "surgical mode names the marker" "!!DRAFT!!" "$SURGICAL_OUT"
  assert_contains "surgical mode instructs Bash python3 edits" "python3" "$SURGICAL_OUT"
  assert_contains "surgical mode says do not rewrite" "do NOT rewrite" "$SURGICAL_OUT"
  assert_contains "surgical mode says marker removal must use Edit" "Edit tool" "$SURGICAL_OUT"
  assert_contains "surgical mode explains WHY Bash removal fails" "never with Bash" "$SURGICAL_OUT"
  assert_contains "surgical mode states the failure consequence" "fail the user's task" "$SURGICAL_OUT"
  assert_not_contains "surgical mode drops the re-write guidance" "one complete re-write" "$SURGICAL_OUT"

  REWRITE_OUT=$(hook_input "rewrite-wording-$$" "$INVALID_FILE" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
  assert_contains "default mode keeps re-write guidance" "one complete re-write" "$REWRITE_OUT"
  assert_not_contains "default mode has no marker instructions" "!!DRAFT!!" "$REWRITE_OUT"

  # --- Part 3: draft_pending recording / clearing (Task 4b) ---
  STATE_DIR_4B="$N8N_KNOWLEDGE_RUNTIME_DIR/state/workflow-validation"
  read_pending() {
    python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('draft_pending',''))" "$1" 2>/dev/null
  }

  # Surgical INVALID feedback (ran above for session surgical-wording-$$) must
  # have recorded draft_pending == the INVALID_FILE path.
  SURGICAL_STATE="$STATE_DIR_4B/surgical-wording-$$.json"
  assert_eq "surgical INVALID records draft_pending" "$INVALID_FILE" "$(read_pending "$SURGICAL_STATE")"

  # Rewrite-mode INVALID (ran above for session rewrite-wording-$$) must NOT set it.
  REWRITE_STATE="$STATE_DIR_4B/rewrite-wording-$$.json"
  assert_eq "rewrite INVALID does not record draft_pending" "" "$(read_pending "$REWRITE_STATE")"

  # Clearing: a VALID validation must remove an existing draft_pending key.
  CLEAR_FILE="$WORK_DIR/clear.workflow.json"
  cat > "$CLEAR_FILE" << 'JSONEOF'
{"nodes": [{"id": "1", "name": "When clicking Test workflow", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 0], "parameters": {}}, {"id": "2", "name": "Edit Fields", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [200, 0], "parameters": {}}], "connections": {"When clicking Test workflow": {"main": [[{"node": "Edit Fields", "type": "main", "index": 0}]]}}}
JSONEOF
  CLEAR_STATE="$STATE_DIR_4B/surgical-clear-$$.json"
  python3 -c "import json,sys;json.dump({'calls':0,'draft_pending':sys.argv[1]}, open(sys.argv[2],'w'))" "$CLEAR_FILE" "$CLEAR_STATE"
  hook_input "surgical-clear-$$" "$CLEAR_FILE" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK" > /dev/null || true
  assert_eq "VALID validation clears draft_pending" "" "$(read_pending "$CLEAR_STATE")"

  rm -f "$SURGICAL_STATE" "$REWRITE_STATE" "$CLEAR_STATE"
fi

echo ""
echo "surgical-edit-mode: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
