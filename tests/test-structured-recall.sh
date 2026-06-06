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

confidence_of() {
  python3 -c "
import sys, re
ctx, frag = sys.argv[1], sys.argv[2]
for block in re.findall(r'<result\b.*?</result>', ctx, re.S):
    if frag in block:
        m = re.search(r'confidence=\"(\w+)\"', block)
        print(m.group(1) if m else ''); break
" "$1" "$2"
}

kind_of() {
  python3 -c "
import sys, re
ctx, frag = sys.argv[1], sys.argv[2]
for block in re.findall(r'<result\b.*?</result>', ctx, re.S):
    if frag in block:
        m = re.search(r'kind=\"([^\"]+)\"', block)
        print(m.group(1) if m else ''); break
" "$1" "$2"
}

result_n_of() {
  python3 -c "
import sys, re
ctx, frag = sys.argv[1], sys.argv[2]
for block in re.findall(r'<result\b.*?</result>', ctx, re.S):
    if frag in block:
        m = re.search(r'n=\"(\d+)\"', block)
        print(m.group(1) if m else ''); break
" "$1" "$2"
}

echo "=== structured recall tests ==="

# --- Test 1: Node-spec only fixture ---
echo ""
echo "--- node-spec only ---"
FIXTURE="$SCRIPT_DIR/fixtures/node-spec-recall.json"
result=$(python3 "$LIB_DIR/format_results.py" "$FIXTURE" --bare 2>/dev/null)

assert_contains "node-spec renders with kind=node-spec" 'kind="node-spec"' "$result"
assert_contains "node-spec has HIGH confidence" 'confidence="HIGH"' "$result"
assert_contains "display name appears" "Node: Slack" "$result"
assert_contains "node type appears" "nodes-base.slack" "$result"
assert_contains "resource.operation shown" "Operation: message.post" "$result"
assert_contains "full spec message present" "Full property spec available" "$result"
assert_contains "source attribution present" "Source: n8n node introspection" "$result"
assert_contains "text content included" "Fields: channel" "$result"
assert_contains "has knowledge base header" "n8n Knowledge Base" "$result"

# --- Test 2: Mixed fixture (node-spec + regular results) ---
echo ""
echo "--- mixed results ---"
MIXED_FIXTURE="$SCRIPT_DIR/fixtures/mixed-with-node-spec.json"
mixed_result=$(python3 "$LIB_DIR/format_results.py" "$MIXED_FIXTURE" --bare 2>/dev/null)

# Node-spec should be rendered first (n=1)
assert_eq "node-spec is result n=1" "1" "$(result_n_of "$mixed_result" "Gmail node")"
assert_eq "node-spec kind is node-spec" "node-spec" "$(kind_of "$mixed_result" "Gmail node")"

# Regular solved community post should also appear
assert_contains "regular result appears" "Gmail OAuth2" "$mixed_result"
assert_eq "regular result kind is post" "post" "$(kind_of "$mixed_result" "Gmail OAuth2")"

# Node-spec should not consume a slot (regular result n should be 2, not 1)
assert_eq "regular result numbered after node-spec" "2" "$(result_n_of "$mixed_result" "Gmail OAuth2")"

# Node-spec always HIGH regardless of engagement
assert_eq "node-spec always HIGH" "HIGH" "$(confidence_of "$mixed_result" "Gmail node")"

# Low-engagement regular post should still be subject to filtering
# (max_low_results=1, so the low-engagement post may or may not appear depending on scoring)
# The solved post should score HIGH (community base 40 + solved 25 + engagement 20 = 85)
assert_eq "solved community is HIGH" "HIGH" "$(confidence_of "$mixed_result" "Gmail OAuth2")"

# --- Test 3: Node-spec with minimal metadata ---
echo ""
echo "--- minimal metadata node-spec ---"
MINIMAL_TMPFILE=$(mktemp)
trap 'rm -f "$MINIMAL_TMPFILE"' EXIT
cat > "$MINIMAL_TMPFILE" <<'ENDJSON'
{
  "results": [
    {
      "text": "HTTP Request node — make HTTP calls to external APIs",
      "tags": ["type:node-spec", "node:nodes-base.httpRequest"],
      "metadata": {},
      "type": "memory"
    }
  ]
}
ENDJSON

minimal_result=$(python3 "$LIB_DIR/format_results.py" "$MINIMAL_TMPFILE" --bare 2>/dev/null)
assert_contains "derives display name from node type" "Http Request" "$minimal_result"
assert_contains "shows node type from tag" "nodes-base.httpRequest" "$minimal_result"
assert_contains "still has kind=node-spec" 'kind="node-spec"' "$minimal_result"
assert_contains "still HIGH confidence" 'confidence="HIGH"' "$minimal_result"
assert_not_contains "no Operation line without metadata" "Operation:" "$minimal_result"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
