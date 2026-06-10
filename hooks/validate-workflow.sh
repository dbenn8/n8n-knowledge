#!/usr/bin/env bash
# PostToolUse workflow validation hook for written/edited n8n workflow JSON files.
# Never blocks: any failure -> exit 0, no output.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"

[ "${CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION:-false}" = "true" ] || exit 0

INPUT=$(cat 2>/dev/null) || exit 0
read_field(){ printf '%s' "$INPUT" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$1',''))" 2>/dev/null; }

SID=$(read_field session_id)
TOOL=$(read_field tool_name)
CWD=$(read_field cwd)
[ "$TOOL" = "Edit" ] || [ "$TOOL" = "Write" ] || exit 0

FILE_PATH=$(printf '%s' "$INPUT" | python3 -c "import json,sys;d=json.load(sys.stdin);ti=d.get('tool_input',{}) or {};print(ti.get('file_path',''))" 2>/dev/null)
[ -n "$FILE_PATH" ] || exit 0
[ -f "$FILE_PATH" ] || exit 0

case "$FILE_PATH" in
  *.json|*.workflow.json) ;;
  *) exit 0 ;;
esac

CAP="${CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS:-3}"
STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"
mkdir -p "$STATE_DIR" 2>/dev/null || true

if [ -n "$SID" ]; then
  SHOULD_VALIDATE=$(python3 - "$STATE_DIR" "$SID" "$CAP" 2>/dev/null << 'PYEOF'
import json
import os
import sys

state_dir, sid, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
path = os.path.join(state_dir, f"{sid}.json")
state = {"calls": 0}
if os.path.exists(path):
    try:
        state = json.load(open(path))
    except Exception:
        state = {"calls": 0}

calls = int(state.get("calls", 0))
if calls >= cap:
    print("cap_reached")
    raise SystemExit(0)

state["calls"] = calls + 1
with open(path, "w") as f:
    json.dump(state, f)
print("fire")
PYEOF
) || exit 0
  if [ "$SHOULD_VALIDATE" = "cap_reached" ]; then
    CAP_CTX=$(python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':sys.argv[1]}}))" "Validator limit reached for this session. No further validator feedback will be injected. The last file you wrote is the output. If the workflow is incomplete, explain what is still missing." 2>/dev/null) || exit 0
    echo "$CAP_CTX"
    exit 0
  fi
  [ "$SHOULD_VALIDATE" = "fire" ] || exit 0
fi

WORKFLOW_TMP=$(mktemp)
trap 'rm -f "$WORKFLOW_TMP"' EXIT

python3 - "$FILE_PATH" "$WORKFLOW_TMP" 2>/dev/null << 'PYEOF' || exit 0
import json
import sys

src = sys.argv[1]
dst = sys.argv[2]
data = json.load(open(src))
if not isinstance(data, dict):
    raise SystemExit(1)
if "nodes" not in data or "connections" not in data:
    raise SystemExit(1)
with open(dst, "w") as f:
    json.dump(data, f)
PYEOF

RESULT=$(python3 "$LIB_DIR/validator_client.py" "$WORKFLOW_TMP" "$CWD" 2>/dev/null) || exit 0
[ -n "$RESULT" ] || exit 0

VALID=$(printf '%s' "$RESULT" | python3 -c "import json,sys;print('true' if json.load(sys.stdin).get('valid') else 'false')" 2>/dev/null) || exit 0

# Apply generic auto-fixes (unambiguous, node-agnostic) before injecting feedback.
# If any fixes apply, re-validate so the model sees the updated state.
AUTOFIX_JSON="{}"
if [ "$VALID" = "false" ]; then
  AUTOFIX_JSON=$(python3 "$LIB_DIR/workflow_autofix.py" "$WORKFLOW_TMP" "$RESULT" 2>/dev/null) || AUTOFIX_JSON="{}"
  if printf '%s' "$AUTOFIX_JSON" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('changes') else 1)" 2>/dev/null; then
    cp "$WORKFLOW_TMP" "$FILE_PATH" 2>/dev/null || true
    # Telemetry: when an external harness sets N8N_KNOWLEDGE_AUTOFIX_LOG, record each
    # autofix fire (one JSON line). Unset in production -> no file written, no side effect.
    if [ -n "${N8N_KNOWLEDGE_AUTOFIX_LOG:-}" ]; then
      printf '%s\n' "$AUTOFIX_JSON" >> "$N8N_KNOWLEDGE_AUTOFIX_LOG" 2>/dev/null || true
    fi
    RESULT=$(python3 "$LIB_DIR/validator_client.py" "$WORKFLOW_TMP" "$CWD" 2>/dev/null) || exit 0
    [ -n "$RESULT" ] || exit 0
    VALID=$(printf '%s' "$RESULT" | python3 -c "import json,sys;print('true' if json.load(sys.stdin).get('valid') else 'false')" 2>/dev/null) || exit 0
  fi
fi

