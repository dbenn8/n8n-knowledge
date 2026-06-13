#!/usr/bin/env bash
# PostToolUse backstop recall. Never blocks: any failure -> exit 0, no output.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/runtime_dirs.sh" 2>/dev/null || exit 0
source "$LIB_DIR/detect-n8n.sh" 2>/dev/null || exit 0
# Per-user runtime paths (debug log lives under ~/.cache, not world-readable /tmp).
nk_runtime_init

[ "${CLAUDE_PLUGIN_OPTION_ENABLEBACKSTOPRECALL:-true}" = "false" ] && exit 0

INPUT=$(cat 2>/dev/null) || exit 0
read_field(){ printf '%s' "$INPUT" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$1',''))" 2>/dev/null; }
SID=$(read_field session_id); TOOL=$(read_field tool_name); CWD=$(read_field cwd)
[ -z "$TOOL" ] && exit 0

CAP="${CLAUDE_PLUGIN_OPTION_BACKSTOPRECALLCAP:-4}"
BUDGET="${CLAUDE_PLUGIN_OPTION_BACKSTOPRECALLBUDGET:-high}"
MAXTOK="${CLAUDE_PLUGIN_OPTION_BACKSTOPRECALLMAXTOKENS:-8000}"

# Always count the tool call; do full logic only for triggers.
case "$TOOL" in
  Edit|Write|Task) IS_TRIGGER=1 ;;
  *) IS_TRIGGER=0 ;;
esac

# Extract content + run decision in one python pass; emits TSV: DECISION\tQUERY\tMORE
RESULT=$(printf '%s' "$INPUT" | KW="$(resolve_trigger_keywords)" CAP="$CAP" IS_TRIGGER="$IS_TRIGGER" python3 -c '
import json,sys,os
sys.path.insert(0, "'"$LIB_DIR"'")
import backstop_state as st, query_window as qw
d=json.load(sys.stdin)
sid=d.get("session_id",""); tool=d.get("tool_name","")
ti=d.get("tool_input",{}) or {}
state=st.load_state(sid)
state["total_calls"]+=1
fire=False; query=""; more=False
if os.environ.get("IS_TRIGGER")=="1":
    state["trigger_calls"]+=1
    if tool in ("Edit","Write"):
        content=ti.get("new_string") or ti.get("content") or ""
    elif tool=="Task":
        content=(ti.get("description","")+"\n"+ti.get("prompt","")).strip()
    else:
        content=""
    kws=os.environ.get("KW","").split()
    covered=st.active_covered(state)
    query,sig,more=qw.window_query(content, kws, covered)
    if query and sig and st.decide(state, sig, int(os.environ.get("CAP","4"))):
        st.record(state, sig)
        fire=True
st.save_state(sid, state)
print(("FIRE" if fire else "SKIP")+"\t"+query.replace("\t"," ").replace("\n"," ")+"\t"+("1" if more else "0"))
' 2>/dev/null) || exit 0

DEC="${RESULT%%	*}"
REST="${RESULT#*	}"; QUERY="${REST%%	*}"; MORE="${REST##*	}"
[ "$DEC" != "FIRE" ] && exit 0

# Gate on should_recall (explicit n8n / codebase tier) using the windowed query.
[ "$(should_recall "$QUERY" "$CWD")" != "yes" ] && exit 0

BLOCK=$(bash "$LIB_DIR/recall-cli.sh" "$QUERY" "$BUDGET" "$MAXTOK" 2>/dev/null) || exit 0
[ -z "$BLOCK" ] && exit 0

if [ "$MORE" = "1" ]; then
  BLOCK="$BLOCK
note: this recall covered the first new n8n topic in your edit; the query was capped at 500 tokens, so other topics you just wrote may be uncovered — run a manual recall if you need them."
fi

HEADER="*** n8n Knowledge Base — context refresh (after $TOOL) ***"
CTX="$HEADER
$BLOCK"
OUTPUT=$(python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':sys.argv[1]}}))" "$CTX" 2>/dev/null) || exit 0

# Debug log: write injected context to file for tail -f monitoring
DEBUG="${CLAUDE_PLUGIN_OPTION_DEBUGRECALL:-summary}"
if [ "$DEBUG" != "off" ]; then
  LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
  printf '%s' "$CTX" | python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR')
import runtime_dirs
from debug_formatter import format_debug
debug_log = os.environ.get('NK_DEBUG_LOG') or runtime_dirs.debug_log_path()
os.umask(0o077)  # debug log holds injected-context text — owner-only (0600)
ctx = sys.stdin.read()
with open(debug_log, 'a') as f:
    f.write(format_debug(ctx, '$DEBUG', 'backstop after $TOOL'))
" 2>/dev/null || true
fi

echo "$OUTPUT"
