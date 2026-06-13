#!/usr/bin/env bash
set -euo pipefail

# Pins the per-user runtime-dir helpers (runtime_dirs.sh + its Python twin).
# Plugin state and the debug log used to live in world-readable /tmp; the debug
# log contains user PROMPT TEXT, so we moved everything under a single 0700 dir
# with 0600 log files. These tests assert:
#   R1  the runtime dir is created mode 700
#   R2  N8N_KNOWLEDGE_RUNTIME_DIR override is honoured
#   R3a nk_debug_log_write appends the line to debug.log
#   R3b the debug.log file is mode 600
#   R4  the Python twin exports the identical dir|log|state strings
#   R5  bash/python parity holds for an empty-but-set XDG_CACHE_HOME (both fall
#       back to ~/.cache, matching bash's :- expansion — not a "" relative path)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_SH="$SCRIPT_DIR/../hooks/lib/runtime_dirs.sh"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"

PASS=0
FAIL=0

ok() {
  echo "  PASS: $1"
  PASS=$((PASS + 1))
}

bad() {
  echo "  FAIL: $1"
  FAIL=$((FAIL + 1))
}

# Portable file-mode read: macOS stat (-f '%Lp') first, GNU stat (-c '%a') fallback.
file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null
}

echo "=== runtime dir helper tests ==="

# Hermetic runtime dir: a subpath under a fresh mktemp dir so init must create it.
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
T="$TMP_ROOT/nk-runtime"

# --- R2: override honoured; init yields NK_RUNTIME_DIR == $T ---
GOT_DIR="$(N8N_KNOWLEDGE_RUNTIME_DIR="$T" bash -c '. "'"$LIB_SH"'"; nk_runtime_init; printf "%s" "$NK_RUNTIME_DIR"')"
if [ "$GOT_DIR" = "$T" ]; then
  ok "R2: N8N_KNOWLEDGE_RUNTIME_DIR override sets NK_RUNTIME_DIR"
else
  bad "R2: NK_RUNTIME_DIR was '$GOT_DIR', expected '$T'"
fi

# --- R1: the dir was created with mode 700 ---
MODE_DIR="$(file_mode "$T")"
if [ "$MODE_DIR" = "700" ]; then
  ok "R1: runtime dir created mode 700"
else
  bad "R1: runtime dir mode was '$MODE_DIR', expected 700"
fi

# --- R3a: nk_debug_log_write appends the line to $T/debug.log ---
LINE="hello from prompt $$"
N8N_KNOWLEDGE_RUNTIME_DIR="$T" bash -c '. "'"$LIB_SH"'"; nk_runtime_init; nk_debug_log_write "'"$LINE"'"'
if [ -f "$T/debug.log" ] && grep -Fq "$LINE" "$T/debug.log"; then
  ok "R3a: nk_debug_log_write appended the line to debug.log"
else
  bad "R3a: debug.log missing or did not contain the written line"
fi

# --- R3b: debug.log is mode 600 ---
MODE_LOG="$(file_mode "$T/debug.log")"
if [ "$MODE_LOG" = "600" ]; then
  ok "R3b: debug.log created mode 600"
else
  bad "R3b: debug.log mode was '$MODE_LOG', expected 600"
fi

# --- R4: Python twin exports the identical dir|log|state strings ---
BASH_TRIPLE="$(N8N_KNOWLEDGE_RUNTIME_DIR="$T" bash -c '. "'"$LIB_SH"'"; nk_runtime_init; printf "%s|%s|%s" "$NK_RUNTIME_DIR" "$NK_DEBUG_LOG" "$NK_STATE_DIR"')"
PY_TRIPLE="$(N8N_KNOWLEDGE_RUNTIME_DIR="$T" PYTHONPATH="$LIB_DIR" python3 -c 'import runtime_dirs as r; print("%s|%s|%s" % (r.runtime_dir(), r.debug_log_path(), r.state_dir()), end="")')"
if [ "$BASH_TRIPLE" = "$PY_TRIPLE" ]; then
  ok "R4: Python twin matches bash exports ($PY_TRIPLE)"
else
  bad "R4: parity mismatch bash='$BASH_TRIPLE' python='$PY_TRIPLE'"
fi

# --- R5: empty-but-set XDG_CACHE_HOME falls back identically in bash and python ---
# With N8N_KNOWLEDGE_RUNTIME_DIR unset and XDG_CACHE_HOME="" (empty, exported),
# bash's ${XDG_CACHE_HOME:-...} and python's `or` must BOTH yield the same
# absolute path under $HOME/.cache — never a cwd-relative "n8n-knowledge".
# Use the bash :- expansion directly (not nk_runtime_init) so the test stays
# hermetic — calling init would mkdir/chmod the real ~/.cache/n8n-knowledge.
BASH_EMPTY="$(unset N8N_KNOWLEDGE_RUNTIME_DIR; XDG_CACHE_HOME="" bash -c 'printf "%s" "${N8N_KNOWLEDGE_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/n8n-knowledge}"')"
PY_EMPTY="$(unset N8N_KNOWLEDGE_RUNTIME_DIR; XDG_CACHE_HOME="" PYTHONPATH="$LIB_DIR" python3 -c 'import runtime_dirs as r; print(r.runtime_dir(), end="")')"
EXPECTED_EMPTY="$HOME/.cache/n8n-knowledge"
if [ "$BASH_EMPTY" = "$PY_EMPTY" ] && [ "$BASH_EMPTY" = "$EXPECTED_EMPTY" ]; then
  ok "R5: empty XDG_CACHE_HOME falls back to ~/.cache in both bash and python ($PY_EMPTY)"
else
  bad "R5: empty-XDG parity mismatch bash='$BASH_EMPTY' python='$PY_EMPTY' expected='$EXPECTED_EMPTY'"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
