#!/usr/bin/env bash
# recall-cli.sh <query> [budget] [max_tokens] -> bare 0.3.3 <result> block on stdout.
set -euo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$LIB_DIR/recall.sh"

QUERY="${1:-}"
BUDGET="${2:-high}"
MAX_TOKENS="${3:-8000}"
[ -z "$QUERY" ] && exit 0

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

if [ "${RECALL_CLI_TEST:-0}" = "1" ] && [ -n "${RECALL_FIXTURE:-}" ]; then
  cat "$RECALL_FIXTURE" > "$TMPFILE"
else
  do_recall "$QUERY" "$BUDGET" "$MAX_TOKENS" > "$TMPFILE" 2>/dev/null || exit 0
fi

python3 "$LIB_DIR/format_results.py" "$TMPFILE" --bare 2>/dev/null || true
