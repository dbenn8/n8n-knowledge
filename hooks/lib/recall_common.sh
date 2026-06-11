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
# Hard timeouts are CRITICAL: these calls run inside Claude Code hooks with a
# fixed budget (hooks.json). Without --max-time a slow endpoint hangs until the
# hook is killed, which silently drops ALL hook output (recall AND the build
# instructions). Proven in eval run 20260611-143327-v2: 4 of 6 plugin sessions
# produced no injection at all under 16-way concurrency.
# RECALL_CURL_MAX_TIME is env-tunable; failures are logged (never silent).
RECALL_CURL_MAX_TIME="${RECALL_CURL_MAX_TIME:-8}"

recall_post() {
  local body="$1"
  local rc=0
  curl -s --connect-timeout 2 --max-time "$RECALL_CURL_MAX_TIME" -X POST "$RECALL_URL" \
    -H "Content-Type: application/json" \
    -d "$body" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "[$(date +%H:%M:%S)] recall_post FAIL rc=$rc url=$RECALL_URL max_time=${RECALL_CURL_MAX_TIME}s" \
      >> /tmp/n8n-knowledge-debug.log 2>/dev/null || true
  fi
  # ALWAYS return 0: callers run under `set -e` inside Claude Code hooks. A
  # propagated curl failure killed the ENTIRE hook (this was the silent-failure
  # mechanism — no recall, no build instructions, nothing). A failed recall must
  # degrade to "no results" while the rest of the hook output still ships.
  return 0
}
