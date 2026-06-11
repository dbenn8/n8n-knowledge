#!/usr/bin/env bash
# recall.sh — Call Hindsight recall API and format results for hook output

# Shared endpoint resolution (RECALL_URL-overridable) + curl/escape helpers.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recall_common.sh"

do_recall() {
  local query="$1"
  local budget="${2:-low}"
  local max_tokens="${3:-3000}"
  recall_post "$(printf '{"query": %s, "budget": "%s", "max_tokens": %s, "include": {"source_facts": {}}}' \
    "$(printf '%s' "$query" | recall_json_escape)" \
    "$budget" "$max_tokens")"
}

format_recall_results() {
  local response_file="$1"
  local project_dir="${2:-}"
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  # || true: a formatter failure must not kill set -e hook callers
  python3 "$lib_dir/format_results.py" "$response_file" "$project_dir" 2>/dev/null || true
}
