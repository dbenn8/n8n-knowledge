#!/usr/bin/env bash
# ============================================================================
#  WORK IN PROGRESS — UNTESTED. DO NOT ENABLE.
#  PreToolUse on Task: prepend n8n context into the subagent prompt via
#  `updatedInput`. This injection path has NOT been verified at runtime — we
#  have not confirmed Claude Code actually honors updatedInput for Task calls.
#  It ships DORMANT, gated behind enableSubagentInjection (default false), so
#  the default install never executes it. Do NOT set
#  enableSubagentInjection=true until a future release verifies this works —
#  enabling it is unsupported and may not work or may corrupt subagent prompts.
# ============================================================================
# Gated by enableSubagentInjection (default false). Never blocks.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
[ "${CLAUDE_PLUGIN_OPTION_enableSubagentInjection:-false}" != "true" ] && exit 0
[ "${CLAUDE_PLUGIN_OPTION_enableBackstopRecall:-true}" = "false" ] && exit 0
source "$LIB_DIR/detect-n8n.sh" 2>/dev/null || exit 0

INPUT=$(cat 2>/dev/null) || exit 0
CWD=$(printf '%s' "$INPUT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
CAP="${CLAUDE_PLUGIN_OPTION_backstopRecallCap:-4}"
BUDGET="${CLAUDE_PLUGIN_OPTION_backstopRecallBudget:-high}"
MAXTOK="${CLAUDE_PLUGIN_OPTION_backstopRecallMaxTokens:-8000}"

DEC=$(printf '%s' "$INPUT" | KW="$(resolve_trigger_keywords)" CAP="$CAP" python3 -c '
import json,sys,os
sys.path.insert(0,"'"$LIB_DIR"'")
import backstop_state as st, query_window as qw
d=json.load(sys.stdin); sid=d.get("session_id",""); ti=d.get("tool_input",{}) or {}
state=st.load_state(sid); state["total_calls"]+=1; state["trigger_calls"]+=1
content=(ti.get("description","")+"\n"+ti.get("prompt","")).strip()
query,sig,more=qw.window_query(content, os.environ.get("KW","").split(), st.active_covered(state))
fire=bool(query and sig and st.decide(state,sig,int(os.environ.get("CAP","4"))))
if fire: st.record(state,sig)
st.save_state(sid,state)
print(("FIRE" if fire else "SKIP")+"\t"+query.replace("\t"," ").replace("\n"," "))
' 2>/dev/null) || exit 0
[ "${DEC%%	*}" != "FIRE" ] && exit 0
QUERY="${DEC#*	}"
[ "$(should_recall "$QUERY" "$CWD")" != "yes" ] && exit 0

BLOCK=$(bash "$LIB_DIR/recall-cli.sh" "$QUERY" "$BUDGET" "$MAXTOK" 2>/dev/null) || exit 0
[ -z "$BLOCK" ] && exit 0

printf '%s' "$INPUT" | BLOCK="$BLOCK" python3 -c '
import json,sys,os
d=json.load(sys.stdin); ti=d.get("tool_input",{}) or {}
header="*** n8n Knowledge Base — context for this subagent ***\n"
ti["prompt"]=header+os.environ["BLOCK"]+"\n\n"+ti.get("prompt","")
print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":ti}}))
' 2>/dev/null || exit 0
