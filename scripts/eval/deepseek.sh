#!/usr/bin/env bash
# Run evals through Claude Code pointed at DeepSeek, scoped to this child process only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.deepseek.env.local"
RUNNER="$SCRIPT_DIR/run-eval-v2.sh"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  cat >&2 <<EOF
Missing DEEPSEEK_API_KEY.

Create $ENV_FILE with:
  DEEPSEEK_API_KEY=...

You can copy the template from:
  $SCRIPT_DIR/deepseek.env.example
EOF
  exit 1
fi

PRO_MODEL="${DEEPSEEK_PRO_MODEL:-deepseek-v4-pro[1m]}"
FLASH_MODEL="${DEEPSEEK_FLASH_MODEL:-deepseek-v4-flash}"
EFFORT_LEVEL="${DEEPSEEK_EFFORT_LEVEL:-max}"
DEFAULT_CLAUDE_MODEL="${DEEPSEEK_DEFAULT_CLAUDE_MODEL:-claude-haiku-4-5}"
MAX_IN_FLIGHT_RUNS="${DEEPSEEK_MAX_IN_FLIGHT_RUNS:-16}"
MODEL_TIMEOUT_SECONDS="${DEEPSEEK_MODEL_TIMEOUT_SECONDS:-0}"

HAS_MODEL_ARG=0
for arg in "$@"; do
  if [ "$arg" = "--model" ]; then
    HAS_MODEL_ARG=1
    break
  fi
done

ARGS=("$@")
if [ "$HAS_MODEL_ARG" -eq 0 ]; then
  # Keep the safe default cheap: a haiku-shaped model maps to DeepSeek Flash.
  ARGS=(--model "$DEFAULT_CLAUDE_MODEL" "${ARGS[@]}")
fi

MODEL_NAME="$DEFAULT_CLAUDE_MODEL"
CONDITIONS_VALUE="plugin,mcp,bare"
GROUPS_VALUE="a"
for ((i=0; i<${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --model)
      if [ $((i + 1)) -lt ${#ARGS[@]} ]; then
        MODEL_NAME="${ARGS[$((i + 1))]}"
      fi
      ;;
    --conditions)
      if [ $((i + 1)) -lt ${#ARGS[@]} ]; then
        CONDITIONS_VALUE="${ARGS[$((i + 1))]}"
      fi
      ;;
    --groups)
      if [ $((i + 1)) -lt ${#ARGS[@]} ]; then
        GROUPS_VALUE="${ARGS[$((i + 1))]}"
      fi
      ;;
  esac
done

CONDITIONS_PARALLEL=0
TARGET_BACKEND="pro"
if [[ "$MODEL_NAME" == claude-haiku* ]]; then
  TARGET_BACKEND="flash"
fi

if [ "$TARGET_BACKEND" = "flash" ]; then
  if [ "$CONDITIONS_VALUE" = "plugin,mcp,bare" ] && [[ "$GROUPS_VALUE" =~ ^[abc]$ ]]; then
    CONDITIONS_PARALLEL=1
  fi
fi

# Honor a caller-provided override (e.g. small smoke tests where total sessions fit
# under the global concurrency cap and conditions can safely run fully in parallel).
if [ -n "${EVAL_CONDITIONS_PARALLEL:-}" ]; then
  CONDITIONS_PARALLEL="$EVAL_CONDITIONS_PARALLEL"
fi

# --- CRITICAL: force the real DeepSeek model id onto the wire ----------------
# Passing a Claude ALIAS (e.g. --model claude-sonnet-4-6) and relying on
# ANTHROPIC_DEFAULT_SONNET_MODEL to remap it does NOT work: Claude Code keeps the
# alias label internally but sends the actual agent turns to the HAIKU default
# (= $FLASH_MODEL). Empirically proven 2026-06-24: every "Pro" run without this
# rewrite actually ran on deepseek-v4-flash (transcripts: 0 pro responses).
# Fix: rewrite the --model VALUE to the concrete DeepSeek id so Claude Code puts it
# on the wire verbatim (probe: --model deepseek-v4-pro[1m] -> served deepseek-v4-pro).
# TARGET_BACKEND is still derived from the caller's alias above, so the existing
# interface (--model claude-sonnet-4-6 => pro, --model claude-haiku-4-5 => flash)
# is preserved; we only swap the on-the-wire value. VERIFY served model every run.
if [ "$TARGET_BACKEND" = "flash" ]; then
  WIRE_MODEL="$FLASH_MODEL"
else
  WIRE_MODEL="$PRO_MODEL"
fi
for ((i=0; i<${#ARGS[@]}; i++)); do
  if [ "${ARGS[$i]}" = "--model" ] && [ $((i + 1)) -lt ${#ARGS[@]} ]; then
    ARGS[$((i + 1))]="$WIRE_MODEL"
    break
  fi
done

ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
ANTHROPIC_MODEL="$PRO_MODEL" \
ANTHROPIC_DEFAULT_OPUS_MODEL="$PRO_MODEL" \
ANTHROPIC_DEFAULT_SONNET_MODEL="$PRO_MODEL" \
ANTHROPIC_DEFAULT_HAIKU_MODEL="$FLASH_MODEL" \
CLAUDE_CODE_SUBAGENT_MODEL="$PRO_MODEL" \
CLAUDE_CODE_EFFORT_LEVEL="$EFFORT_LEVEL" \
EVAL_CONDITIONS_PARALLEL="$CONDITIONS_PARALLEL" \
EVAL_MAX_IN_FLIGHT_RUNS="$MAX_IN_FLIGHT_RUNS" \
EVAL_MODEL_TIMEOUT_SECONDS="$MODEL_TIMEOUT_SECONDS" \
EVAL_COST_MODEL="deepseek-$TARGET_BACKEND" \
exec bash "$RUNNER" "${ARGS[@]}"
