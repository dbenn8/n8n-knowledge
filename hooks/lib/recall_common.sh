#!/usr/bin/env bash
# recall_common.sh — shared endpoint resolution + curl/JSON-escape helpers for the
# Hindsight recall calls. Sourced by recall.sh and structured_recall.sh so both use
# ONE endpoint resolution (env-overridable) and ONE escaping/POST implementation.

# Single endpoint resolution: default to the public applikuapp recall URL, but allow
# RECALL_URL to override it consistently for BOTH recall.sh and structured_recall.sh.
# (Before this, recall.sh hardcoded the URL and was NOT overridable — inconsistent.)
RECALL_URL="${RECALL_URL:-https://n8nhindsight.applikuapp.com/public/recall}"

# JSON-escape stdin (a string) into a quoted JSON string literal.
recall_json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'
}

# POST a JSON body to the resolved recall endpoint.
recall_post() {
  local body="$1"
  curl -s -X POST "$RECALL_URL" \
    -H "Content-Type: application/json" \
    -d "$body"
}
