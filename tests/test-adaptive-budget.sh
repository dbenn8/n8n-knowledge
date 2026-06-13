#!/usr/bin/env bash
set -euo pipefail

# Adaptive validation budget tests (Task 4d).
# Part 1 (no validator needed): the budget gate honors WORKFLOWVALIDATIONBUDGETMODE.
#   - static (default): every call spends one unit; calls >= cap -> cap message.
#   - adaptive: only stagnant rounds spend budget — the gate frees calls until
#     either stagnant >= cap OR calls >= cap*3 (hard ceiling).
# Part 2 (needs local n8n-mcp validator, SKIPs otherwise): stagnation accounting
#   and the adaptive budget-line wording, exercised end-to-end.

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

echo "=== adaptive budget tests ==="

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

seed_state() {
  # $1 = state file, $2 = json
  printf '%s' "$2" > "$1"
}

read_key() {
  # $1 = state file, $2 = key
  python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2" 2>/dev/null
}

# --- Part 1: budget gate behavior (validator-free) ---

# A valid-shape but invalid-JSON file: passes extension + marker checks, then the
# hook exits silently at the JSON sanity-copy step. Lets us observe gate behavior
# (cap message vs. silent proceed) WITHOUT needing a validator.
NOTJSON_FILE="$WORK_DIR/notjson.workflow.json"
printf 'notjson\n' > "$NOTJSON_FILE"

# 1. static default: calls already at cap -> cap-reached message (unchanged).
SID1="adaptive-static-cap-$$"
ST1="$STATE_DIR/$SID1.json"
seed_state "$ST1" '{"calls": 2}'
OUT1=$(hook_input "$SID1" "$NOTJSON_FILE" | \
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=static \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 bash "$HOOK")
assert_contains "static default still caps at calls>=cap" "Validator limit reached" "$OUT1"
rm -f "$ST1"

# 2. adaptive gate frees calls past the cap (stagnant under cap, calls under ceiling).
SID2="adaptive-free-$$"
ST2="$STATE_DIR/$SID2.json"
seed_state "$ST2" '{"calls": 5, "stagnant": 0}'
OUT2=$(hook_input "$SID2" "$NOTJSON_FILE" | \
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=adaptive bash "$HOOK")
assert_not_contains "adaptive frees call past cap (no cap message)" "Validator limit reached" "$OUT2"
assert_eq "adaptive increments calls past cap" "6" "$(read_key "$ST2" calls)"
rm -f "$ST2"

# 3. adaptive stagnant cap: stagnant >= cap -> cap-reached.
SID3="adaptive-stagnant-cap-$$"
ST3="$STATE_DIR/$SID3.json"
seed_state "$ST3" '{"calls": 1, "stagnant": 2}'
OUT3=$(hook_input "$SID3" "$NOTJSON_FILE" | \
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=adaptive bash "$HOOK")
assert_contains "adaptive caps when stagnant>=cap" "Validator limit reached" "$OUT3"
rm -f "$ST3"

# 4. adaptive hard ceiling: calls >= cap*3 -> cap-reached even if stagnant low.
SID4="adaptive-ceiling-$$"
ST4="$STATE_DIR/$SID4.json"
seed_state "$ST4" '{"calls": 6, "stagnant": 0}'
OUT4=$(hook_input "$SID4" "$NOTJSON_FILE" | \
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 \
  CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=adaptive bash "$HOOK")
assert_contains "adaptive caps at hard ceiling calls>=cap*3" "Validator limit reached" "$OUT4"
rm -f "$ST4"

# --- Part 2: stagnation accounting + wording (needs local validator) ---

if ! ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db >/dev/null 2>&1; then
  echo "  SKIP: no local n8n-mcp install — stagnation accounting/wording not exercised"