if [ "$VALID" = "true" ]; then
  OK_CTX=$(RESULT_JSON="$RESULT" FILE_PATH="$FILE_PATH" AUTOFIX_JSON="$AUTOFIX_JSON" python3 - 2>/dev/null << 'PYEOF'
import json
import os

result = json.loads(os.environ["RESULT_JSON"])
autofix = json.loads(os.environ.get("AUTOFIX_JSON") or "{}")
file_path = os.environ["FILE_PATH"]
mode = result.get("validator_mode") or "unknown"
node_count = result.get("node_count", 0)
trigger_count = result.get("trigger_count", 0)
auto_changes = autofix.get("changes") or []

header = "*** n8n Workflow Validator ***"
body = [
    f"File: {file_path}",
    f"Validator target: {mode}",
    f"Validation passed. Nodes: {node_count}. Trigger nodes: {trigger_count}.",
]
if auto_changes:
    body.append("")
    body.append("Auto-patched (verify these are correct before accepting as final):")
    body.extend(f"  - {c}" for c in auto_changes)
body.extend([
    "",
    "Schema check passed. Before stopping, verify: does this workflow fully solve the user's original request? Are all required nodes and connections present?",
    "- YES → The saved file is the importable output. Tell the user the filename and that they can import it directly into n8n.",
    "- NO → Add the missing steps, re-save, and wait for the next validation result.",
])
print("\n".join(body).join([header + "\n", ""]))
PYEOF
  ) || exit 0
  [ -n "$OK_CTX" ] || exit 0
  OK_OUTPUT=$(python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':sys.argv[1]}}))" "$OK_CTX" 2>/dev/null) || exit 0
  echo "$OK_OUTPUT"
  exit 0
fi

# Extract node types from the workflow that have validation errors, then inject
# their correct schemas alongside the feedback. This gives the model the exact
# valid operation/resource values it needs to self-correct — the same data MCP
# gets via on-demand tool calls, delivered through the validator feedback loop.
NODE_SPEC_BLOCK=""
NODE_SPEC_BLOCK=$(python3 - "$WORKFLOW_TMP" "$RESULT" "$LIB_DIR" 2>/dev/null << 'PYEOF' || true
import json
import os
import re
import sys

workflow_path, result_json, lib_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, lib_dir)
from nodes_db_inject import build_cheatsheet

workflow = json.load(open(workflow_path))
result = json.loads(result_json)

# Collect node types that have errors (from issues and repair_messages)
error_node_names = set()
for issue in result.get("issues", []):
    name = issue.get("node")
    if name:
        error_node_names.add(name)

# Also extract node names from repair messages (e.g., 'for node n8n-nodes-base.X')
for msg in result.get("repair_messages", []):
    for m in re.finditer(r'node (n8n-[\w.-]+)', msg):
        # This is a node type, not a name — track it directly
        pass

# Map error node names to their node types
error_node_types = set()
all_node_types = set()
for node in workflow.get("nodes", []):
    node_type = node.get("type", "")
    all_node_types.add(node_type)
    if node.get("name") in error_node_names:
        error_node_types.add(node_type)

# Also catch node types mentioned directly in error messages
for issue in result.get("issues", []):
    msg = issue.get("message", "")
    for m in re.finditer(r'n8n-[\w.-]+', msg):
        nt = m.group(0)
        if nt in all_node_types:
            error_node_types.add(nt)

# If no specific error nodes identified, inject specs for ALL workflow nodes
# (the validator may not always tag which node caused the error)
if not error_node_types:
    error_node_types = all_node_types

# Convert to nodes-base.X format for nodes_db_inject
db_types = []
for nt in error_node_types:
    if nt.startswith("n8n-"):
        db_types.append(nt[4:])  # n8n-nodes-base.slack -> nodes-base.slack
    elif nt.startswith("@n8n/n8n-"):
        db_types.append(nt[9:])  # @n8n/n8n-nodes-langchain.X -> nodes-langchain.X
    else:
        db_types.append(nt)

if db_types:
    cheatsheet = build_cheatsheet(db_types)
    if cheatsheet:
        print(cheatsheet)
PYEOF
)

CTX=$(RESULT_JSON="$RESULT" FILE_PATH="$FILE_PATH" AUTOFIX_JSON="$AUTOFIX_JSON" NODE_SPECS="$NODE_SPEC_BLOCK" python3 - 2>/dev/null << 'PYEOF'
import json
import os

result = json.loads(os.environ["RESULT_JSON"])
autofix = json.loads(os.environ.get("AUTOFIX_JSON") or "{}")
file_path = os.environ["FILE_PATH"]
mode = result.get("validator_mode") or "unknown"
feedback = result.get("feedback_block", "").strip()
issues = result.get("issues_block", "").strip()
auto_changes = autofix.get("changes") or []
node_specs = os.environ.get("NODE_SPECS", "").strip()
if not feedback:
    raise SystemExit(1)

header = "*** n8n Workflow Validator ***"
body = [
    f"File: {file_path}",
    f"Validator target: {mode}",
]
if auto_changes:
    body.extend([
        "Auto-patched (verify these are correct):",
    ] + [f"  - {c}" for c in auto_changes] + [""])
    body.append("Remaining issues to fix manually:")
else:
    body.append("The written workflow JSON is currently invalid. Fix these issues before moving on:")
body.append(feedback)
if issues:
    body.extend([
        "",
        "Structured patch targets:",
        issues,
        "",
        "Make the smallest targeted edits possible. Do not rewrite unrelated nodes or the whole workflow unless the validator error requires it.",
    ])
if node_specs:
    body.extend([
        "",
        node_specs,
    ])
print("\n".join(body).join([header + "\n", ""]))
PYEOF
) || exit 0

[ -n "$CTX" ] || exit 0
OUTPUT=$(python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':sys.argv[1]}}))" "$CTX" 2>/dev/null) || exit 0
echo "$OUTPUT"
