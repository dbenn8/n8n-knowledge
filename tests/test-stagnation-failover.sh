#!/usr/bin/env bash
set -euo pipefail

# Stagnation-failover tests (Task 4e).
# In surgical+adaptive mode, when the model is demonstrably stuck the INVALID
# feedback flips strategy from surgical edits to one full rewrite.
#   (A) consec_stagnant accounting: incremented on a non-improving INVALID round,
#       reset to 0 on an improving round (or the first/baseline round).
#   (B) failover wording: surgical style + consec_stagnant >= 2 replaces the
#       SURGICAL EDITS recipe with a STRATEGY CHANGE / one-full-rewrite directive
#       (no !!DRAFT!! marker instructions).
#   (C) unknown-path hint: when the issues text names "<unknown path>", append a
#       note recommending a full per-node rewrite for those nodes.
# All end-to-end assertions need the local n8n-mcp validator and SKIP otherwise.

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

echo "=== stagnation failover tests ==="

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# Hermetic per-user runtime dir (hook resolves $NK_STATE_DIR/workflow-validation from this)
export N8N_KNOWLEDGE_RUNTIME_DIR="$WORK_DIR/runtime"

STATE_DIR="$N8N_KNOWLEDGE_RUNTIME_DIR/state/workflow-validation"
mkdir -p "$STATE_DIR"

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

read_key() {
  # $1 = state file, $2 = key
  python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2" 2>/dev/null
}

# --- end-to-end (needs local validator) ---

if ! ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db >/dev/null 2>&1; then
  echo "  SKIP: no local n8n-mcp install — stagnation-failover not exercised"
