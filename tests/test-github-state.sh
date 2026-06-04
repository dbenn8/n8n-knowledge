#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"

PASS=0
FAIL=0

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"; FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    echo "  FAIL: $desc (should NOT contain '$needle')"; FAIL=$((FAIL + 1))
  else
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  fi
}

echo "=== github state tests ==="

# Unit: github_state_tag for closed/completed
unit=$(python3 -c "import sys;sys.path.insert(0,'$LIB_DIR');import format_results as fr;print(fr.github_state_tag({'state':'closed','state_reason':'completed','closed_at':'2026-02-26T10:00:00Z'},['source:github']))")
assert_contains "github_state_tag closed/completed" "CLOSED·completed·2026-02-26" "$unit"

FIXTURE="$SCRIPT_DIR/fixtures/github-state.json"
context=$(python3 "$LIB_DIR/format_results.py" "$FIXTURE" | python3 -c "import json,sys;print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")

assert_contains "closed Slack post shows CLOSED·completed tag" "[CLOSED·completed·2026-02-26]" "$context"
assert_contains "open NDV post shows OPEN tag" "[OPEN]" "$context"
assert_contains "observation inherits source-fact CLOSED·not_planned tag" "[CLOSED·not_planned·2026-05-29]" "$context"
assert_contains "header guidance present" "Verify a result" "$context"
assert_contains "community result text present" "format a date with Luxon" "$context"
assert_not_contains "community result not tagged OPEN" "[OPEN] User asks how to format" "$context"
# Gap #1: the legacy bucket phrase must not over-assert a fix for completed closures
assert_not_contains "completed result does not over-assert 'fixed'" "fixed — update n8n for the fix" "$context"
assert_contains "completed bucket is non-asserting" "verify a fix actually shipped" "$context"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
