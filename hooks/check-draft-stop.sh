#!/usr/bin/env bash
# Stop-hook safety net for stuck surgical-edit drafts.
#
# The surgical-edit recipe tells the model to patch a workflow file via a Bash
# python3 script that leaves the literal first line !!DRAFT!! in the file, then
# delete that marker with the Edit tool (the Edit triggers validation). Failure
# modes: the model leaves the marker, or removes it with Bash (so validation
# never fires), then ends its turn. This Stop hook catches that — it BLOCKS the
# stop and re-prompts the model to finish the recipe. Capped at 2 nudges.
#
# Never blocks on malfunction: any failure -> exit 0 with no output.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/runtime_dirs.sh"
nk_runtime_init

[ "${CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION:-false}" = "true" ] || exit 0

INPUT=$(cat 2>/dev/null) || exit 0
SID=$(printf '%s' "$INPUT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null) || exit 0
[ -n "$SID" ] || exit 0

STATE_DIR="$NK_STATE_DIR/workflow-validation"
STATE_FILE="$STATE_DIR/$SID.json"
[ -f "$STATE_FILE" ] || exit 0

# Extract the pending draft file path (empty if no draft_pending key).
DRAFT_FILE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('draft_pending',''))" "$STATE_FILE" 2>/dev/null) || exit 0
[ -n "$DRAFT_FILE" ] || exit 0

# Cap: never nudge more than twice for a session.
NUDGES=$(python3 -c "import json,sys;print(int(json.load(open(sys.argv[1])).get('draft_nudges',0)))" "$STATE_FILE" 2>/dev/null) || exit 0
[ "$NUDGES" -ge 2 ] && exit 0

# If the pending file no longer exists, nothing to nudge about.
[ -f "$DRAFT_FILE" ] || exit 0

# Determine which reason to emit: marker still present vs. marker gone but
# validation never re-ran (draft_pending was never cleared).
DRAFT_MARKER='!!DRAFT!!'
if [ "$(head -c 9 "$DRAFT_FILE" 2>/dev/null)" = "$DRAFT_MARKER" ]; then
  REASON="The workflow file $DRAFT_FILE still begins with the !!DRAFT!! marker — validation has NOT run and the file is not importable. Finish the surgical-edit recipe now: delete the !!DRAFT!! line using the Edit tool (old_string: '!!DRAFT!!\n{', new_string: '{'). That Edit triggers validation."
else
  REASON="The workflow file $DRAFT_FILE was modified but never re-validated (the marker was likely removed with Bash — the validator only sees Edit/Write). Trigger validation now: re-save the file once with the Write tool (full current content) or make a small Edit to it. Do not end the turn until the validator reports VALID."
fi

# Increment draft_nudges (best-effort; failure must not block the stop).
python3 -c "
import json, sys
path = sys.argv[1]
try:
    state = json.load(open(path))
except Exception:
    state = {}
state['draft_nudges'] = int(state.get('draft_nudges', 0)) + 1
with open(path, 'w') as f:
    json.dump(state, f)
" "$STATE_FILE" 2>/dev/null || true

# Build the block JSON with json.dumps so escaping is correct.
OUTPUT=$(REASON="$REASON" python3 -c "
import json, os
print(json.dumps({'decision': 'block', 'reason': os.environ['REASON']}))
" 2>/dev/null) || exit 0
[ -n "$OUTPUT" ] || exit 0
echo "$OUTPUT"
exit 0
