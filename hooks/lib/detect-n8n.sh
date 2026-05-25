#!/usr/bin/env bash
# detect-n8n.sh — Determine if a user message should trigger n8n recall
# Returns: "yes" or "no" on stdout

N8N_BROAD_KEYWORDS="workflow|node|trigger|webhook|credential|expression|execution"

is_n8n_codebase() {
  local cwd="$1"
  [ -z "$cwd" ] && { echo "no"; return; }

  if [ -f "$cwd/package.json" ] && grep -qE '"n8n[-"]' "$cwd/package.json" 2>/dev/null; then
    echo "yes"; return
  fi

  if ls "$cwd"/*.n8n.json 1>/dev/null 2>&1; then
    echo "yes"; return
  fi

  echo "no"
}

is_n8n_consumer() {
  local cwd="$1"
  [ -z "$cwd" ] && { echo "no"; return; }

  if [ -f "$cwd/docker-compose.yml" ] && grep -qi "n8n" "$cwd/docker-compose.yml" 2>/dev/null; then
    echo "yes"; return
  fi
  if [ -f "$cwd/docker-compose.yaml" ] && grep -qi "n8n" "$cwd/docker-compose.yaml" 2>/dev/null; then
    echo "yes"; return
  fi

  echo "no"
}

should_recall() {
  local message="$1"
  local cwd="$2"
  local lower_message
  lower_message=$(printf '%s' "$message" | tr '[:upper:]' '[:lower:]')

  if printf '%s' "$lower_message" | grep -qw "n8n"; then
    echo "yes"; return
  fi

  if [ "$(is_n8n_codebase "$cwd")" = "yes" ]; then
    if printf '%s' "$lower_message" | grep -qEi "\b($N8N_BROAD_KEYWORDS)\b"; then
      echo "yes"; return
    fi
  fi

  echo "no"
}
