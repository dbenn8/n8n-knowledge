#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"

PASS=0
FAIL=0

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

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -q "$needle"; then
    echo "  FAIL: $desc (should NOT contain '$needle')"
    FAIL=$((FAIL + 1))
  else
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  fi
}

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

assert_valid_json() {
  local desc="$1" json="$2"
  if echo "$json" | python3 -m json.tool > /dev/null 2>&1; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (invalid JSON)"
    FAIL=$((FAIL + 1))
  fi
}

confidence_of() { # $1=context  $2=text fragment
  python3 -c "
import sys, re
ctx, frag = sys.argv[1], sys.argv[2]
for block in re.findall(r'<result\b.*?</result>', ctx, re.S):
    if frag in block:
        m = re.search(r'confidence=\"(\w+)\"', block)
        print(m.group(1) if m else ''); break
" "$1" "$2"
}

echo "=== recall format tests ==="

FIXTURE="$SCRIPT_DIR/fixtures/recall-response.json"
result=$(python3 "$LIB_DIR/format_results.py" "$FIXTURE")
assert_valid_json "output is valid JSON" "$result"

assert_contains "has hookEventName" "UserPromptSubmit" "$result"

context=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")
assert_contains "has knowledge base header" "n8n Knowledge Base" "$context"
assert_contains "has confidence legend" "HIGH = official docs" "$context"

# Docs should score HIGH (base 80)
assert_eq "docs result is HIGH" "HIGH" "$(confidence_of "$context" "docker-com")"

# GitHub with in-linear + team:ai + 15 reactions + 8 comments (49+25+5+20=99 engagement=15+32=47) = HIGH
assert_eq "github with in-linear is HIGH" "HIGH" "$(confidence_of "$context" "discards incoming request headers")"
assert_contains "github in-linear shows team:ai" "team:ai" "$context"

# Solved community with some engagement should score MEDIUM or higher
assert_contains "solved community has solved label" "solved" "$context"

# Built-with-n8n with high engagement (22 likes, 8 votes, 1200 views) should NOT be LOW
assert_not_contains "high-engagement built-with is not LOW" "LOW" "$(confidence_of "$context" "automated invoice processing")"

# Low-engagement unsolved community gets filtered out (max_low_results=1, github LOW scores higher)
assert_not_contains "low-engagement community filtered by max_low_results" "connect n8n to their local database" "$context"

# Source URLs should be present in suffixes
assert_contains "has docs URL" "docs.n8n.io" "$context"
assert_contains "has github URL" "github.com" "$context"

# GitHub no-signal issue (hhh: base 49, no bonuses) should be LOW
assert_eq "github no signals is LOW" "LOW" "$(confidence_of "$context" "zero engagement")"

# Stale github (fff: closed+completed BUT has Stale label = no clear_signal_bonus, base 49 + medium engagement 1+8=9 < 10 so no bonus = 49) = LOW
assert_contains "stale github shows stale hint" "stale.*no resolution" "$context"

# GitHub closed not_planned with MEMBER author (ggg: 49+25+5+10=89) = HIGH
assert_eq "not_planned member is HIGH" "HIGH" "$(confidence_of "$context" "not planned for current archit")"

# Resolution bucket hints
assert_contains "acknowledged hint in suffix" "acknowledged" "$context"
assert_contains "wont fix hint in suffix" "won.t fix" "$context"
assert_contains "no resolution hint" "no resolution yet" "$context"

# Community suffix has engagement details
assert_contains "community suffix has views" "views" "$context"

# Docs suffix has Source URL
assert_contains "docs suffix has source" "Source.*docs.n8n.io" "$context"

# Metadata suffix has reactions and comments for github
assert_contains "github suffix has reactions" "reactions" "$context"
assert_contains "github suffix has comments" "comments" "$context"

# Empty results returns nothing
echo '{"results": []}' > /tmp/empty-recall.json
empty_result=$(python3 "$LIB_DIR/format_results.py" "/tmp/empty-recall.json" 2>/dev/null) || true
assert_eq "empty results returns empty string" "" "${empty_result:-}"
rm /tmp/empty-recall.json

# Consolidated observation: source URL traced from source_facts (no second call).
CONSOLIDATED_FIXTURE="$SCRIPT_DIR/fixtures/recall-response-consolidated.json"
consolidated=$(python3 "$LIB_DIR/format_results.py" "$CONSOLIDATED_FIXTURE" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])" 2>/dev/null) || true
assert_contains "consolidated output uses result tags" "<result" "$consolidated"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
