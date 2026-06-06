#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/detect-n8n.sh"
source "$SCRIPT_DIR/lib/recall.sh"
source "$SCRIPT_DIR/lib/structured_recall.sh"

# Check if auto-recall is enabled (default: true)
ENABLED="${CLAUDE_PLUGIN_OPTION_enableAutoRecall:-true}"
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
trap 'rm -f "$TMPFILE" "$STRUCT_TMPFILE"' EXIT
do_recall "$PROMPT" "low" > "$TMPFILE"

# Check for node names in the prompt and do structured recall if found
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
  do_structured_recall "$NODE_TYPE" > "$STRUCT_TMPFILE" 2>/dev/null || true
  # Merge structured results (prepended) into semantic results
  python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        sem = json.load(f)
    with open(sys.argv[2]) as f:
        struct = json.load(f)
    struct_results = struct.get('results', [])
    if struct_results:
        sem['results'] = struct_results + sem.get('results', [])
    with open(sys.argv[1], 'w') as f:
        json.dump(sem, f)
except Exception:
    pass
" "$TMPFILE" "$STRUCT_TMPFILE" 2>/dev/null || true
fi

# Format and output results (pass CWD for .local.md config lookup)
RESULT=$(format_recall_results "$TMPFILE" "$CWD")

# Debug mode: off, summary (default — truncated preview), full (complete injected context)
# Output written to log file — tail -f /tmp/n8n-knowledge-debug.log in another terminal
DEBUG="${CLAUDE_PLUGIN_OPTION_debugRecall:-summary}"
if [ "$DEBUG" != "off" ] && [ -n "$RESULT" ]; then
  echo "$RESULT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
ctx = data.get('hookSpecificOutput', {}).get('additionalContext', '')
if ctx:
    mode = '$DEBUG'
    lines = ctx.split('\n')
    total = len(lines)
    with open('/tmp/n8n-knowledge-debug.log', 'a') as f:
        f.write('\n┌─── n8n-knowledge: auto-recall (' + str(total) + ' lines) ───┐\n')
        if mode == 'full':
            f.write(ctx + '\n')
        else:
            f.write('\n'.join(lines[:30]) + '\n')
            if total > 30:
                f.write(f'... ({total - 30} more lines, set debugRecall=full to see all)\n')
        f.write('└────────────────────────────────────────────────────┘\n')
" 2>/dev/null || true
fi

echo "$RESULT"
