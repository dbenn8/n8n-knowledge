#!/usr/bin/env bash
# recall.sh — Call Hindsight recall API and format results for hook output

RECALL_URL="https://n8nhindsight.applikuapp.com/public/recall"

do_recall() {
  local query="$1"
  local budget="${2:-low}"
  curl -s -X POST "$RECALL_URL" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"query": %s, "budget": "%s", "max_tokens": 3000}' "$(printf '%s' "$query" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')" "$budget")"
}

format_recall_results() {
  local response_file="$1"
  local project_dir="${2:-}"
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  python3 "$lib_dir/format_results.py" "$response_file" "$project_dir" 2>/dev/null
}
