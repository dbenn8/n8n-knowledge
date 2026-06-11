#!/usr/bin/env bash
set -euo pipefail

# Pins the validator-budget counter line in validate-workflow.sh feedback so a
# refactor can't silently drop it. The hook must tell the model how much of its
# validation budget is spent:
#   VALID path:   "Validator budget: N of CAP calls used this session."
#   INVALID path: "Validator budget: N of CAP calls used (M remaining)."
# Runs the real hook end-to-end against the local n8n-mcp validator; skips
# (exit 0 with SKIP notice) when no local validator install is present.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/validate-workflow.sh"

PASS=0
FAIL=0

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -Fq "$needle"; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== validator budget line tests ==="

# Local validator availability gate (same discovery as the plugin)
if ! ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db >/dev/null 2>&1; then
  echo "  SKIP: no local n8n-mcp install found — cannot exercise the validator hook"
  exit 0
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

run_hook() {
  # $1 = workflow file path, $2 = session id
  # Pin the runtime dir under WORK_DIR so the validator session state stays hermetic
  # (lives at $WORK_DIR/state/workflow-validation/<sid>.json, cleaned with WORK_DIR).
  printf '{"session_id":"%s","tool_name":"Write","cwd":"%s","tool_input":{"file_path":"%s"}}' \
    "$2" "$WORK_DIR" "$1" |
    N8N_KNOWLEDGE_RUNTIME_DIR="$WORK_DIR" \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=10 \
    CLAUDE_PLUGIN_OPTION_VALIDATORMODE=local \
    bash "$HOOK" 2>/dev/null || true
}

SID="test-budget-$$"

# --- VALID workflow: two connected nodes, schema-correct ---
VALID_WF="$WORK_DIR/valid.workflow.json"
cat > "$VALID_WF" << 'EOF'
{
  "name": "budget test valid",
  "nodes": [
    {"id": "a1", "name": "Manual Trigger", "type": "n8n-nodes-base.manualTrigger",
     "typeVersion": 1, "position": [0, 0], "parameters": {}},
    {"id": "b2", "name": "NoOp", "type": "n8n-nodes-base.noOp",
     "typeVersion": 1, "position": [200, 0], "parameters": {}}
  ],
  "connections": {"Manual Trigger": {"main": [[{"node": "NoOp", "type": "main", "index": 0}]]}}
}
EOF

OUT_VALID="$(run_hook "$VALID_WF" "$SID")"
assert_contains "VALID feedback carries the budget line" \
  "Validator budget: 1 of 10 calls used" "$OUT_VALID"

# --- INVALID workflow: bogus operation forces the INVALID path (call #2) ---
INVALID_WF="$WORK_DIR/invalid.workflow.json"
cat > "$INVALID_WF" << 'EOF'
{
  "name": "budget test invalid",
  "nodes": [
    {"id": "a1", "name": "Manual Trigger", "type": "n8n-nodes-base.manualTrigger",
     "typeVersion": 1, "position": [0, 0], "parameters": {}},
    {"id": "b2", "name": "Sheets", "type": "n8n-nodes-base.googleSheets",
     "typeVersion": 4, "position": [200, 0],
     "parameters": {"resource": "sheet", "operation": "definitelyNotAnOperation"}}
  ],
  "connections": {"Manual Trigger": {"main": [[{"node": "Sheets", "type": "main", "index": 0}]]}}
}
EOF

OUT_INVALID="$(run_hook "$INVALID_WF" "$SID")"
assert_contains "INVALID feedback carries the budget line with remaining count" \
  "Validator budget: 2 of 10 calls used (8 remaining)" "$OUT_INVALID"
assert_contains "INVALID feedback tells the model to batch fixes" \
  "Batch ALL fixes below into one complete re-write" "$OUT_INVALID"

# Cleanup the session state file this test created (lives under the pinned runtime dir).
rm -f "$WORK_DIR/state/workflow-validation/$SID.json"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
