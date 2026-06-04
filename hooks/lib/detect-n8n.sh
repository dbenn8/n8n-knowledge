#!/usr/bin/env bash
# detect-n8n.sh — Determine if a user message should trigger n8n recall
# Returns: "yes" or "no" on stdout

N8N_DEFAULT_KEYWORDS="workflow node trigger webhook credential expression execution"

resolve_trigger_keywords() {
  # Output a space-separated keyword list. Honors CLAUDE_PLUGIN_OPTION_triggerKeywords
  # (comma list). The token DEFAULTS expands to the built-in list inline.
  local cfg="${CLAUDE_PLUGIN_OPTION_triggerKeywords:-}"
  if [ -z "$cfg" ]; then
    printf '%s' "$N8N_DEFAULT_KEYWORDS"; return
  fi
  local out=""
  local IFS=','
  for w in $cfg; do
    w="$(printf '%s' "$w" | tr -d '[:space:]')"
    [ -z "$w" ] && continue
    if [ "$w" = "DEFAULTS" ]; then
      out="$out $N8N_DEFAULT_KEYWORDS"
    else
      out="$out $w"
    fi
  done
  printf '%s' "$out" | tr -s ' ' | sed 's/^ //; s/ $//'
}

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
    local kw_regex
    kw_regex="$(resolve_trigger_keywords | tr ' ' '|')"
    if printf '%s' "$lower_message" | grep -qEi "\b($kw_regex)\b"; then
      echo "yes"; return
    fi
  fi

  echo "no"
}
