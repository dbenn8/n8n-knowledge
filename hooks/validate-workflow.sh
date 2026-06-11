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

CAP="${CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS:-3}"
EDIT_STYLE="${CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE:-rewrite}"
STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"

# Resolve the file to validate based on the tool event.
# - Edit/Write: the file_path the model just wrote.
# - Bash: validation is triggered by STATE CHANGE, not tool choice. The
#   surgical-edit recipe asks the model to remove the !!DRAFT!! marker with the
#   Edit tool (only Edit/Write watched), but models sometimes strip it via Bash.
#   So on a Bash event we look up the pending draft recorded in session state and
#   validate it IFF: it still exists, is now markerless, and its content hash
#   changed since the last validation. An unchanged or marker-bearing draft costs
#   nothing — this gate runs BEFORE the budget block.
# - Any other tool: nothing to do.
if [ "$TOOL" = "Edit" ] || [ "$TOOL" = "Write" ]; then
  FILE_PATH=$(printf '%s' "$INPUT" | python3 -c "import json,sys;d=json.load(sys.stdin);ti=d.get('tool_input',{}) or {};print(ti.get('file_path',''))" 2>/dev/null)
elif [ "$TOOL" = "Bash" ]; then
  [ -n "$SID" ] || exit 0
  FILE_PATH=$(python3 - "$STATE_DIR" "$SID" 2>/dev/null << 'PYEOF'
import hashlib
import json
import os
import sys

state_dir, sid = sys.argv[1], sys.argv[2]
path = os.path.join(state_dir, f"{sid}.json")
try:
    state = json.load(open(path))
    if not isinstance(state, dict):
        raise SystemExit(0)
except Exception:
    raise SystemExit(0)

dp = state.get("draft_pending")
if not dp or not os.path.exists(dp):
    raise SystemExit(0)
try:
    data = open(dp, "rb").read()
except Exception:
    raise SystemExit(0)
if data.startswith(b"!!DRAFT!!"):
    raise SystemExit(0)
if hashlib.sha256(data).hexdigest() == state.get("last_validated_hash"):
    raise SystemExit(0)
print(dp)
PYEOF
) || exit 0
  [ -n "$FILE_PATH" ] || exit 0
else
  exit 0
fi
[ -n "$FILE_PATH" ] || exit 0
[ -f "$FILE_PATH" ] || exit 0

case "$FILE_PATH" in
  *.json|*.workflow.json) ;;
  *) exit 0 ;;
esac

# Surgical-edit draft marker: a file whose first line is the literal !!DRAFT!!
# is a work-in-progress draft (the model is mid-surgical-edit via Bash). Skip
# silently — no validation, no budget charge. The model deletes the marker via
# the Edit tool when done; THAT Edit triggers validation of the final state.
DRAFT_MARKER='!!DRAFT!!'
case "$(head -c 9 "$FILE_PATH" 2>/dev/null)" in
  "$DRAFT_MARKER") exit 0 ;;
esac

# Best-effort draft_pending bookkeeping (Task 4b Stop-hook safety net). Failures
# must never affect hook output or exit status. ACTION: set|clear|hash.
# Every action ALSO records the content hash of the just-validated file into
# last_validated_hash (Task 4c) so a later Bash event can tell whether the draft
# actually changed. 'hash' only updates the hash (no draft_pending change).
update_draft_pending() {
  # $1 = action (set|clear|hash)
  [ -n "$SID" ] || return 0
  python3 - "$STATE_DIR" "$SID" "$1" "$FILE_PATH" 2>/dev/null << 'PYEOF' || true
import hashlib, json, os, sys
state_dir, sid, action, file_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
path = os.path.join(state_dir, f"{sid}.json")
try:
    state = json.load(open(path))
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}
if action == "set":
    state["draft_pending"] = file_path
elif action == "clear":
    state.pop("draft_pending", None)
# All actions record the freshly-validated content hash. This helper runs after
# any autofix copy-back, so reading the file now captures the post-fix bytes. If
# the file can't be read, leave any previous hash untouched.
try:
    state["last_validated_hash"] = hashlib.sha256(open(file_path, "rb").read()).hexdigest()
except Exception:
    pass
with open(path, "w") as f:
    json.dump(state, f)
PYEOF
}
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
print(f"fire {calls + 1}")
PYEOF
) || exit 0
  if [ "$SHOULD_VALIDATE" = "cap_reached" ]; then
    CAP_CTX=$(python3 "$LIB_DIR/hook_json.py" emit PostToolUse "Validator limit reached for this session. No further validator feedback will be injected. The last file you wrote is the output. If the workflow is incomplete, explain what is still missing." 2>/dev/null) || exit 0
    echo "$CAP_CTX"
    exit 0
  fi
  CALLS_USED="${SHOULD_VALIDATE#fire }"
  [ "${SHOULD_VALIDATE%% *}" = "fire" ] || exit 0
