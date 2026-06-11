#!/usr/bin/env bash
# runtime_dirs.sh — per-user runtime paths for n8n-knowledge state + debug log.
#
# Everything used to live in world-readable /tmp; the debug log contains user
# PROMPT TEXT, so on shared machines that leaked prompts to other accounts and
# contradicted PRIVACY.md. One dir, mode 0700, log files 0600.
#
# Override with N8N_KNOWLEDGE_RUNTIME_DIR (tests use this to stay hermetic).

nk_runtime_init() {
  NK_RUNTIME_DIR="${N8N_KNOWLEDGE_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/n8n-knowledge}"
  NK_DEBUG_LOG="$NK_RUNTIME_DIR/debug.log"
  NK_STATE_DIR="$NK_RUNTIME_DIR/state"
  mkdir -p "$NK_STATE_DIR" 2>/dev/null || true
  chmod 700 "$NK_RUNTIME_DIR" 2>/dev/null || true
  export NK_RUNTIME_DIR NK_DEBUG_LOG NK_STATE_DIR
}

nk_debug_log_write() {
  # $1 = line. Never fails the caller.
  [ -n "${NK_DEBUG_LOG:-}" ] || nk_runtime_init
  { umask 077; printf '%s\n' "$1" >> "$NK_DEBUG_LOG"; } 2>/dev/null || true
}