else
  # INVALID-1: single bogus-op Slack node + trigger (error_count 1).
  write_invalid1() {
    cat > "$1" << 'JSONEOF'
{
  "name": "stagnation invalid 1",
  "nodes": [
    {"id": "t", "name": "Schedule", "type": "n8n-nodes-base.scheduleTrigger",
     "typeVersion": 1, "position": [0, 0], "parameters": {}},
    {"id": "s1", "name": "Slack1", "type": "n8n-nodes-base.slack",
     "typeVersion": 2.2, "position": [200, 0],
     "parameters": {"resource": "message", "operation": "bogusOperation"}}
  ],
  "connections": {"Schedule": {"main": [[{"node": "Slack1", "type": "main", "index": 0}]]}}
}
JSONEOF
  }
  # INVALID-2: two bogus-op Slack nodes chained (error_count 2).
  write_invalid2() {
    cat > "$1" << 'JSONEOF'
{
  "name": "stagnation invalid 2",
  "nodes": [
    {"id": "t", "name": "Schedule", "type": "n8n-nodes-base.scheduleTrigger",
     "typeVersion": 1, "position": [0, 0], "parameters": {}},
    {"id": "s1", "name": "Slack1", "type": "n8n-nodes-base.slack",
     "typeVersion": 2.2, "position": [200, 0],
     "parameters": {"resource": "message", "operation": "bogusOperation"}},
    {"id": "s2", "name": "Slack2", "type": "n8n-nodes-base.slack",
     "typeVersion": 2.2, "position": [400, 0],
     "parameters": {"resource": "message", "operation": "bogusOperation"}}
  ],
  "connections": {
    "Schedule": {"main": [[{"node": "Slack1", "type": "main", "index": 0}]]},
    "Slack1": {"main": [[{"node": "Slack2", "type": "main", "index": 0}]]}
  }
}
JSONEOF
  }
  # CODE-PRIMITIVE: a Code node returning a primitive — may yield an issues_block
  # containing "<unknown path>" depending on the validator. Used only for the
  # unknown-path hint test (SKIPs if not reproducible locally).
  write_code_primitive() {
    cat > "$1" << 'JSONEOF'
{
  "name": "code primitive",
  "nodes": [
    {"id": "1", "name": "Sched", "type": "n8n-nodes-base.scheduleTrigger",
     "typeVersion": 1.2, "position": [0, 0], "parameters": {}},
    {"id": "2", "name": "Code", "type": "n8n-nodes-base.code",
     "typeVersion": 2, "position": [200, 0],
     "parameters": {"jsCode": "return 42;"}}
  ],
  "connections": {"Sched": {"main": [[{"node": "Code", "type": "main", "index": 0}]]}}
}
JSONEOF
  }

  run_surgical() {
    # $1 = file, $2 = sid, $3 = cap (adaptive + surgical)
    hook_input "$2" "$1" | \
      CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
      CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS="$3" \
      CLAUDE_PLUGIN_OPTION_VALIDATORMODE=local \
      CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical \
      CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=adaptive bash "$HOOK" 2>/dev/null || true
  }

  # 1. consec accounting: baseline round 0, stagnant round 1 (no failover yet).
  SID1="stag-consec-$$"
  ST1="$STATE_DIR/$SID1.json"
  rm -f "$ST1"
  INV1="$WORK_DIR/c-inv1-$$.workflow.json"
  write_invalid1 "$INV1"
  R1=$(run_surgical "$INV1" "$SID1" 8)
  assert_eq "round 1 (baseline) consec_stagnant=0" "0" "$(read_key "$ST1" consec_stagnant)"
  R2=$(run_surgical "$INV1" "$SID1" 8)
  assert_eq "round 2 (stagnant) consec_stagnant=1" "1" "$(read_key "$ST1" consec_stagnant)"
  assert_contains "round 2 still shows SURGICAL EDITS recipe" "SURGICAL EDITS" "$R2"
  assert_not_contains "round 2 has no STRATEGY CHANGE" "STRATEGY CHANGE" "$R2"

  # 2. failover at consec==2.
  R3=$(run_surgical "$INV1" "$SID1" 8)
  assert_eq "round 3 consec_stagnant=2" "2" "$(read_key "$ST1" consec_stagnant)"
  assert_contains "round 3 shows STRATEGY CHANGE" "STRATEGY CHANGE" "$R3"
  assert_contains "round 3 demands one complete rewrite" "ONE complete rewrite" "$R3"
  assert_not_contains "round 3 drops the !!DRAFT!! marker instructions" "!!DRAFT!!" "$R3"
  rm -f "$ST1"

  # 3. reset on improvement.
  SID3="stag-reset-$$"
  ST3="$STATE_DIR/$SID3.json"
  rm -f "$ST3"
  IMP="$WORK_DIR/r-imp-$$.workflow.json"
  write_invalid2 "$IMP"
  run_surgical "$IMP" "$SID3" 8 > /dev/null
  assert_eq "reset baseline consec_stagnant=0" "0" "$(read_key "$ST3" consec_stagnant)"
  run_surgical "$IMP" "$SID3" 8 > /dev/null
  assert_eq "reset stagnant round consec_stagnant=1" "1" "$(read_key "$ST3" consec_stagnant)"
  write_invalid1 "$IMP"
  R3b=$(run_surgical "$IMP" "$SID3" 8)
  assert_eq "improving round resets consec_stagnant=0" "0" "$(read_key "$ST3" consec_stagnant)"
  assert_contains "improving round back to SURGICAL EDITS recipe" "SURGICAL EDITS" "$R3b"
  assert_not_contains "improving round has no STRATEGY CHANGE" "STRATEGY CHANGE" "$R3b"
  rm -f "$ST3"

  # 4. static+rewrite mode untouched (no budget-mode env, no edit-style env).
  SID4="stag-static-$$"
  ST4="$STATE_DIR/$SID4.json"
  rm -f "$ST4"
  INV4="$WORK_DIR/s-inv-$$.workflow.json"
  write_invalid1 "$INV4"
  S1=$(hook_input "$SID4" "$INV4" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=static \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=rewrite \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=8 \
    CLAUDE_PLUGIN_OPTION_VALIDATORMODE=local bash "$HOOK" 2>/dev/null || true)
  S2=$(hook_input "$SID4" "$INV4" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=static \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=rewrite \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=8 \
    CLAUDE_PLUGIN_OPTION_VALIDATORMODE=local bash "$HOOK" 2>/dev/null || true)
  assert_not_contains "static/rewrite round 1 has no STRATEGY CHANGE" "STRATEGY CHANGE" "$S1"
  assert_not_contains "static/rewrite round 2 has no STRATEGY CHANGE" "STRATEGY CHANGE" "$S2"
  assert_contains "static/rewrite keeps classic re-write wording" "one complete re-write" "$S2"
  rm -f "$ST4"

  # 5. unknown-path hint (SKIP if validator does not produce <unknown path>).
  SID5="stag-unknown-$$"
  ST5="$STATE_DIR/$SID5.json"
  rm -f "$ST5"
  CODE="$WORK_DIR/u-code-$$.workflow.json"
  write_code_primitive "$CODE"
  R5=$(run_surgical "$CODE" "$SID5" 8)
  if echo "$R5" | grep -Fq "<unknown path>"; then
    assert_contains "unknown-path hint appears when target unresolvable" \
      "no resolvable patch target" "$R5"
    assert_contains "unknown-path hint recommends full node rewrite" \
      "rewrite the affected node's full definition" "$R5"
  else
    echo "  SKIP: unknown-path not reproducible locally"
  fi
  rm -f "$ST5"
fi

echo ""
echo "stagnation-failover: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
