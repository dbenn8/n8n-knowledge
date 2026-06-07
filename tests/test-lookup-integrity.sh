#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"
DATA_FILE="$LIB_DIR/node_lookup_data.json"

PASS=0
FAIL=0

assert_true() {
  local desc="$1" result="$2"
  if [ "$result" = "true" ]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== lookup integrity tests ==="

RESULT=$(python3 -c "
import json, sys

data = json.load(open('$DATA_FILE'))
checks = {}

# Minimum entry count (1851 nodes should produce at least 2000 entries)
checks['min_entries'] = len(data) >= 2000

# Core nodes-base nodes must be present and map to nodes-base package
core_nodes = {
    'slack': 'nodes-base.slack',
    'postgres': 'nodes-base.postgres',
    'gmail': 'nodes-base.gmail',
    'if': 'nodes-base.if',
    'set': 'nodes-base.set',
    'code': 'nodes-base.code',
    'webhook': 'nodes-base.webhook',
    'http request': 'nodes-base.httpRequest',
    'merge': 'nodes-base.merge',
    'switch': 'nodes-base.switch',
}
missing = [k for k, v in core_nodes.items() if data.get(k) != v]
checks['core_nodes_present'] = len(missing) == 0
if missing:
    print(f'MISSING: {missing}', file=sys.stderr)

# Action nodes must NOT be overwritten by triggers
action_keys = ['slack', 'postgres', 'jira', 'notion', 'hubspot', 'airtable', 'discord']
trigger_overwrites = [k for k in action_keys if 'trigger' in data.get(k, '').lower()]
checks['no_trigger_overwrites'] = len(trigger_overwrites) == 0
if trigger_overwrites:
    print(f'TRIGGER OVERWRITES: {trigger_overwrites}', file=sys.stderr)

# Trigger variants should exist separately
trigger_nodes = ['slacktrigger', 'gmailTrigger', 'schedule trigger']
checks['trigger_variants_exist'] = all(k.lower() in {k2.lower() for k2 in data} for k in trigger_nodes)

# CamelCase splits should produce multi-word entries
checks['camelcase_splits'] = 'http request' in data and 'google sheets' in data

# All values must be non-empty strings
checks['all_values_valid'] = all(isinstance(v, str) and len(v) > 0 for v in data.values())

# All keys must be lowercase
checks['all_keys_lowercase'] = all(k == k.lower() for k in data.keys())

# nodes-base should be preferred for common names
base_preferred = ['slack', 'postgres', 'code', 'merge', 'set', 'if']
checks['nodes_base_preferred'] = all(data.get(k, '').startswith('nodes-base.') for k in base_preferred)

for name, passed in checks.items():
    print(f'{name}|{\"true\" if passed else \"false\"}')
" 2>&1)

while IFS='|' read -r check_name result; do
  # Skip stderr lines (MISSING/TRIGGER OVERWRITES debug)
  if [[ "$check_name" == *":"* ]] || [[ -z "$result" ]]; then
    echo "  DEBUG: $check_name"
    continue
  fi
  assert_true "$check_name" "$result"
done <<< "$RESULT"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
