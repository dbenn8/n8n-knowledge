#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/detect-n8n.sh"
source "$SCRIPT_DIR/lib/recall.sh"

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
trap 'rm -f "$TMPFILE"' EXIT
do_recall "$PROMPT" "low" > "$TMPFILE"

# Format and output results (pass CWD for .local.md config lookup)
format_recall_results "$TMPFILE" "$CWD"
