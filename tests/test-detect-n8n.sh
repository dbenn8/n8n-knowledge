#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"

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

source "$LIB_DIR/detect-n8n.sh"

echo "=== detect-n8n tests ==="

# Explicit "n8n" keyword always triggers
result=$(should_recall "How do I set up n8n with Docker?" "/tmp/no-n8n-project")
assert_eq "n8n keyword triggers outside any repo" "yes" "$result"

result=$(should_recall "How do I sort a list in Python?" "/tmp/no-n8n-project")
assert_eq "non-n8n message in non-n8n repo does not trigger" "no" "$result"

result=$(should_recall "How does N8N handle webhooks?" "/tmp/no-n8n-project")
assert_eq "N8N uppercase triggers" "yes" "$result"

# Codebase tier: broad keywords trigger
mkdir -p /tmp/test-n8n-codebase
echo '{"name": "my-n8n-workflows", "dependencies": {"n8n-workflow": "1.0.0"}}' > /tmp/test-n8n-codebase/package.json
result=$(should_recall "The webhook trigger isn't firing" "/tmp/test-n8n-codebase")
assert_eq "broad keyword in n8n codebase triggers" "yes" "$result"

result=$(is_n8n_codebase "/tmp/test-n8n-codebase")
assert_eq "package.json with n8n dep detected as codebase" "yes" "$result"
rm -rf /tmp/test-n8n-codebase

# Consumer tier: broad keywords do NOT trigger
mkdir -p /tmp/test-n8n-consumer
printf 'services:\n  n8n:\n    image: n8nio/n8n' > /tmp/test-n8n-consumer/docker-compose.yml
result=$(should_recall "Why is my workflow execution failing?" "/tmp/test-n8n-consumer")
assert_eq "broad keyword in consumer repo does NOT trigger" "no" "$result"

result=$(is_n8n_consumer "/tmp/test-n8n-consumer")
assert_eq "docker-compose with n8n detected as consumer" "yes" "$result"

result=$(is_n8n_codebase "/tmp/test-n8n-consumer")
assert_eq "docker-compose only repo is NOT codebase" "no" "$result"

# Consumer tier: explicit "n8n" still triggers
result=$(should_recall "How do I update n8n in docker?" "/tmp/test-n8n-consumer")
assert_eq "explicit n8n in consumer repo triggers" "yes" "$result"
rm -rf /tmp/test-n8n-consumer

# Non-n8n repo: broad keywords do NOT trigger
result=$(should_recall "The webhook trigger isn't firing" "/tmp/no-n8n-project")
assert_eq "broad keyword outside n8n repo does not trigger" "no" "$result"

# .n8n.json workflow files make it codebase tier
mkdir -p /tmp/test-n8n-workflows
touch /tmp/test-n8n-workflows/my-flow.n8n.json
result=$(is_n8n_codebase "/tmp/test-n8n-workflows")
assert_eq ".n8n.json files detected as codebase" "yes" "$result"
result=$(should_recall "The trigger node keeps failing" "/tmp/test-n8n-workflows")
assert_eq "broad keyword with .n8n.json files triggers" "yes" "$result"
rm -rf /tmp/test-n8n-workflows

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
