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

MENTAL_MODEL_URL="${MENTAL_MODEL_URL:-https://n8nhindsight.applikuapp.com/public/mental-models}"
MENTAL_MODEL_CACHE_DIR="${MENTAL_MODEL_CACHE_DIR:-${HOME}/.cache/n8n-knowledge/mental-models}"
MENTAL_MODEL_CACHE_TTL="${MENTAL_MODEL_CACHE_TTL:-86400}"  # 24h default
MENTAL_MODEL_MANIFEST_URL="${MENTAL_MODEL_MANIFEST_URL:-${MENTAL_MODEL_URL}/manifest}"
MENTAL_MODEL_MANIFEST_TTL="${MENTAL_MODEL_MANIFEST_TTL:-3600}"  # 1h default

# Fetch manifest if stale/missing, cache to disk. Returns 0 if manifest is available.
_ensure_manifest() {
  local manifest_file="${MENTAL_MODEL_CACHE_DIR}/manifest.json"
  if [ -f "$manifest_file" ]; then
    local now file_age
    now=$(date +%s)
    file_age=$(stat -f %m "$manifest_file" 2>/dev/null || stat -c %Y "$manifest_file" 2>/dev/null || echo 0)
    if [ $((now - file_age)) -lt "$MENTAL_MODEL_MANIFEST_TTL" ]; then
      return 0
    fi
  fi

  local auth_args=()
  if [ -n "${N8N_HINDSIGHT_API_KEY:-}" ]; then
    auth_args=(-H "Authorization: Bearer $N8N_HINDSIGHT_API_KEY")
  fi

  local result
  result=$(curl -s --connect-timeout 2 --max-time 5 \
    "${auth_args[@]+"${auth_args[@]}"}" \
    "${MENTAL_MODEL_MANIFEST_URL}" 2>/dev/null) || return 1

  echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'models' in d" 2>/dev/null || return 1
  mkdir -p "$MENTAL_MODEL_CACHE_DIR" 2>/dev/null
  printf '%s' "$result" > "$manifest_file" 2>/dev/null || return 1
}

# Get the content_hash for a tag from the cached manifest. Empty if not found.
_manifest_hash() {
  local tag="$1"
  python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
h = m.get('models', {}).get(sys.argv[2], {}).get('content_hash', '')
if h: print(h)
" "${MENTAL_MODEL_CACHE_DIR}/manifest.json" "$tag" 2>/dev/null
}

do_mental_model_recall() {
  local node_type="$1"
  local prompt="${2:-}"

  local service
  service=$(echo "$node_type" | sed 's/.*\.//' | sed 's/Trigger$//' | sed 's/Tool$//')
  local tag
  tag=$(_node_to_community_tag "$service")

  local cache_file="${MENTAL_MODEL_CACHE_DIR}/${tag}.md"
  local hash_file="${MENTAL_MODEL_CACHE_DIR}/${tag}.hash"
  local content=""

  # --- Version check: manifest (preferred) or TTL (fallback) ---
  if _ensure_manifest 2>/dev/null; then
    local manifest_hash
    manifest_hash=$(_manifest_hash "$tag")
    if [ -z "$manifest_hash" ]; then
      return 0
    fi
    if [ -f "$cache_file" ] && [ -f "$hash_file" ]; then
      local local_hash
      local_hash=$(cat "$hash_file" 2>/dev/null)
      if [ "$local_hash" = "$manifest_hash" ]; then
        content=$(cat "$cache_file")
      fi
    fi
  else
    # Manifest unavailable — fall back to TTL
    if [ -f "$cache_file" ]; then
      local now file_age
      now=$(date +%s)
      file_age=$(stat -f %m "$cache_file" 2>/dev/null || stat -c %Y "$cache_file" 2>/dev/null || echo 0)
      if [ $((now - file_age)) -lt "$MENTAL_MODEL_CACHE_TTL" ]; then
        content=$(cat "$cache_file")
      fi
    fi
  fi

  # --- Cache miss or stale: fetch from API ---
  if [ -z "$content" ]; then
    local max_time="${RECALL_CURL_MAX_TIME:-8}"

    local auth_args=()
    if [ -n "${N8N_HINDSIGHT_API_KEY:-}" ]; then
      auth_args=(-H "Authorization: Bearer $N8N_HINDSIGHT_API_KEY")
    fi

    local result
    result=$(curl -s --connect-timeout 2 --max-time "$max_time" \
      "${auth_args[@]+"${auth_args[@]}"}" \
      "${MENTAL_MODEL_URL}?tags=tag:${tag}" 2>/dev/null) || return 0

    content=$(echo "$result" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read(), strict=False)
    items = data.get('items', [])
    if items:
        c = items[0].get('content', '')
        if c and c != 'Generating content...':
            print(c)
except Exception:
    pass
" 2>/dev/null || true)

    # Write to cache with content hash
    if [ -n "$content" ]; then
      mkdir -p "$MENTAL_MODEL_CACHE_DIR" 2>/dev/null
      printf '%s' "$content" > "$cache_file" 2>/dev/null || true
      printf '%s' "$content" | shasum -a 256 | cut -d' ' -f1 > "$hash_file" 2>/dev/null || true
    fi
  fi

  [ -z "$content" ] && return 0

  # --- Section selection: inject only prompt-relevant sections (max ~4K) ---
  local selector="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/lib/section_selector.py"
  if [ -n "$prompt" ] && [ -f "$selector" ]; then
    printf '%s' "$content" | python3 "$selector" "$prompt" 2>/dev/null || printf '%s' "$content"
  else
    printf '%s' "$content"
  fi
}

do_gotcha_recall() {
  local node_type="$1"
  local service
  service=$(echo "$node_type" | sed 's/.*\.//' | sed 's/Trigger$//' | sed 's/Tool$//')

  local query_escaped
  query_escaped=$(printf '%s node bug issue workaround error' "$service" | recall_json_escape)

  # NO tag filter: github memories are not tagged node:<type>, so
  # ["source:github","node:X"] with tags_match=any degenerated to "the whole
  # github corpus" and drowned the node-specific signal (merge queries returned
  # Facebook/Salesforce error-output noise). Pure semantic ranking on the
  # node-name query returns the actual node's known issues.
  # source facts carry the observation provenance (URLs/engagement); without the flag the API strips source_fact_ids entirely.
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
