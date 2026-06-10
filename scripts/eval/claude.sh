#!/usr/bin/env bash
# Run evals with the normal Claude backend, ignoring any DeepSeek-related env overrides.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$SCRIPT_DIR/run-eval-v2.sh"
DEFAULT_CLAUDE_MODEL="${CLAUDE_EVAL_DEFAULT_MODEL:-claude-haiku-4-5}"

unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_MODEL
unset ANTHROPIC_DEFAULT_OPUS_MODEL
unset ANTHROPIC_DEFAULT_SONNET_MODEL
unset ANTHROPIC_DEFAULT_HAIKU_MODEL
unset CLAUDE_CODE_SUBAGENT_MODEL
unset CLAUDE_CODE_EFFORT_LEVEL

HAS_MODEL_ARG=0
for arg in "$@"; do
  if [ "$arg" = "--model" ]; then
    HAS_MODEL_ARG=1
    break
  fi
done

ARGS=("$@")
if [ "$HAS_MODEL_ARG" -eq 0 ]; then
  ARGS=(--model "$DEFAULT_CLAUDE_MODEL" "${ARGS[@]}")
fi

exec bash "$RUNNER" "${ARGS[@]}"
