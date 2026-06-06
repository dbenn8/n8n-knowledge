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

echo "=== node lookup tests ==="

FIXTURE="$SCRIPT_DIR/fixtures/node-lookup-queries.json"

# Run Python to produce test results, capture to temp file to avoid subshell
TMPOUT=$(mktemp)
trap "rm -f $TMPOUT" EXIT

python3 -c "
import json, sys
sys.path.insert(0, '$LIB_DIR')
from node_lookup import identify_nodes
fixtures = json.load(open('$FIXTURE'))
for f in fixtures:
    result = identify_nodes(f['query'])
    top = result[0][1] if result else None
    exp = f['expect']
    got_base = top.split('.')[-1] if top else None
    exp_base = exp.split('.')[-1] if exp else None
    status = 'PASS' if got_base == exp_base else 'FAIL'
    print(f'{status}|{f[\"query\"][:60]}|{exp or \"None\"}|{top or \"None\"}')
" > "$TMPOUT"

while IFS='|' read -r status query exp got; do
  assert_eq "$query" "$exp" "$got"
done < "$TMPOUT"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
