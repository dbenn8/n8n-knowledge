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

echo ""; echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