else
  # INVALID-1: single bogus-op Slack node + trigger (error_count 1).
  write_invalid1() {
    cat > "$1" << 'JSONEOF'
{
  "name": "adaptive invalid 1",
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
  "name": "adaptive invalid 2",
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
  write_valid() {
    cat > "$1" << 'JSONEOF'
{"nodes": [{"id": "1", "name": "When clicking Test workflow", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 0], "parameters": {}}, {"id": "2", "name": "Edit Fields", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [200, 0], "parameters": {}}], "connections": {"When clicking Test workflow": {"main": [[{"node": "Edit Fields", "type": "main", "index": 0}]]}}}
JSONEOF
  }

  run_adaptive() {
    # $1 = file, $2 = sid, $3 = cap
    hook_input "$2" "$1" | \
      CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
      CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS="$3" \
      CLAUDE_PLUGIN_OPTION_VALIDATORMODE=local \
      CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=adaptive bash "$HOOK" 2>/dev/null || true
  }

  # 5. first INVALID round sets baseline, no strike.
  SID5="adaptive-baseline-$$"
  ST5="$STATE_DIR/$SID5.json"
  rm -f "$ST5"
  INV1="$WORK_DIR/inv1-$$.workflow.json"
  write_invalid1 "$INV1"
  OUT5=$(run_adaptive "$INV1" "$SID5" 3)
  assert_contains "first INVALID round shows adaptive wording" "(adaptive)" "$OUT5"
  assert_contains "first INVALID round shows 0 stagnant" "0 of 3 stagnant" "$OUT5"
  assert_eq "first INVALID sets last_error_count=1" "1" "$(read_key "$ST5" last_error_count)"
  assert_eq "first INVALID leaves stagnant=0" "0" "$(read_key "$ST5" stagnant)"
  assert_eq "first INVALID spent one call" "1" "$(read_key "$ST5" calls)"

  # 6. stagnant round strikes (same session, same unchanged file).
  OUT6=$(run_adaptive "$INV1" "$SID5" 3)
  assert_eq "second identical INVALID strikes stagnant=1" "1" "$(read_key "$ST5" stagnant)"
  assert_eq "second INVALID spent another call" "2" "$(read_key "$ST5" calls)"
  assert_contains "second INVALID shows 1 stagnant" "1 of 3 stagnant" "$OUT6"
  rm -f "$ST5"

  # 7. improving round is free.
  SID7="adaptive-improve-$$"
  ST7="$STATE_DIR/$SID7.json"
  rm -f "$ST7"
  IMP="$WORK_DIR/imp-$$.workflow.json"
  write_invalid2 "$IMP"
  run_adaptive "$IMP" "$SID7" 3 > /dev/null
  assert_eq "improving baseline last_error_count=2" "2" "$(read_key "$ST7" last_error_count)"
  write_invalid1 "$IMP"
  OUT7=$(run_adaptive "$IMP" "$SID7" 3)
  assert_eq "improving round keeps stagnant=0" "0" "$(read_key "$ST7" stagnant)"
  assert_eq "improving round records new last_error_count=1" "1" "$(read_key "$ST7" last_error_count)"
  assert_eq "improving round spent two calls" "2" "$(read_key "$ST7" calls)"
  assert_contains "improving round shows 0 stagnant" "0 of 3 stagnant" "$OUT7"
  rm -f "$ST7"

  # 8. static wording untouched (no budget-mode env).
  SID8="adaptive-static-word-$$"
  ST8="$STATE_DIR/$SID8.json"
  rm -f "$ST8"
  INV8="$WORK_DIR/inv8-$$.workflow.json"
  write_invalid1 "$INV8"
  OUT8=$(hook_input "$SID8" "$INV8" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=3 \
    CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=static \
    CLAUDE_PLUGIN_OPTION_VALIDATORMODE=local bash "$HOOK" 2>/dev/null || true)
  assert_contains "static INVALID keeps classic budget wording" "calls used (" "$OUT8"
  assert_not_contains "static INVALID has no adaptive wording" "adaptive" "$OUT8"
  rm -f "$ST8"

  # 9. VALID path adaptive wording.
  SID9="adaptive-valid-word-$$"
  ST9="$STATE_DIR/$SID9.json"
  rm -f "$ST9"
  VAL9="$WORK_DIR/val9-$$.workflow.json"
  write_valid "$VAL9"
  OUT9=$(run_adaptive "$VAL9" "$SID9" 3)
  assert_contains "VALID path shows adaptive stagnant wording" "stagnant rounds used" "$OUT9"
  rm -f "$ST9"
fi

echo ""
echo "adaptive-budget: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
