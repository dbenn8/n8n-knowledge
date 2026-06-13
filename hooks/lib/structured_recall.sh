#!/usr/bin/env bash
# structured_recall.sh — Tag-filtered recall for node specs from Hindsight

# Shared endpoint resolution (RECALL_URL-overridable) + curl/escape helpers.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recall_common.sh"

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
  query_escaped=$(printf '%s node specification' "$display_name" | recall_json_escape)

  recall_post "$(printf '{"query": %s, "budget": "low", "max_tokens": 3000, "tags": %s, "tags_match": "all"}' \
    "$query_escaped" "$tags_json")"
}

do_gotcha_recall() {
  local node_type="$1"
  local service
  service=$(echo "$node_type" | sed 's/.*\.//' | sed 's/Trigger$//' | sed 's/Tool$//')

  local query_escaped
  query_escaped=$(printf '%s node bug issue workaround error' "$service" | recall_json_escape)

  local body
  body=$(printf '{"query": %s, "budget": "low", "max_tokens": 2000, "include": {"source_facts": {}}}' \
    "$query_escaped")

  local result
  result=$(recall_post "$body")

  # Retry once after 1s if the initial call returned empty/invalid JSON.
  # Under eval concurrency (16+ parallel calls hitting one Hindsight instance),
  # the 8s curl timeout frequently fires. A single retry recovers most transient
  # failures without adding meaningful latency to the happy path.
  if ! echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
    sleep 1
    echo "[$(date +%H:%M:%S)] gotcha_recall retry for $service (initial empty/failed)" \
      >> /tmp/n8n-knowledge-debug.log 2>/dev/null || true
    result=$(recall_post "$body")
  fi

  echo "$result"
}