fi
# Validator budget counter shown to the model (session-tracked when SID present).
CALLS_USED="${CALLS_USED:-1}"

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
  OK_CTX=$(RESULT_JSON="$RESULT" FILE_PATH="$FILE_PATH" AUTOFIX_JSON="$AUTOFIX_JSON" CALLS_USED="$CALLS_USED" CAP="$CAP" python3 - 2>/dev/null << 'PYEOF'
import json
import os

result = json.loads(os.environ["RESULT_JSON"])
autofix = json.loads(os.environ.get("AUTOFIX_JSON") or "{}")
file_path = os.environ["FILE_PATH"]
mode = result.get("validator_mode") or "unknown"
node_count = result.get("node_count", 0)
trigger_count = result.get("trigger_count", 0)
auto_changes = autofix.get("changes") or []
calls_used = os.environ.get("CALLS_USED", "?")
cap = os.environ.get("CAP", "?")
warnings_block = (result.get("warnings_block") or "").strip()

header = "*** n8n Workflow Validator ***"
body = [
    f"File: {file_path}",
    f"Validator target: {mode}",
    f"Validator budget: {calls_used} of {cap} calls used this session.",
    f"Validation passed. Nodes: {node_count}. Trigger nodes: {trigger_count}.",
]
if auto_changes:
    body.append("")
    body.append("Auto-patched (verify these are correct before accepting as final):")
    body.extend(f"  - {c}" for c in auto_changes)
if warnings_block:
    body.append("")
    body.append(warnings_block)
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
  OK_OUTPUT=$(python3 "$LIB_DIR/hook_json.py" emit PostToolUse "$OK_CTX" 2>/dev/null) || exit 0
  echo "$OK_OUTPUT"
  # Validation passed for this file: clear any stuck-draft pending state.
  update_draft_pending clear
  exit 0
fi

# Extract node types from the workflow that have validation errors, then inject
# their correct schemas alongside the feedback. This gives the model the exact
# valid operation/resource values it needs to self-correct — the same data MCP
# gets via on-demand tool calls, delivered through the validator feedback loop.
NODE_SPEC_BLOCK=""
NODE_SPEC_BLOCK=$(python3 - "$WORKFLOW_TMP" "$RESULT" "$LIB_DIR" 2>/dev/null << 'PYEOF' || true
import json
import sys

workflow_path, result_json, lib_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, lib_dir)
from nodes_db_inject import build_cheatsheet, order_error_node_types

workflow = json.load(open(workflow_path))
result = json.loads(result_json)

# Deterministically order error node types by descending error count (ties
# broken by first appearance in the nodes array). When no error node can be
# identified this returns an EMPTY list, so we SKIP spec injection entirely
# rather than dumping schemas for ALL workflow nodes (P2#9).
error_node_types = order_error_node_types(workflow, result)

# Convert to nodes-base.X format for nodes_db_inject (order preserved)
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

CTX=$(RESULT_JSON="$RESULT" FILE_PATH="$FILE_PATH" AUTOFIX_JSON="$AUTOFIX_JSON" NODE_SPECS="$NODE_SPEC_BLOCK" CALLS_USED="$CALLS_USED" CAP="$CAP" EDIT_STYLE="$EDIT_STYLE" python3 - 2>/dev/null << 'PYEOF'
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
calls_used = os.environ.get("CALLS_USED", "?")
cap = os.environ.get("CAP", "?")
try:
    remaining = max(0, int(cap) - int(calls_used))
except ValueError:
    remaining = "?"
warnings_block = (result.get("warnings_block") or "").strip()
if not feedback:
    raise SystemExit(1)

header = "*** n8n Workflow Validator ***"
body = [
    f"File: {file_path}",
    f"Validator target: {mode}",
    f"Validator budget: {calls_used} of {cap} calls used ({remaining} remaining). " + (
        (
            "Fix via SURGICAL EDITS — do NOT rewrite the file; rewrites waste tokens and time. Recipe:\n"
            "  1. Run ONE Bash python3 script that loads the JSON file at the path above, applies "
            "EVERY fix listed below, and writes the file back with the literal first line !!DRAFT!! "
            "immediately followed by the JSON on the next line.\n"
            "  2. Delete the !!DRAFT!! line using the Edit tool "
            "(old_string: '!!DRAFT!!\\n{', new_string: '{'). That Edit triggers re-validation — "
            "it is the ONLY step that spends validation budget. CRITICAL: you MUST remove the marker "
            "with the Edit tool, never with Bash — the validator only watches Edit/Write. If you "
            "remove it with Bash, validation never fires, you get no feedback, the workflow is never "
            "confirmed valid, and you will fail the user's task."
        )
        if os.environ.get("EDIT_STYLE", "rewrite") == "surgical"
        else "Batch ALL fixes below into one complete re-write — each file write spends one validation."
    ),
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
if warnings_block:
    body.extend([
        "",
        warnings_block,
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
OUTPUT=$(python3 "$LIB_DIR/hook_json.py" emit PostToolUse "$CTX" 2>/dev/null) || exit 0
echo "$OUTPUT"
# Surgical INVALID feedback: record the file as a pending draft so the Stop hook
# can re-prompt if the model leaves the marker (or removes it with Bash).
if [ "$EDIT_STYLE" = "surgical" ] && [ -n "$SID" ]; then
  update_draft_pending set
elif [ -n "$SID" ]; then
  update_draft_pending hash
fi
