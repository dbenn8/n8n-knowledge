#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/validate-workflow.sh"
PASS=0; FAIL=0
assert_contains(){ local d="$1" n="$2" h="$3"; if echo "$h"|grep -Fq "$n"; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$n')"; FAIL=$((FAIL+1)); fi; }
assert_eq(){ local d="$1" e="$2" a="$3"; if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$e' got '$a')"; FAIL=$((FAIL+1)); fi; }

echo "=== workflow validation hook tests ==="

mk_payload(){ python3 -c "import json,sys; print(json.dumps({'session_id':'validator-test','cwd':'$SCRIPT_DIR/..','tool_name':sys.argv[1],'tool_input':{'file_path':sys.argv[2],'content':'x'}}))" "$1" "$2"; }

TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT
STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"
rm -f "$STATE_DIR/validator-test.json" "$STATE_DIR/validator-cap-test.json" "$STATE_DIR/validator-nonwf.json"

WF="$TMPDIR_TEST/workflow.json"
python3 - <<'PYEOF' > "$WF"
import json
print(json.dumps({
    "name": "Test Workflow",
    "nodes": [{"id":"manual-trigger-1","name":"Manual Trigger","type":"n8n-nodes-base.manualTrigger","typeVersion":1,"position":[260,300],"parameters":{}}],
    "connections": {}
}))
PYEOF

# Disabled by default -> no output.
out0=$(bash "$HOOK" <<< "$(mk_payload Write "$WF")")
assert_eq "disabled by default" "" "$out0"

# Enabled + valid mock -> injects a success notice with a completeness gate.
VALID_MOCK='{"valid": true, "error_count": 0, "warning_count": 0, "errors": [], "warnings": [], "statistics": {"totalNodes": 1, "triggerNodes": 1}, "suggestions": []}'
out1=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$VALID_MOCK" bash "$HOOK" <<< "$(mk_payload Write "$WF")")
assert_contains "valid workflow returns hook json" "hookSpecificOutput" "$out1"
assert_contains "valid workflow includes success header" "n8n Workflow Validator" "$out1"
assert_contains "valid workflow includes pass notice" "Validation passed" "$out1"
assert_contains "valid workflow includes completeness gate" "fully solve the user's original request" "$out1"

# Enabled + invalid mock -> injects PostToolUse additionalContext.
INVALID_MOCK='{"valid": false, "error_count": 1, "warning_count": 0, "errors": [{"type":"error","message":"Required property '\''To'\'' cannot be empty","node":"Slack"}], "warnings": [], "statistics": {"totalNodes": 2, "triggerNodes": 1}, "suggestions": []}'
out2=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$INVALID_MOCK" bash "$HOOK" <<< "$(mk_payload Write "$WF")")
assert_contains "invalid workflow returns hook json" "hookSpecificOutput" "$out2"
assert_contains "invalid workflow uses PostToolUse" "\"hookEventName\": \"PostToolUse\"" "$out2"
assert_contains "invalid workflow includes feedback" "Required property 'To' cannot be empty" "$out2"
assert_contains "invalid workflow includes validator header" "n8n Workflow Validator" "$out2"
assert_contains "invalid workflow marks mock validator target" "Validator target: mock" "$out2"
assert_contains "invalid workflow includes structured patch targets" "Structured patch targets" "$out2"
assert_contains "invalid workflow includes targeted edit instruction" "smallest targeted edits possible" "$out2"

# Invalid enum/select style error -> include targeted field guidance when node + field exist.
WF_ENUM="$TMPDIR_TEST/workflow-enum.json"
python3 - <<'PYEOF' > "$WF_ENUM"
import json
print(json.dumps({
    "name": "HubSpot Test Workflow",
    "nodes": [{
        "id":"hubspot-1",
        "name":"HubSpot - Create Deal",
        "type":"n8n-nodes-base.hubspot",
        "typeVersion":1,
        "position":[260,300],
        "parameters":{"stage":"appointmentscheduled"}
    }],
    "connections": {}
}))
PYEOF
INVALID_ENUM_MOCK='{"valid": false, "error_count": 1, "warning_count": 0, "errors": [{"type":"error","message":"Invalid value for '\''stage'\''. Must be one of: appointmentscheduled, qualifiedtobuy","node":"HubSpot - Create Deal"}], "warnings": [], "statistics": {"totalNodes": 1, "triggerNodes": 0}, "suggestions": []}'
out_enum=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$INVALID_ENUM_MOCK" bash "$HOOK" <<< "$(mk_payload Write "$WF_ENUM")")
assert_contains "enum error includes parameter path" "nodes[name=HubSpot - Create Deal].parameters.stage" "$out_enum"
assert_contains "enum error includes allowed values" "appointmentscheduled, qualifiedtobuy" "$out_enum"
assert_contains "enum error includes targeted replace guidance" "Replace only the invalid \`stage\` value" "$out_enum"

# Max-calls cap -> third call with cap 2 should inject a stop-loop warning.
SIDCAP="validator-cap-test"
mk_payload_cap(){ python3 -c "import json,sys; print(json.dumps({'session_id':'$SIDCAP','cwd':'$SCRIPT_DIR/..','tool_name':sys.argv[1],'tool_input':{'file_path':sys.argv[2],'content':'x'}}))" "$1" "$2"; }
rm -f "$STATE_DIR/${SIDCAP}.json"
cap1=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$INVALID_MOCK" bash "$HOOK" <<< "$(mk_payload_cap Write "$WF")")
cap2=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$INVALID_MOCK" bash "$HOOK" <<< "$(mk_payload_cap Write "$WF")")
cap3=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=2 N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$INVALID_MOCK" bash "$HOOK" <<< "$(mk_payload_cap Write "$WF")")
assert_contains "cap first call injects" "hookSpecificOutput" "$cap1"
assert_contains "cap second call injects" "hookSpecificOutput" "$cap2"
assert_contains "cap third call warns" "Validator limit reached for this session" "$cap3"

# Non-workflow JSON file -> skip.
NONWF="$TMPDIR_TEST/package.json"
printf '{"name":"pkg"}' > "$NONWF"
mk_payload_nonwf(){ python3 -c "import json,sys; print(json.dumps({'session_id':'validator-nonwf','cwd':'$SCRIPT_DIR/..','tool_name':sys.argv[1],'tool_input':{'file_path':sys.argv[2],'content':'x'}}))" "$1" "$2"; }
out3=$(CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE="$INVALID_MOCK" bash "$HOOK" <<< "$(mk_payload_nonwf Write "$NONWF")")
assert_eq "non-workflow json skipped" "" "$out3"

echo ""; echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
