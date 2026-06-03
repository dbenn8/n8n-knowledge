#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"
PASS=0; FAIL=0
assert_contains(){ local d="$1" n="$2" h="$3"; if echo "$h"|grep -q "$n"; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$n')"; FAIL=$((FAIL+1)); fi; }
assert_eq(){ local d="$1" e="$2" a="$3"; if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$e' got '$a')"; FAIL=$((FAIL+1)); fi; }

echo "=== backstop tests ==="

# Task 1: do_recall accepts budget + max_tokens and builds the right payload.
payload=$(
  source "$LIB_DIR/recall.sh"
  # shadow curl so do_recall prints its JSON body instead of calling the network
  curl(){ for a in "$@"; do prev="${prev:-}"; if [ "$prev" = "-d" ]; then printf '%s' "$a"; fi; prev="$a"; done; }
  export -f curl 2>/dev/null || true
  do_recall "test query" "high" "8000"
)
assert_contains "do_recall sends budget high" '"budget": "high"' "$payload"
assert_contains "do_recall sends max_tokens 8000" '"max_tokens": 8000' "$payload"
assert_contains "do_recall keeps source_facts" '"source_facts"' "$payload"

# Task 2: format_results.py supports --event and --bare.
FIX="$SCRIPT_DIR/fixtures/recall-with-source-facts.json"
bare=$(python3 "$LIB_DIR/format_results.py" "$FIX" --bare 2>/dev/null)
assert_contains "bare mode emits <result> tags" "<result" "$bare"
assert_contains "bare mode omits hook json wrapper" "n8n Knowledge Base" "$bare"
case "$bare" in *hookSpecificOutput*) echo "  FAIL: bare should not wrap in hook json"; FAIL=$((FAIL+1));; *) echo "  PASS: bare has no hook json"; PASS=$((PASS+1));; esac
evt=$(python3 "$LIB_DIR/format_results.py" "$FIX" --event PostToolUse 2>/dev/null)
assert_contains "event arg sets PostToolUse" '"hookEventName": "PostToolUse"' "$evt"

# Task 3: recall-cli.sh returns a bare <result> block (mock do_recall to fixture).
cli=$(RECALL_CLI_TEST=1 RECALL_FIXTURE="$SCRIPT_DIR/fixtures/recall-with-source-facts.json" bash "$LIB_DIR/recall-cli.sh" "connect Claude Desktop n8n MCP" high 8000 2>/dev/null)
assert_contains "recall-cli emits <result>" "<result" "$cli"
assert_contains "recall-cli has no hook json" "n8n Knowledge Base" "$cli"

# Task 5: trigger keywords configurable, DEFAULTS sentinel.
r1=$(CLAUDE_PLUGIN_OPTION_triggerKeywords="DEFAULTS, gizmo" bash -c 'source "'"$LIB_DIR"'/detect-n8n.sh"; resolve_trigger_keywords')
assert_contains "DEFAULTS expands to built-ins" "workflow" "$r1"
assert_contains "DEFAULTS keeps additions" "gizmo" "$r1"
r2=$(CLAUDE_PLUGIN_OPTION_triggerKeywords="alpha, beta" bash -c 'source "'"$LIB_DIR"'/detect-n8n.sh"; resolve_trigger_keywords')
assert_contains "replace mode keeps custom" "alpha" "$r2"
case "$r2" in *workflow*) echo "  FAIL: replace mode should drop defaults"; FAIL=$((FAIL+1));; *) echo "  PASS: replace drops defaults"; PASS=$((PASS+1));; esac
r3=$(bash -c 'source "'"$LIB_DIR"'/detect-n8n.sh"; resolve_trigger_keywords')
assert_contains "unset uses defaults" "webhook" "$r3"

# Task 6: state load/save + staleness + decide.
st=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import backstop_state as s
state = s.new_state()
assert state['total_calls']==0 and state['topics']=={}
# stale: recorded at total=0,trigger=0; now total=16 -> stale by total
assert s.is_stale({'at_total':0,'at_trigger':0}, 16, 0) is True
assert s.is_stale({'at_total':0,'at_trigger':0}, 10, 6) is True   # stale by trigger
assert s.is_stale({'at_total':0,'at_trigger':0}, 10, 4) is False  # fresh
# decide: new signature fires under cap
state['recalls_done']=0
assert s.decide(state, ['webhook'], cap=4) is True
# at cap -> no fire
state['recalls_done']=4
assert s.decide(state, ['webhook'], cap=4) is False
print('ok')
")
assert_eq "backstop_state logic" "ok" "$st"

echo ""; echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
