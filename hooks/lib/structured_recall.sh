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

  # source facts carry the observation provenance (URLs/engagement); without the flag the API strips source_fact_ids entirely.
  recall_post "$(printf '{"query": %s, "budget": "low", "max_tokens": 3000, "tags": %s, "tags_match": "all", "include": {"source_facts": {}}}' \
    "$query_escaped" "$tags_json")"
}

_node_to_community_tag() {
  local service="$1"
  local tag
  tag=$(echo "$service" | python3 -c "
import re, sys
s = sys.stdin.read().strip()
# camelCase to kebab-case
tag = re.sub(r'([a-z])([A-Z])', r'\1-\2', s).lower()
# Known mappings where the community tag diverges from the node name
MAP = {
    'open-ai': 'openai', 'lm-chat-open-ai': 'openai',
    'lm-open-ai': 'openai', 'open-ai-assistant': 'openai',
    'http-request': 'http-request',
    'split-in-batches': 'split-in-batches',
    'execute-workflow': 'execute-workflow',
    'schedule-trigger': 'schedule-trigger',
    'form-trigger': 'form-trigger',
}
print(MAP.get(tag, tag))
")
  echo "$tag"
}

# Extract task keywords from the user's prompt for the Layer-2 (within-node)
# query: lowercase words (3+ chars) minus stopwords and the node/service name,
# capped at 8. This is what makes a dense node (OpenAI ~40 bugs) surface the ~5
# bugs relevant to THIS task instead of a generic set. Empty string if no prompt.
_gotcha_task_keywords() {
  local service="$1"
  python3 -c "
import sys, re
prompt = sys.stdin.read().lower()
service = sys.argv[1].lower() if len(sys.argv) > 1 else ''
STOP = {'the','and','for','with','from','that','this','your','into','out','use',
        'using','via','read','reads','write','writes','get','gets','set','add',
        'adds','build','builds','create','creates','make','makes','workflow',
        'workflows','node','nodes','data','results','result','then','when','want',
        'need','needs','please','help','some','all','two','one','new','run','runs'}
words = re.findall(r'[a-z][a-z0-9]{2,}', prompt)
seen, kw = set(), []
for w in words:
    if w in STOP or w == service or w in seen:
        continue
    seen.add(w); kw.append(w)
    if len(kw) >= 8:
        break
print(' '.join(kw))
" "$service" 2>/dev/null || true
}

do_gotcha_recall() {
  local node_type="$1"
  local prompt="${2:-}"
  local service
  service=$(echo "$node_type" | sed 's/.*\.//' | sed 's/Trigger$//' | sed 's/Tool$//')

  local tag
  tag=$(_node_to_community_tag "$service")

  # Layer 2 — task-aware query: fold the prompt's task keywords into the gotcha
  # query so the contextually-relevant bugs rank to the top for dense nodes.
  local task_kw=""
  if [ -n "$prompt" ]; then
    task_kw=$(printf '%s' "$prompt" | _gotcha_task_keywords "$service")
  fi

  local query_escaped
  query_escaped=$(printf '%s %s bug issue workaround error' "$service" "$task_kw" | recall_json_escape)

  # Relevance via the node tag with tags_match=all_strict. Plain "all"/"any"
  # DELIBERATELY include untagged memories (Hindsight design) — which is exactly
  # how the old gotcha recall drowned in Facebook/Salesforce noise. all_strict
  # restricts to memories actually tagged node:<tag>; importance/ranking is then
  # carried by the engagement-based scoring downstream. No curation tag.
  # source facts carry observation provenance (URLs/engagement); without the flag the API strips source_fact_ids.
  local body
  body=$(printf '{"query": %s, "budget": "low", "max_tokens": 2000, "tags": ["node:%s"], "tags_match": "all_strict", "include": {"source_facts": {}}}' \
    "$query_escaped" "$tag")

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

    # Transition fallback: until the GitHub corpus is fully re-retained WITH
    # node:<tag> tags, an all_strict node query legitimately returns 0 (no tagged
    # memories yet). Fall back ONCE to the untagged task-aware semantic query so
    # the plugin never regresses below today's behavior during the gradual
    # re-retain. Once a node is tagged, this branch never fires.
    if ! echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
      local fb_body
      fb_body=$(printf '{"query": %s, "budget": "low", "max_tokens": 2000, "include": {"source_facts": {}}}' \
        "$query_escaped")
      result=$(recall_post "$fb_body")
    fi
  fi

  echo "$result"
}
