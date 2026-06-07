#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/detect-n8n.sh"
source "$SCRIPT_DIR/lib/recall.sh"
source "$SCRIPT_DIR/lib/structured_recall.sh"

# Check if auto-recall is enabled (default: true)
# Note: Claude Code uppercases all plugin option env var names
ENABLED="${CLAUDE_PLUGIN_OPTION_ENABLEAUTORECALL:-true}"
if [ "$ENABLED" = "false" ]; then
  exit 0
fi

# Read hook input from stdin
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('prompt',''))")
CWD=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))")

# Check if we should recall
if [ "$(should_recall "$PROMPT" "$CWD")" != "yes" ]; then
  exit 0
fi

# Call the recall API
TMPFILE=$(mktemp)
STRUCT_TMPFILE=$(mktemp)
GOTCHA_TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE" "$STRUCT_TMPFILE" "$GOTCHA_TMPFILE"' EXIT
do_recall "$PROMPT" "low" > "$TMPFILE"

# Check for node names in the prompt and do structured + gotcha recall if found
NODE_TYPE=$(python3 -c "
import sys, json
sys.path.insert(0, '$SCRIPT_DIR/lib')
from node_lookup import identify_nodes
prompt = json.loads(sys.stdin.read())
hits = identify_nodes(prompt)
if hits:
    print(hits[0][1])
" <<< "$(printf '%s' "$PROMPT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" 2>/dev/null || true)

if [ -n "$NODE_TYPE" ]; then
  # Run node-spec and gotcha recalls in parallel
  do_structured_recall "$NODE_TYPE" > "$STRUCT_TMPFILE" 2>/dev/null &
  do_gotcha_recall "$NODE_TYPE" > "$GOTCHA_TMPFILE" 2>/dev/null &
  wait

  # Merge: gotchas FIRST (highest priority), then semantic, then capped node specs
  python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        sem = json.load(f)
    with open(sys.argv[2]) as f:
        struct = json.load(f)
    gotcha_results = []
    try:
        with open(sys.argv[3]) as f:
            gotcha = json.load(f)
        gotcha_results = gotcha.get('results', [])[:5]
    except Exception:
        pass
    struct_results = struct.get('results', [])[:5]
    sem_results = sem.get('results', [])
    # Order: gotchas (bugs/issues) → semantic (docs/community) → node specs (reference)
    sem['results'] = gotcha_results + sem_results + struct_results
    with open(sys.argv[1], 'w') as f:
        json.dump(sem, f)
except Exception:
    pass
" "$TMPFILE" "$STRUCT_TMPFILE" "$GOTCHA_TMPFILE" 2>/dev/null || true
fi

# Format and output results (pass CWD for .local.md config lookup)
RESULT=$(format_recall_results "$TMPFILE" "$CWD")

# Debug mode: off, summary (default — condensed), full (complete with formatting)
# Output written to /tmp/n8n-knowledge-debug.log — tail -f in another terminal to watch
DEBUG="${CLAUDE_PLUGIN_OPTION_DEBUGRECALL:-summary}"
if [ "$DEBUG" != "off" ] && [ -n "$RESULT" ]; then
  echo "$RESULT" | python3 -c "
import json, sys
sys.path.insert(0, '$SCRIPT_DIR/lib')
from debug_formatter import format_debug
data = json.load(sys.stdin)
ctx = data.get('hookSpecificOutput', {}).get('additionalContext', '')
if ctx:
    with open('/tmp/n8n-knowledge-debug.log', 'a') as f:
        f.write(format_debug(ctx, '$DEBUG', 'auto-recall'))
" 2>/dev/null || true
fi

echo "$RESULT"
