#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/auto-recall.sh"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected '$expected', got '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -q "$needle"; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== auto-recall integration tests ==="

# Test 1: Non-n8n message produces no output
output=$(cat "$SCRIPT_DIR/fixtures/prompt-without-n8n.json" | CLAUDE_PLUGIN_OPTION_enableAutoRecall=true bash "$HOOK" 2>/dev/null)
assert_eq "non-n8n message returns empty" "" "$output"

# Test 2: n8n message produces JSON output (hits live API)
HINDSIGHT_URL="https://n8nhindsight.applikuapp.com"
if curl -s --max-time 3 "$HINDSIGHT_URL/health" > /dev/null 2>&1; then
  output=$(cat "$SCRIPT_DIR/fixtures/prompt-with-n8n.json" | CLAUDE_PLUGIN_OPTION_enableAutoRecall=true bash "$HOOK" 2>/dev/null)
  assert_contains "n8n message returns knowledge base results" "n8n Knowledge Base" "$output"
else
  echo "  SKIP: n8n message test (API unreachable)"
fi

# Test 3: Disabled auto-recall produces no output
output=$(cat "$SCRIPT_DIR/fixtures/prompt-with-n8n.json" | CLAUDE_PLUGIN_OPTION_enableAutoRecall=false bash "$HOOK" 2>/dev/null)
assert_eq "disabled auto-recall returns empty" "" "$output"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
