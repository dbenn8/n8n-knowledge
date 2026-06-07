#!/usr/bin/env bash
# structured_recall.sh — Tag-filtered recall for node specs from Hindsight

RECALL_URL="${RECALL_URL:-https://n8nhindsight.applikuapp.com/public/recall}"

do_structured_recall() {
  local node_type="$1"
  local resource="${2:-}"
  local display_name
  display_name=$(echo "$node_type" | sed 's/.*\.//' | sed 's/\([A-Z]\)/ \1/g' | sed 's/^ //')

  local tags_json='["type:node-spec", "node:'"$node_type"'"]'
  if [ -n "$resource" ]; then
    tags_json='["type:node-spec", "node:'"$node_type"'", "resource:'"$resource"'"]'
  fi

  local query_escaped
  query_escaped=$(printf '%s node specification' "$display_name" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')

  curl -s -X POST "$RECALL_URL" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"query": %s, "budget": "low", "max_tokens": 3000, "tags": %s, "tags_match": "all"}' \
      "$query_escaped" "$tags_json")"
}
