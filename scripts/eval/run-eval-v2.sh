#!/usr/bin/env bash
# PRODUCTION EVAL HARNESS v2
# - Reads prompts from ground_truth.jsonl (no hardcoded arrays)
# - 3 conditions: bare / plugin / mcp
# - N runs per prompt per condition (default 5)
# - Full isolation via --settings
# - Parallel execution within each condition
# - Outputs per-run JSON + aggregate metrics CSV
#
# Usage:
#   bash scripts/eval/run-eval-v2.sh                    # all prompts, 5 runs each
#   bash scripts/eval/run-eval-v2.sh --limit 10         # first 10 prompts
#   bash scripts/eval/run-eval-v2.sh --runs 3           # 3 runs per prompt (default: 1)
#   bash scripts/eval/run-eval-v2.sh --conditions plugin,mcp  # skip bare
#   bash scripts/eval/run-eval-v2.sh --condition-advance-threshold 30
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/../.."
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_DIR="$REPO_DIR/out/eval/$TIMESTAMP-v2"

# Optional local env file (same pattern as deepseek.sh). Sourced BEFORE the
# defaults below so EVAL_* knobs set there feed the `${VAR:-default}` reads.
# Override order (lowest → highest precedence), consistent everywhere:
#   built-in default  <  .eval.env.local / environment  <  CLI flag
EVAL_ENV_FILE="${EVAL_ENV_FILE:-$SCRIPT_DIR/.eval.env.local}"
if [ -f "$EVAL_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$EVAL_ENV_FILE"
  set +a
fi

# Defaults
LIMIT=""
RUNS=1
CONDITIONS="plugin,mcp,bare"
MODEL="claude-sonnet-4-6"
BATCH_SIZE=0      # 0 = no batching (all parallel); set e.g. 32 to avoid rate limits
BATCH_PAUSE=10    # seconds to pause between batches
RESUME=""         # path to prior results dir — skip prompts that already have valid output
RESUME_FAILED_FROM=""  # path to prior results dir — continue unfinished prompts from saved Claude sessions when possible
RESUME_CLAUDE_CONFIG_DIR=""  # explicit Claude config/session store to use for --resume recovery
EVAL_GROUPS="a"   # comma-separated group letters to run (a, b, c); default is just group a
PROMPT_FILE_IDXS="${EVAL_PROMPT_FILE_IDXS:-}"  # optional comma-separated source line indices to run
PROMPT_IDS="${EVAL_PROMPT_IDS:-}"  # optional comma-separated prompt ids to run
CONDITIONS_PARALLEL="${EVAL_CONDITIONS_PARALLEL:-0}"  # 1 = run conditions in parallel
# ⚠️ DO NOT enable repair without reading the big warning block above repair_if_needed().
#    It is currently HALF-BROKEN for the file-based plugin flow AND biases plugin vs mcp/bare.
#    Keep this 0 for honest single-shot measurements.
REPAIR_INVALID="${EVAL_REPAIR_INVALID:-0}"            # 1 = repair invalid workflows with validator feedback (see warning @ repair_if_needed)
REPAIR_MAX_ATTEMPTS="${EVAL_REPAIR_MAX_ATTEMPTS:-3}"  # number of repair rounds after first draft
CONDITION_ADVANCE_THRESHOLD="${EVAL_CONDITION_ADVANCE_THRESHOLD:-30}"  # start next condition after N completed runs
MAX_IN_FLIGHT_RUNS="${EVAL_MAX_IN_FLIGHT_RUNS:-0}"    # 0 = unlimited; caps total concurrent runs across conditions
MODEL_TIMEOUT_SECONDS="${EVAL_MODEL_TIMEOUT_SECONDS:-0}"  # 0 or negative = DISABLED (default). Do NOT default to a positive cap: a wall-clock timeout truncates slow-but-valid runs (esp. complex group-C prompts >240s) and biases results. Override per-run via EVAL_MODEL_TIMEOUT_SECONDS / .eval.env.local / --model-timeout-seconds.
# 1 = keep session transcripts: sessions run WITH persistence and the transcript
# JSONL is moved into the per-run folder as prompt-NNN-runNN.transcript.jsonl,
# so every tool call / hook injection / validator round is reviewable post-run.
# Default 0 (off) to avoid transcript-dir churn on large batch runs.
KEEP_TRANSCRIPTS="${EVAL_KEEP_TRANSCRIPTS:-1}"
PERSIST_SESSIONS="${EVAL_PERSIST_SESSIONS:-0}"  # 1 = keep the Claude session store so failed runs can be resumed later
SESSION_PERSIST_ARGS=()
CLAUDE_BIN="${EVAL_CLAUDE_BIN:-claude}"
RESULTS_BASENAME="$(basename "$RESULTS_DIR")"
RUN_MANIFEST_PATH="$RESULTS_DIR/run-manifest.json"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --conditions) CONDITIONS="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --batch-size) BATCH_SIZE="$2"; shift 2 ;;
    --batch-pause) BATCH_PAUSE="$2"; shift 2 ;;
    --resume) RESUME="$2"; shift 2 ;;
    --resume-failed-from) RESUME_FAILED_FROM="$2"; shift 2 ;;
    --resume-claude-config-dir) RESUME_CLAUDE_CONFIG_DIR="$2"; shift 2 ;;
    --groups) EVAL_GROUPS="$2"; shift 2 ;;
    --prompt-file-idxs) PROMPT_FILE_IDXS="$2"; shift 2 ;;
    --prompt-ids) PROMPT_IDS="$2"; shift 2 ;;
    --condition-advance-threshold) CONDITION_ADVANCE_THRESHOLD="$2"; shift 2 ;;
    --max-in-flight-runs) MAX_IN_FLIGHT_RUNS="$2"; shift 2 ;;
    --model-timeout-seconds) MODEL_TIMEOUT_SECONDS="$2"; shift 2 ;;
    --persist-sessions) PERSIST_SESSIONS=1; shift ;;
    *) shift ;;
  esac
done

if [ -n "$RESUME_FAILED_FROM" ] && [ -n "$RESUME" ]; then
  echo "ERROR: use either --resume or --resume-failed-from, not both"
  exit 1
fi

RESUME_SOURCE="${RESUME_FAILED_FROM:-$RESUME}"
RESUME_MANIFEST_PATH=""
if [ -n "$RESUME_SOURCE" ] && [ -f "$RESUME_SOURCE/run-manifest.json" ]; then
  RESUME_MANIFEST_PATH="$RESUME_SOURCE/run-manifest.json"
fi

if [ -z "$PROMPT_FILE_IDXS" ] && [ -n "$RESUME_MANIFEST_PATH" ]; then
  PROMPT_FILE_IDXS="$(python3 - "$RESUME_MANIFEST_PATH" << 'PYEOF'
import json
import sys

try:
    manifest = json.load(open(sys.argv[1]))
except Exception:
    manifest = {}

prompt_idxs = manifest.get("prompt_file_idxs") or []
if isinstance(prompt_idxs, list):
    print(",".join(str(x) for x in prompt_idxs))
PYEOF
)"
fi

if [ -z "$PROMPT_IDS" ] && [ -n "$RESUME_MANIFEST_PATH" ]; then
  PROMPT_IDS="$(python3 - "$RESUME_MANIFEST_PATH" << 'PYEOF'
import json
import sys

try:
    manifest = json.load(open(sys.argv[1]))
except Exception:
    manifest = {}

prompt_ids = manifest.get("prompt_ids") or []
if isinstance(prompt_ids, list):
    print(",".join(str(x) for x in prompt_ids))
PYEOF
)"
fi

if [ -z "$RESUME_CLAUDE_CONFIG_DIR" ] && [ -n "$RESUME_MANIFEST_PATH" ]; then
  RESUME_CLAUDE_CONFIG_DIR="$(python3 - "$RESUME_MANIFEST_PATH" << 'PYEOF'
import json
import sys

try:
    manifest = json.load(open(sys.argv[1]))
except Exception:
    manifest = {}

value = manifest.get("claude_config_dir") or ""
print(value)
PYEOF
)"
fi

if [ "$KEEP_TRANSCRIPTS" = "1" ] || [ "$PERSIST_SESSIONS" = "1" ] || [ -n "$RESUME_FAILED_FROM" ]; then
  SESSION_PERSIST_ARGS=()
else
  SESSION_PERSIST_ARGS=(--no-session-persistence)
fi

# Read prompts from ground_truth.jsonl
GT_FILE="${EVAL_GROUND_TRUTH_FILE:-$SCRIPT_DIR/ground_truth.jsonl}"
if [ ! -f "$GT_FILE" ]; then
  echo "ERROR: $GT_FILE not found"
  exit 1
fi

PROMPTS=()
IDS=()
PIDXS=()   # original index in ground_truth.jsonl — stable across group-filtered runs
# Load prompts via Python (handles group filtering and multiline prompts safely)
while IFS=$'\t' read -r fileidx pid pprompt; do
  PIDXS+=("$fileidx")
  IDS+=("$pid")
  PROMPTS+=("$pprompt")
done < <(EVAL_PROMPT_FILE_IDXS="$PROMPT_FILE_IDXS" EVAL_PROMPT_IDS="$PROMPT_IDS" python3 - "$GT_FILE" "$EVAL_GROUPS" << 'PYEOF'
import json, os, sys

gt_file = sys.argv[1]
groups_arg = sys.argv[2] if len(sys.argv) > 2 else ""
allowed = set(g.strip() for g in groups_arg.split(",") if g.strip()) if groups_arg else set()
# Optional: restrict to specific ground-truth line indices (comma-separated),
# e.g. EVAL_PROMPT_FILE_IDXS=89,101,107 for targeted hard-prompt tests.
idxs_arg = os.environ.get("EVAL_PROMPT_FILE_IDXS", "").strip()
allowed_idxs = set(int(x) for x in idxs_arg.split(",") if x.strip()) if idxs_arg else None
ids_arg = os.environ.get("EVAL_PROMPT_IDS", "").strip()
allowed_ids = set(x.strip() for x in ids_arg.split(",") if x.strip()) if ids_arg else None

with open(gt_file) as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        g = e.get("group", "")
        if allowed and g and g not in allowed:
            continue
        if allowed_idxs is not None and i not in allowed_idxs:
            continue
        # Tab-separated: fileidx \t id \t prompt (prompt may not contain tabs)
        prompt = e["prompt"].replace("\t", " ").replace("\n", " ")
        eid = e["id"]
        if allowed_ids is not None and eid not in allowed_ids:
            continue
        print(f"{i}\t{eid}\t{prompt}")
PYEOF
)

TOTAL=${#PROMPTS[@]}
if [ -n "$LIMIT" ] && [ "$LIMIT" -lt "$TOTAL" ]; then
  TOTAL=$LIMIT
fi

# Isolation configs
mkdir -p "$RESULTS_DIR"
CLEAN_SETTINGS="$RESULTS_DIR/clean-settings.json"
cat > "$CLEAN_SETTINGS" << 'EOF'
{"hooks":{},"enabledPlugins":{}}
EOF

EMPTY_MCP="$RESULTS_DIR/empty-mcp.json"
cat > "$EMPTY_MCP" << 'EOF'
{"mcpServers":{}}
EOF

N8N_MCP_CONFIG="$RESULTS_DIR/n8n-mcp.json"
cat > "$N8N_MCP_CONFIG" << 'EOF'
{"mcpServers":{"n8n-mcp":{"command":"npx","args":["-y","n8n-mcp"]}}}
EOF

# Plugin isolation (all conditions): user-scope INSTALLED plugins load from
# $CLAUDE_CONFIG_DIR/plugins (installed_plugins.json) regardless of --settings,
# so "enabledPlugins":{} above does NOT block them. On this machine that means
# the n8n-local marketplace (symlinked to this live repo) plus hindsight-memory
# inject context into every condition — proven contamination in run
# 20260611-143327-v2 (mcp transcripts carried the plugin's KB injections).
# A scratch config dir guarantees no user-scope plugins load in ANY condition;
# the plugin condition re-adds this repo explicitly via --plugin-dir.
# Auth is seeded by copying credential files by path (never printed). The dir
# lives OUTSIDE the repo tree and is removed on exit so credential copies can
# never be committed.
# Startup sweep: remove scratch dirs stranded by previously hard-killed runs.
# Age-gated (>3h) so a concurrently RUNNING harness's fresh dir is never swept.
find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'n8n-eval-claude-config.*' -type d -mmin +180 -exec rm -rf {} + 2>/dev/null || true
CLAUDE_CONFIG_ROOT="${RESUME_CLAUDE_CONFIG_DIR:-}"
CLAUDE_CONFIG_CREATED=0
CLAUDE_CONFIG_IS_PERSISTENT=0
RESUME_SESSION_STORE_AVAILABLE=0
if [ -n "$CLAUDE_CONFIG_ROOT" ]; then
  mkdir -p "$CLAUDE_CONFIG_ROOT"
  RESUME_SESSION_STORE_AVAILABLE=1
else
  if [ "$PERSIST_SESSIONS" = "1" ] || [ -n "$RESUME_FAILED_FROM" ]; then
    CLAUDE_CONFIG_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/n8n-eval-claude-config-persist.${RESULTS_BASENAME}.XXXXXX")"
    CLAUDE_CONFIG_IS_PERSISTENT=1
  else
    CLAUDE_CONFIG_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/n8n-eval-claude-config.XXXXXX")"
  fi
  CLAUDE_CONFIG_CREATED=1
fi
[ -f "$HOME/.claude.json" ] && [ ! -f "$CLAUDE_CONFIG_ROOT/.claude.json" ] && cp "$HOME/.claude.json" "$CLAUDE_CONFIG_ROOT/.claude.json"
# Symlink (not copy) the credentials: OAuth tokens rotate mid-run, and a stale
# copy 401s every session (observed 2026-06-12: token rotated 1 min after launch,
# all 256 Sonnet sessions died). The scratch-dir cleanup only removes the link.
[ -f "$HOME/.claude/.credentials.json" ] && [ ! -e "$CLAUDE_CONFIG_ROOT/.credentials.json" ] && ln -s "$HOME/.claude/.credentials.json" "$CLAUDE_CONFIG_ROOT/.credentials.json"
export CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_ROOT"
RESUME_PROJECT_ROOT="$(cd "$REPO_DIR" && pwd)"
RESUME_PROJECT_SLUG="$(printf '%s' "$RESUME_PROJECT_ROOT" | tr '/.' '--')"
# Clean up credential copies on EVERY exit path we can trap. SIGKILL cannot be
# trapped — the startup sweep below handles dirs stranded by a hard kill.
cleanup_scratch_config() {
  if [ "$CLAUDE_CONFIG_CREATED" = "1" ] && [ "$CLAUDE_CONFIG_IS_PERSISTENT" != "1" ]; then
    rm -rf "$CLAUDE_CONFIG_ROOT"
  fi
}
trap cleanup_scratch_config EXIT
# On INT/TERM the cleanup runs twice (signal handler + the EXIT trap that fires
# on the handler's own `exit`); rm -rf is idempotent, so the double call is safe.
trap 'cleanup_scratch_config; exit 130' INT
trap 'cleanup_scratch_config; exit 143' TERM

session_transcript_path_for_id() {
  local session_id="$1"
  printf '%s/projects/%s/%s.jsonl\n' "$CLAUDE_CONFIG_DIR" "$RESUME_PROJECT_SLUG" "$session_id"
}

session_transcript_available() {
  local session_id="$1"
  local session_path
  session_path="$(session_transcript_path_for_id "$session_id")"
  [ -f "$session_path" ]
}

reconstruct_resume_transcripts() {
  local source_root="$1"
  python3 - "$source_root" "$CLAUDE_CONFIG_DIR" "$RESUME_PROJECT_SLUG" << 'PYEOF'
import glob
import json
import os
import shutil
import sys

source_root, config_dir, project_slug = sys.argv[1:4]
target_dir = os.path.join(config_dir, "projects", project_slug)
os.makedirs(target_dir, exist_ok=True)

restored = 0

for transcript_path in sorted(
    glob.glob(os.path.join(source_root, "**", "prompt-*-run*.transcript.jsonl"), recursive=True)
):
    base = transcript_path[: -len(".transcript.jsonl")]
    session_id = ""

    for candidate in (base + ".session.json", base + ".json", base + ".meta.json"):
        if not os.path.exists(candidate):
            continue
        try:
            payload = json.load(open(candidate))
        except Exception:
            continue
        session_id = payload.get("session_id") or ""
        if session_id:
            break

    if not session_id:
        try:
            with open(transcript_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    session_id = payload.get("sessionId") or payload.get("session_id") or ""
                    if session_id:
                        break
        except Exception:
            session_id = ""

    if not session_id:
        continue

    target_path = os.path.join(target_dir, session_id + ".jsonl")
    if os.path.exists(target_path):
        try:
            if os.path.getsize(target_path) >= os.path.getsize(transcript_path):
                continue
        except OSError:
            continue
    shutil.copy2(transcript_path, target_path)
    restored += 1

print(restored)
PYEOF
}

if [ -n "$RESUME_FAILED_FROM" ]; then
  RECONSTRUCTED_TRANSCRIPTS_COUNT="$(reconstruct_resume_transcripts "$RESUME_FAILED_FROM")"
  if [ "${RECONSTRUCTED_TRANSCRIPTS_COUNT:-0}" -gt 0 ]; then
    RESUME_SESSION_STORE_AVAILABLE=1
  fi
else
  RECONSTRUCTED_TRANSCRIPTS_COUNT=0
fi

describe_condition_isolation() {
  local cond="$1"
  python3 - "$cond" "$CLEAN_SETTINGS" "$EMPTY_MCP" "$N8N_MCP_CONFIG" "$REPO_DIR" << 'PYEOF'
import json
import os
import sys

cond, clean_settings_path, empty_mcp_path, n8n_mcp_config_path, repo_dir = sys.argv[1:6]

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

clean_settings = load_json(clean_settings_path)
empty_mcp = load_json(empty_mcp_path)
n8n_mcp_config = load_json(n8n_mcp_config_path)

hooks = clean_settings.get("hooks", {})
enabled_plugins = clean_settings.get("enabledPlugins", {})
empty_mcp_servers = empty_mcp.get("mcpServers", {})
n8n_mcp_servers = n8n_mcp_config.get("mcpServers", {})

plugin_validation_enabled = os.environ.get("EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION", "1") == "1"
plugin_validator_mode = os.environ.get("EVAL_PLUGIN_VALIDATOR_MODE", "").strip() or "default"
plugin_validator_cloud = os.environ.get("EVAL_PLUGIN_VALIDATOR_CLOUD_URL", "").strip() or "<unset>"
plugin_validator_local = os.environ.get("EVAL_PLUGIN_VALIDATOR_LOCAL_PATH", "").strip() or "<auto>"

print(f"  [{cond}] isolation summary:")
print(
    f"    settings: hooks={len(hooks)} enabledPlugins={len(enabled_plugins)} "
    f"(from {os.path.basename(clean_settings_path)})"
)

if cond == "plugin":
    print(
        f"    plugin dir: {repo_dir}"
    )
    print(
        f"    mcp config: servers={len(empty_mcp_servers)} "
        f"(from {os.path.basename(empty_mcp_path)})"
    )
    print(
        f"    workflow validation: enabled={plugin_validation_enabled} "
        f"mode={plugin_validator_mode} cloud_url={plugin_validator_cloud} local_path={plugin_validator_local}"
    )
elif cond == "mcp":
    print("    plugin dir: <none>")
    print(
        f"    mcp config: servers={len(n8n_mcp_servers)} names={','.join(sorted(n8n_mcp_servers.keys())) or '<none>'} "
        f"(from {os.path.basename(n8n_mcp_config_path)})"
    )
    print("    workflow validation: plugin-only path disabled for this condition")
else:
    print("    plugin dir: <none>")
    print(
        f"    mcp config: servers={len(empty_mcp_servers)} "
        f"(from {os.path.basename(empty_mcp_path)})"
    )
    print("    workflow validation: disabled for this condition")
PYEOF
}

write_run_manifest() {
  python3 - "$RUN_MANIFEST_PATH" "$RESULTS_DIR" "$RESUME" "$RESUME_FAILED_FROM" "$CLAUDE_CONFIG_DIR" "$MODEL" "$RUNS" "$CONDITIONS" "$EVAL_GROUPS" "$LIMIT" "$PROMPT_FILE_IDXS" "$PROMPT_IDS" "$TOTAL" "$PERSIST_SESSIONS" "$KEEP_TRANSCRIPTS" << 'PYEOF'
import json
import os
import sys

(
    manifest_path,
    results_dir,
    resume_from,
    resume_failed_from,
    claude_config_dir,
    model,
    runs,
    conditions,
    groups,
    limit_value,
    prompt_file_idxs,
    prompt_ids,
    total,
    persist_sessions,
    keep_transcripts,
) = sys.argv[1:16]

idx_values = []
if prompt_file_idxs.strip():
    idx_values = [int(x) for x in prompt_file_idxs.split(",") if x.strip()]
prompt_id_values = [x for x in prompt_ids.split(",") if x.strip()]

manifest = {
    "results_dir": results_dir,
    "resume_from": resume_from or None,
    "resume_failed_from": resume_failed_from or None,
    "claude_config_dir": claude_config_dir or None,
    "model": model,
    "runs": int(runs),
    "conditions": [x.strip() for x in conditions.split(",") if x.strip()],
    "groups": [x.strip() for x in groups.split(",") if x.strip()],
    "limit": int(limit_value) if limit_value else None,
    "prompt_file_idxs": idx_values,
    "prompt_ids": prompt_id_values,
    "prompt_count": int(total),
    "persist_sessions": persist_sessions == "1",
    "keep_transcripts": keep_transcripts == "1",
}

with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
PYEOF
}

copy_resume_artifacts_for_condition() {
  local src_dir="$1" dst_dir="$2" allowed_idxs_csv="${3:-}"
  python3 - "$src_dir" "$dst_dir" "$allowed_idxs_csv" << 'PYEOF'
import os
import re
import shutil
import sys

src_dir, dst_dir, allowed_idxs_csv = sys.argv[1:4]
if not os.path.isdir(src_dir):
    raise SystemExit(0)

os.makedirs(dst_dir, exist_ok=True)

allowed_idxs = None
if allowed_idxs_csv.strip():
    allowed_idxs = {int(x) for x in allowed_idxs_csv.split(",") if x.strip()}

for name in os.listdir(src_dir):
    if not name.startswith("prompt-"):
        continue
    match = re.match(r"prompt-(\d+)-run\d+", name)
    if allowed_idxs is not None and not (match and int(match.group(1)) in allowed_idxs):
        continue
    src = os.path.join(src_dir, name)
    dst = os.path.join(dst_dir, name)
    if os.path.isdir(src):
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
PYEOF
}

SYSTEM="You are helping a user build n8n workflows. Build the complete solution that fully addresses the user's request — include all required nodes and connections. Use full node type names (e.g. n8n-nodes-base.slack). Include typeVersion, position, and all required parameters for each node."

echo "=== PRODUCTION EVAL v2 ==="
echo "  Prompts: $TOTAL (groups: $EVAL_GROUPS)"
echo "  Runs per prompt: $RUNS"
echo "  Conditions: $CONDITIONS"
echo "  Model: $MODEL"
echo "  Ground truth: $GT_FILE"
echo "  Conditions parallel: $CONDITIONS_PARALLEL"
echo "  Repair invalid: $REPAIR_INVALID"
echo "  Repair max attempts: $REPAIR_MAX_ATTEMPTS"
echo "  Condition advance threshold: $CONDITION_ADVANCE_THRESHOLD"
echo "  Max in-flight runs: $MAX_IN_FLIGHT_RUNS"
echo "  Model timeout: ${MODEL_TIMEOUT_SECONDS}s (0=disabled)"
echo "  Batch size: ${BATCH_SIZE} (0=all parallel)"
echo "  Prompt file idxs: ${PROMPT_FILE_IDXS:-<all in selected groups>}"
echo "  Prompt ids: ${PROMPT_IDS:-<all in selected groups>}"
echo "  Plugin validator: mode=${EVAL_PLUGIN_VALIDATOR_MODE:-default} cloud_url=${EVAL_PLUGIN_VALIDATOR_CLOUD_URL:-<unset>} local_path=${EVAL_PLUGIN_VALIDATOR_LOCAL_PATH:-<auto>}"
if [ -n "${EVAL_SCORING_VALIDATOR_MODE:-}" ]; then
  scoring_mode_display="$EVAL_SCORING_VALIDATOR_MODE"
elif [ "${EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION:-1}" = "1" ]; then
  scoring_mode_display="same-as-plugin"
else
  scoring_mode_display="local"
fi
echo "  Scoring validator: mode=${scoring_mode_display} cloud_url=${EVAL_SCORING_VALIDATOR_CLOUD_URL:-<unset>} local_path=${EVAL_SCORING_VALIDATOR_LOCAL_PATH:-<auto>}"
if [ -n "$RESUME" ]; then
  echo "  Resume from: $RESUME"
fi
if [ -n "$RESUME_FAILED_FROM" ]; then
  echo "  Resume failed from: $RESUME_FAILED_FROM"
fi
echo "  Claude config dir: $CLAUDE_CONFIG_DIR"
echo "  Persist sessions: $PERSIST_SESSIONS"
if [ -n "$RESUME_FAILED_FROM" ]; then
  echo "  Reconstructed transcripts: ${RECONSTRUCTED_TRANSCRIPTS_COUNT:-0}"
  if [ "$RESUME_SESSION_STORE_AVAILABLE" != "1" ]; then
    echo "  Resume failed mode: no prior Claude session store detected, so unfinished prompts will rerun fresh"
  fi
fi
echo "  Output: $RESULTS_DIR"
echo ""

if [ "${EVAL_SKIP_VALIDATOR_PREFLIGHT:-0}" != "1" ]; then
  python3 "$SCRIPT_DIR/validator_preflight.py"
  echo ""
fi

write_run_manifest

# Resume: seed output dir with prior results so run_one can skip completed prompts
if [ -n "$RESUME_SOURCE" ]; then
  SELECTED_PROMPT_FILE_IDXS=""
  for ((i=0; i<TOTAL; i++)); do
    if [ -n "$SELECTED_PROMPT_FILE_IDXS" ]; then
      SELECTED_PROMPT_FILE_IDXS+=","
    fi
    SELECTED_PROMPT_FILE_IDXS+="${PIDXS[$i]}"
  done
  echo "Seeding results from $RESUME_SOURCE ..."
  IFS=',' read -ra COND_LIST_TMP <<< "$CONDITIONS"
  for cond in "${COND_LIST_TMP[@]}"; do
    if [ -d "$RESUME_SOURCE/$cond" ]; then
      mkdir -p "$RESULTS_DIR/$cond"
      copy_resume_artifacts_for_condition "$RESUME_SOURCE/$cond" "$RESULTS_DIR/$cond" "$SELECTED_PROMPT_FILE_IDXS"
      echo "  Seeded $(find "$RESULTS_DIR/$cond" -maxdepth 1 -name 'prompt-*' | wc -l | tr -d ' ') artifacts for $cond"
    fi
  done
  echo ""
fi

run_is_successful() {
  local outfile="$1"
  [ -s "$outfile" ] || return 1
  python3 - "$outfile" << 'PYEOF'
import json
import sys

try:
    payload = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)

raise SystemExit(0 if not (payload.get("is_error") or payload.get("error")) else 1)
PYEOF
}

read_session_id_for_outfile() {
  local outfile="$1"
  python3 - "$outfile" << 'PYEOF'
import json
import os
import sys

outfile = sys.argv[1]
base = outfile[:-5] if outfile.endswith(".json") else outfile
for path in (base + ".session.json", outfile):
    if not os.path.exists(path):
        continue
    try:
        payload = json.load(open(path))
    except Exception:
        continue
    sid = payload.get("session_id") or ""
    if sid:
        print(sid)
        raise SystemExit(0)
PYEOF
}

write_session_sidecar() {
  local outfile="$1" cond="$2" idx="$3" run="$4" fileidx="$5" prompt_id="$6" session_id="$7" resume_used="${8:-0}"
  python3 - "$outfile" "$cond" "$idx" "$run" "$fileidx" "$prompt_id" "$session_id" "$resume_used" << 'PYEOF'
import json
import os
import sys

outfile, cond, idx, run, fileidx, prompt_id, session_id, resume_used = sys.argv[1:9]
sidecar = outfile[:-5] + ".session.json" if outfile.endswith(".json") else outfile + ".session.json"
payload = {
    "condition": cond,
    "prompt_array_idx": int(idx),
    "prompt_idx": int(fileidx),
    "prompt_id": prompt_id,
    "run": int(run),
    "session_id": session_id,
    "resume_session_used": resume_used == "1",
}
with open(sidecar, "w") as f:
    json.dump(payload, f)
PYEOF
}

run_command_with_timeout() {
  local timeout_seconds="$1" cwd="$2" outfile="$3" errfile="$4"
  shift 4

  if [ "${timeout_seconds:-0}" -le 0 ]; then
    (
      cd "$cwd"
      "$@"
    ) > "$outfile" 2> "$errfile"
    return $?
  fi

  python3 - "$timeout_seconds" "$cwd" "$outfile" "$errfile" "$@" << 'PYEOF'
import os
import signal
import subprocess
import sys
import time

timeout_seconds = int(sys.argv[1])
cwd = sys.argv[2]
outfile = sys.argv[3]
errfile = sys.argv[4]
command = sys.argv[5:]

with open(outfile, "wb") as stdout_f, open(errfile, "ab") as stderr_f:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=stdout_f,
        stderr=stderr_f,
        start_new_session=True,
    )
    try:
        rc = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        stderr_f.write(
            (
                f"\n[eval-timeout] Model invocation exceeded {timeout_seconds}s; "
                f"terminating pid={proc.pid}\n"
            ).encode("utf-8", errors="replace")
        )
        stderr_f.flush()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
        sys.exit(124)
    else:
        sys.exit(rc)
PYEOF
}

invoke_model() {
  local cond="$1" prompt="$2" outfile="$3" session_id="$4" resume_session="${5:-0}"
  local errfile="${outfile%.json}.stderr.log"
  local wf_dir=""
  local -a plugin_env=()
  local -a plugin_cmd=()
  local -a common_args=()
  local cmd_rc=0
  local effective_prompt="$prompt"
  rm -f "$errfile"

  # File-output mode: when enabled, EVERY condition (bare/plugin/mcp) gets the SAME
  # system prompt with the SAME neutral directive to save the final workflow as a .json
  # file in a deterministic per-run folder. This is symmetric (no condition advantage),
  # realistic (the user must import a file into n8n anyway), and authoritative (system
  # prompt → the model actually writes the file, which engages the plugin's validator
  # loop). Scoring globs this folder for all conditions. It is NOT teaching-to-the-test:
  # it specifies WHERE to put output, not HOW to build a valid workflow.
  local run_system="$SYSTEM"
  if [ "${EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION:-1}" = "1" ]; then
    wf_dir=$(python3 -c "import os,sys;print(os.path.abspath(sys.argv[1]))" "${outfile%.json}.workflow")
    rm -rf "$wf_dir"; mkdir -p "$wf_dir"
    run_system="${SYSTEM}

Save the final workflow as a single .json file inside this folder: $wf_dir
Choose a descriptive filename based on what the workflow does (e.g. slack-post-message.json). Put exactly one workflow file in that folder — this file is the importable deliverable the user will upload to n8n."
  fi

  common_args=(
    --output-format json
    --system-prompt "$run_system"
    --model "$MODEL"
    --disable-slash-commands
    --dangerously-skip-permissions
  )
  if [ "$resume_session" = "1" ]; then
    effective_prompt="${EVAL_RESUME_CONTINUE_PROMPT:-Continue from the current session state and finish the task. Do not restart from scratch. Reuse any existing workflow file/output folder from earlier in this session and return the final answer in the same format as before.}"
    common_args=(--resume "$session_id" "${common_args[@]}")
  else
    common_args=(--session-id "$session_id" "${common_args[@]}")
    if [ "${#SESSION_PERSIST_ARGS[@]}" -gt 0 ]; then
      common_args+=("${SESSION_PERSIST_ARGS[@]}")
    fi
  fi

  case "$cond" in
    bare)
      run_command_with_timeout "$MODEL_TIMEOUT_SECONDS" "$REPO_DIR" "$outfile" "$errfile" \
        "$CLAUDE_BIN" -p "$effective_prompt" \
          "${common_args[@]}" \
          --settings "$CLEAN_SETTINGS" \
          --strict-mcp-config --mcp-config "$EMPTY_MCP" \
          || cmd_rc=$?
      ;;
    plugin)
      # System prompt ($run_system) is IDENTICAL across plugin/mcp/bare — including the
      # neutral "save the final workflow to <folder>" directive. The plugin's ONLY
      # condition-specific behavior comes from its real mechanism: hook-injected recall +
      # the validator loop (enabled via the env below) + generic build guidance from the
      # auto-recall hook. Nothing plugin-specific lives in the prompt → no teaching-to-the-test.
      if [ "${EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION:-1}" = "1" ]; then
        plugin_env+=("CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true")
        plugin_env+=("CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONMAXCALLS=${EVAL_PLUGIN_WORKFLOW_VALIDATION_MAX_CALLS:-10}")
        plugin_env+=("N8N_KNOWLEDGE_AUTOFIX_LOG=${outfile%.json}.autofix.jsonl")
        rm -f "${outfile%.json}.autofix.jsonl"
        [ -n "${EVAL_PLUGIN_VALIDATOR_MODE:-}" ] && plugin_env+=("CLAUDE_PLUGIN_OPTION_VALIDATORMODE=${EVAL_PLUGIN_VALIDATOR_MODE}")
        [ -n "${EVAL_PLUGIN_VALIDATOR_CLOUD_URL:-}" ] && plugin_env+=("CLAUDE_PLUGIN_OPTION_VALIDATORCLOUDURL=${EVAL_PLUGIN_VALIDATOR_CLOUD_URL}")
        [ -n "${EVAL_PLUGIN_VALIDATOR_LOCAL_PATH:-}" ] && plugin_env+=("CLAUDE_PLUGIN_OPTION_VALIDATORLOCALPATH=${EVAL_PLUGIN_VALIDATOR_LOCAL_PATH}")
        [ -n "${EVAL_PLUGIN_WORKFLOW_EDIT_STYLE:-}" ] && plugin_env+=("CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=${EVAL_PLUGIN_WORKFLOW_EDIT_STYLE}")
        [ -n "${EVAL_PLUGIN_WORKFLOW_VALIDATION_BUDGET_MODE:-}" ] && plugin_env+=("CLAUDE_PLUGIN_OPTION_WORKFLOWVALIDATIONBUDGETMODE=${EVAL_PLUGIN_WORKFLOW_VALIDATION_BUDGET_MODE}")
      fi
      [ -n "${MENTAL_MODEL_URL:-}" ] && plugin_env+=("MENTAL_MODEL_URL=${MENTAL_MODEL_URL}")
      [ -n "${N8N_HINDSIGHT_API_KEY:-}" ] && plugin_env+=("N8N_HINDSIGHT_API_KEY=${N8N_HINDSIGHT_API_KEY}")
      [ -n "${NK_MAX_CTX:-}" ] && plugin_env+=("NK_MAX_CTX=${NK_MAX_CTX}")
      plugin_cmd=(
        "$CLAUDE_BIN" -p "$effective_prompt"
        "${common_args[@]}"
        --settings "$CLEAN_SETTINGS"
        --plugin-dir "$REPO_DIR"
        --strict-mcp-config --mcp-config "$EMPTY_MCP"
      )
      if [ "${#plugin_env[@]}" -gt 0 ]; then
        run_command_with_timeout "$MODEL_TIMEOUT_SECONDS" "$REPO_DIR" "$outfile" "$errfile" \
          env "${plugin_env[@]}" "${plugin_cmd[@]}" || cmd_rc=$?
      else
        run_command_with_timeout "$MODEL_TIMEOUT_SECONDS" "$REPO_DIR" "$outfile" "$errfile" \
          "${plugin_cmd[@]}" || cmd_rc=$?
      fi
      ;;
    mcp)
      run_command_with_timeout "$MODEL_TIMEOUT_SECONDS" "$REPO_DIR" "$outfile" "$errfile" \
        "$CLAUDE_BIN" -p "$effective_prompt" \
          "${common_args[@]}" \
          --settings "$CLEAN_SETTINGS" \
          --strict-mcp-config --mcp-config "$N8N_MCP_CONFIG" \
          || cmd_rc=$?
      ;;
  esac

  if [ -s "$outfile" ] && python3 -c "
import json,sys
try:
    json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0)
" "$outfile" 2>/dev/null; then
    # The model's workflow file already lives in the persistent per-run folder
    # ("${outfile%.json}.workflow/"). Nothing to copy — scoring reads it directly.
    [ -s "$errfile" ] || rm -f "$errfile"
    return 0
  fi

  python3 - "$outfile" "$errfile" "${cmd_rc:-0}" "$MODEL_TIMEOUT_SECONDS" << 'PYEOF'
import json
import os
import sys

outfile = sys.argv[1]
errfile = sys.argv[2]
exit_code = int(sys.argv[3])
timeout_seconds = int(sys.argv[4])

stderr_text = ""
if os.path.exists(errfile):
    stderr_text = open(errfile).read()

payload = {
    "error": ("model_invocation_timeout" if exit_code == 124 else "model_invocation_failed"),
    "is_error": True,
    "exit_code": exit_code,
    "timeout_seconds": timeout_seconds if exit_code == 124 else None,
    "stderr_excerpt": stderr_text[:4000],
}

with open(outfile, "w") as f:
    json.dump(payload, f)
PYEOF

  # On model failure, any partial best-attempt the model wrote already persists in
  # the per-run folder ("${outfile%.json}.workflow/"). Nothing to move.
}

write_transport_failure_validation() {
  local outfile="$1"
  python3 - "$outfile" << 'PYEOF'
import json
import sys

outfile = sys.argv[1]
payload = json.load(open(outfile))
stderr_excerpt = payload.get("stderr_excerpt", "")
error_code = payload.get("error", "model_invocation_failed")
timeout_seconds = payload.get("timeout_seconds")
messages = [
    (
        f"Model invocation timed out after {timeout_seconds}s before a workflow response was produced."
        if error_code == "model_invocation_timeout" and timeout_seconds
        else "Model invocation failed before a workflow response was produced."
    ),
]
if stderr_excerpt:
    messages.append(f"CLI stderr: {stderr_excerpt[:500]}")

output = {
    "valid": False,
    "has_json": False,
    "extract_error": error_code,
    "error_count": 1,
    "warning_count": 0,
    "node_count": 0,
    "trigger_count": 0,
    "repair_messages": messages,
    "feedback_block": "\n".join(f"- {msg}" for msg in messages),
}

with open(f"{outfile[:-5]}.validation.json", "w") as f:
    json.dump(output, f)
PYEOF
}

# ═══════════════════════════════════════════════════════════════════════════════
# ⚠️  REPAIR — KNOWN-BROKEN / EVAL-INTEGRITY WARNING  (read before enabling)
# ═══════════════════════════════════════════════════════════════════════════════
# Enabled only when EVAL_REPAIR_INVALID=1 (default 0). When OFF, this function just
# writes the final validation.json once and returns — that path IS clean and is what
# the smoke/benchmark runs use.
#
# When ON, repair is a post-session, PLUGIN-ONLY loop that re-invokes the model up to
# REPAIR_MAX_ATTEMPTS times, feeding validator errors back so it can self-correct.
# There are TWO problems with it right now, and both must be fixed before it can be
# trusted in a published result:
#
#   1. HALF-BROKEN for the file-based flow.
#      The clean pipeline tells the model: "write the workflow to a FILE, do NOT paste
#      JSON into your response." But the repair loop below still:
#        - scrapes the response TEXT for a ```json``` block (validator_feedback.py
#          without --workflow-file), and
#        - builds a repair prompt that instructs the model to "return JSON in a code
#          block" (build_repair_prompt / replace_response_workflow.py).
#      That directly contradicts the file-based instruction. So for the plugin condition
#      it will see no_json_found and nag the model to paste JSON — fighting the very
#      protocol we standardized on. To fix: make the repair loop read/iterate on the
#      model's OUTPUT FILE in the persistent per-run folder ("${outfile%.json}.workflow/",
#      pass --workflow-file), and drop the "paste JSON" language from the repair prompt.
#
#   2. EVAL-INTEGRITY BIAS (plugin vs mcp/bare).
#      Repair only runs for cond=plugin and grants it EXTRA model attempts that the
#      mcp and bare conditions never get. That inflates the plugin's apparent
#      validated-rate — it's no longer a single-shot, apples-to-apples comparison.
#      If we want a "with iteration" benchmark, give the SAME iteration budget to
#      mcp/bare too, and report it as a separate condition ("plugin+repair" vs
#      "mcp+repair"), not folded into the headline plugin number.
#
# NOTE: the in-session PostToolUse validation loop (the plugin hook giving feedback
# during the SAME session) is a DIFFERENT thing and is legitimate real plugin behavior.
# This warning is only about the post-session repair re-invocation below.
# ═══════════════════════════════════════════════════════════════════════════════
repair_if_needed() {
  local cond="$1" prompt="$2" outfile="$3"
  local candidate_file="${outfile%.json}.candidate.workflow.json"
  local validated_file="${outfile%.json}.validated.workflow.json"
  local enrichment_mode="basic"
  if [ "$cond" = "plugin" ]; then
    enrichment_mode="plugin"
  fi
  if python3 -c "
import json,sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if (d.get('error') or d.get('is_error')) else 1)
" "$outfile" 2>/dev/null; then
    write_transport_failure_validation "$outfile"
    return 0
  fi
  if [ "$REPAIR_INVALID" != "1" ] || [ "$cond" != "plugin" ]; then
    # Deliverable: the model's own .json inside the persistent per-run folder. EVERY
    # condition (bare/plugin/mcp) is directed to write there by the shared system prompt,
    # so glob it for all and validate that file directly — authoritative, no response
    # scraping. NOTE: trailing "|| true" is required — with set -e + pipefail, an empty
    # folder makes "ls *.json" fail and would abort run_one before write_meta runs. An
    # empty folder is not an error: we fall back to scraping the response text.
    local wf_file=""
    wf_file=$(ls -t "${outfile%.json}.workflow"/*.json 2>/dev/null | head -1) || true
    local workflow_file_arg=()
    if [ -n "$wf_file" ] && [ -f "$wf_file" ]; then
      workflow_file_arg=(--workflow-file "$wf_file")
    fi
    python3 "$SCRIPT_DIR/validator_feedback.py" \
      --response-file "$outfile" \
      --enrichment-mode "$enrichment_mode" \
      --candidate-file "$candidate_file" \
      --validated-file "$validated_file" \
      "${workflow_file_arg[@]+"${workflow_file_arg[@]}"}" \
      --max-errors 8 > "${outfile%.json}.validation.json" 2>/dev/null || true
    return 0
  fi

  local idx=1
  while [ "$idx" -le "$REPAIR_MAX_ATTEMPTS" ]; do
    local prompt_file feedback_file user_prompt_file patched_candidate_file auto_fix_result_file
    prompt_file="$(mktemp "${TMPDIR:-/tmp}/n8n-repair-prompt.XXXXXX")"
    feedback_file="$(mktemp "${TMPDIR:-/tmp}/n8n-repair-feedback.XXXXXX")"
    user_prompt_file="$(mktemp "${TMPDIR:-/tmp}/n8n-user-prompt.XXXXXX")"
    patched_candidate_file="$(mktemp "${TMPDIR:-/tmp}/n8n-candidate-patched.XXXXXX")"
    auto_fix_result_file="$(mktemp "${TMPDIR:-/tmp}/n8n-auto-fix-result.XXXXXX")"
    printf '%s' "$prompt" > "$user_prompt_file"

    if python3 -c "
import json,sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if (d.get('error') or d.get('is_error')) else 1)
" "$outfile" 2>/dev/null; then
      write_transport_failure_validation "$outfile"
      rm -f "$prompt_file" "$feedback_file" "$user_prompt_file" "$patched_candidate_file" "$auto_fix_result_file"
      return 0
    fi

    python3 "$SCRIPT_DIR/validator_feedback.py" \
      --response-file "$outfile" \
      --enrichment-mode "$enrichment_mode" \
      --original-prompt-file "$user_prompt_file" \
      --repair-prompt-file "$prompt_file" \
      --candidate-file "$candidate_file" \
      --validated-file "$validated_file" \
      --max-errors 8 > "$feedback_file" 2>/dev/null || {
        rm -f "$prompt_file" "$feedback_file" "$user_prompt_file" "$patched_candidate_file" "$auto_fix_result_file"
        return 0
      }

    if python3 -c "
import json,sys
d = json.load(open(sys.argv[1]))
if d.get('valid'):
    sys.exit(0)
sys.exit(1)
" "$feedback_file" 2>/dev/null; then
      mv "$feedback_file" "${outfile%.json}.validation.json"
      rm -f "$prompt_file" "$user_prompt_file" "$patched_candidate_file" "$auto_fix_result_file"
      return 0
    fi

    if [ -f "$candidate_file" ]; then
      python3 "$SCRIPT_DIR/apply_validator_fixes.py" \
        --workflow-file "$candidate_file" \
        --feedback-file "$feedback_file" \
        --output-file "$patched_candidate_file" > "$auto_fix_result_file" 2>/dev/null || true

      if python3 -c "
import json,sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if (d.get('changed') and d.get('valid')) else 1)
" "$auto_fix_result_file" 2>/dev/null; then
        cp "$outfile" "${outfile%.json}.attempt$(printf '%02d' "$idx").json"
        mv "$feedback_file" "${outfile%.json}.attempt$(printf '%02d' "$idx").validation.json"
        python3 "$SCRIPT_DIR/replace_response_workflow.py" "$outfile" "$patched_candidate_file" >/dev/null 2>&1 || true
        cp "$patched_candidate_file" "$candidate_file"
        cp "$patched_candidate_file" "$validated_file"
        rm -f "$prompt_file" "$user_prompt_file" "$patched_candidate_file" "$auto_fix_result_file"
        idx=$((idx + 1))
        continue
      fi
    fi

    cp "$outfile" "${outfile%.json}.attempt$(printf '%02d' "$idx").json"
    invoke_model "$cond" "$(cat "$prompt_file")" "$outfile"
    mv "$feedback_file" "${outfile%.json}.attempt$(printf '%02d' "$idx").validation.json"
    rm -f "$prompt_file" "$user_prompt_file" "$patched_candidate_file" "$auto_fix_result_file"
    idx=$((idx + 1))
  done

  python3 "$SCRIPT_DIR/validator_feedback.py" \
    --response-file "$outfile" \
    --enrichment-mode "$enrichment_mode" \
    --candidate-file "$candidate_file" \
    --validated-file "$validated_file" \
    --max-errors 8 > "${outfile%.json}.validation.json" 2>/dev/null || true
}

write_meta() {
  local cond="$1" idx="$2" run="$3" fileidx="$4" outfile="$5" elapsed="$6" resume_used="${7:-0}"
  python3 -c "
import glob, json, os
try:
    paths = [
        p for p in sorted(glob.glob('${outfile%.json}.attempt*.json'))
        if not p.endswith('.validation.json')
    ] + ['$outfile']
    payloads = []
    for path in paths:
        try:
            payloads.append(json.load(open(path)))
        except Exception:
            pass
    d = payloads[-1] if payloads else {}
    def usage_sum(key):
        total = 0
        for item in payloads:
            total += item.get('usage', {}).get(key, 0) or 0
        return total
    inp = usage_sum('input_tokens')
    cache_c = usage_sum('cache_creation_input_tokens')
    cache_r = usage_sum('cache_read_input_tokens')
    out_tok = usage_sum('output_tokens')
    cost = sum((item.get('total_cost_usd', 0) or 0) for item in payloads)
    turns = sum((item.get('num_turns', 0) or 0) for item in payloads)
    repair_enabled = bool($REPAIR_INVALID) and '$cond' == 'plugin'
    # In-session autofix telemetry: each line in the autofix log is one fire by the
    # plugin's PostToolUse hook. Count fires and total changes so the plugin's
    # validated-rate stays transparent about where wins come from (deterministic
    # autofix vs knowledge injection).
    autofix_fires = 0
    autofix_changes = 0
    try:
        with open('${outfile%.json}.autofix.jsonl') as af:
            for line in af:
                line = line.strip()
                if not line:
                    continue
                try:
                    chs = (json.loads(line).get('changes') or [])
                except Exception:
                    chs = []
                if chs:
                    autofix_fires += 1
                    autofix_changes += len(chs)
    except Exception:
        pass
    # Semantic filename the model chose inside the forced per-run folder (plugin only).
    workflow_filename = None
    try:
        wf_files = sorted(glob.glob('${outfile%.json}.workflow/*.json'))
        if wf_files:
            workflow_filename = os.path.basename(wf_files[-1])
    except Exception:
        pass
    # Actual provider cost: Claude Code prices total_cost_usd at ANTHROPIC rates
    # for the requested model name, even when ANTHROPIC_BASE_URL points at a
    # different provider (~25x overstatement observed vs real DeepSeek billing).
    # When the wrapper exports EVAL_COST_MODEL, reprice from token counts using
    # the provider's published per-1M rates (cache miss = input + cache_creation,
    # cache hit = cache_read; DeepSeek does not bill cache writes separately).
    cost_model = '${EVAL_COST_MODEL:-}'
    cost_actual = None
    PROVIDER_RATES = {
        # per 1M tokens: (input cache miss, input cache hit, output)
        'deepseek-pro':   (0.435, 0.003625, 0.87),
        'deepseek-flash': (0.14,  0.0028,   0.28),
    }
    if cost_model in PROVIDER_RATES:
        miss_rate, hit_rate, out_rate = PROVIDER_RATES[cost_model]
        cost_actual = (
            (inp + cache_c) * miss_rate
            + cache_r * hit_rate
            + out_tok * out_rate
        ) / 1_000_000
    # In-session validator call count (plugin only): the PostToolUse hook tracks
    # calls per session_id in a per-user runtime state file. Read it via the
    # session_id in the response payload so the summary can report validator-budget
    # usage. Path mirrors runtime_dirs.py: <runtime>/state/workflow-validation/<sid>.json
    # where runtime = N8N_KNOWLEDGE_RUNTIME_DIR, else (XDG_CACHE_HOME or ~/.cache)/n8n-knowledge.
    validator_calls = None
    try:
        sid = d.get('session_id', '')
        if sid:
            runtime = (
                os.environ.get('N8N_KNOWLEDGE_RUNTIME_DIR')
                or os.path.join(
                    os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                    'n8n-knowledge')
            )
            state_path = os.path.join(
                runtime, 'state', 'workflow-validation', sid + '.json')
            if os.path.exists(state_path):
                validator_calls = int(json.load(open(state_path)).get('calls', 0))
    except Exception:
        pass
    meta = {
        'condition': '$cond',
        'prompt_idx': ${fileidx:-$idx},
        'run': $run,
        'session_id': d.get('session_id') or None,
        'resume_session_used': bool($resume_used),
        'time_ms': $elapsed,
        'input_tokens': inp,
        'cache_creation': cache_c,
        'cache_read': cache_r,
        'total_input_tokens': inp + cache_c + cache_r,
        'output_tokens': out_tok,
        'cost_usd': cost,
        'num_turns': turns,
        'response_chars': len(d.get('result', '')),
        'is_error': bool(d.get('is_error', False) or d.get('error')),
        'repair_enabled': repair_enabled,
        'repair_max_attempts': ($REPAIR_MAX_ATTEMPTS if repair_enabled else 0),
        'repair_attempts_used': (max(0, len(paths) - 1) if repair_enabled else 0),
        'autofix_fires': autofix_fires,
        'autofix_changes': autofix_changes,
        'workflow_filename': workflow_filename,
        'validator_calls': validator_calls,
        'cost_model': cost_model or None,
        'cost_usd_actual': cost_actual,
    }
    with open('${outfile%.json}.meta.json', 'w') as f:
        json.dump(meta, f)
except Exception as e:
    with open('${outfile%.json}.meta.json', 'w') as f:
        json.dump({'condition':'$cond','prompt_idx':$idx,'run':$run,'time_ms':$elapsed,'error':str(e)}, f)
" 2>/dev/null
}

# When transcripts are kept, move each session's transcript JSONL out of
# <config dir>/projects/<project>/ and into the per-run folder so the full tool
# call / hook / validator history lives next to the run's other artifacts.
# Sessions write transcripts under CLAUDE_CONFIG_DIR (the scratch isolation dir
# above), falling back to ~/.claude for runs without isolation. The transcript
# MUST be rescued before the scratch dir's EXIT trap removes it.
save_transcript() {
  local outfile="$1"
  [ "$KEEP_TRANSCRIPTS" = "1" ] || return 0
  local sid
  sid="$(read_session_id_for_outfile "$outfile" 2>/dev/null || true)"
  [ -n "$sid" ] || return 0
  local resolved_repo project_slug src
  resolved_repo="$(cd "$REPO_DIR" && pwd)"
  project_slug="$(printf '%s' "$resolved_repo" | tr '/.' '--')"
  for config_root in "${CLAUDE_CONFIG_DIR:-}" "$HOME/.claude"; do
    [ -n "$config_root" ] || continue
    src="$config_root/projects/$project_slug/$sid.jsonl"
    if [ -f "$src" ]; then
      mv "$src" "${outfile%.json}.transcript.jsonl" 2>/dev/null || true
      return 0
    fi
  done
}

run_one() {
  local cond="$1" idx="$2" run="$3" prompt="$4" fileidx="$5"
  # fileidx is the prompt's original index in ground_truth.jsonl — used for stable output filenames
  # so results from group a/b/c runs can all live in the same output dir without collisions
  local out_dir="$RESULTS_DIR/$cond"
  mkdir -p "$out_dir"
  local outfile="$out_dir/prompt-$(printf '%03d' "${fileidx:-$idx}")-run$(printf '%02d' "$run").json"
  local prompt_id="${IDS[$idx]}"
  local session_id=""
  local resume_session=0

  # --resume: skip if we already have a valid successful (non-error) output file
  if run_is_successful "$outfile"; then
    echo "  [$cond] p$idx r$run — skipped (resume)"
    return
  fi

  if [ -n "$RESUME_FAILED_FROM" ]; then
    session_id="$(read_session_id_for_outfile "$outfile" 2>/dev/null || true)"
    if [ -n "$session_id" ] && session_transcript_available "$session_id"; then
      resume_session=1
    fi
  fi

  if [ "$resume_session" != "1" ]; then
    rm -f "${outfile%.json}.candidate.workflow.json" "${outfile%.json}.validated.workflow.json" "${outfile%.json}.autofix.jsonl"
    rm -rf "${outfile%.json}.workflow"
  fi

  if [ -z "$session_id" ]; then
    session_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  fi
  write_session_sidecar "$outfile" "$cond" "$idx" "$run" "$fileidx" "$prompt_id" "$session_id" "$resume_session"

  local start_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  invoke_model "$cond" "$prompt" "$outfile" "$session_id" "$resume_session"
  save_transcript "$outfile"
  repair_if_needed "$cond" "$prompt" "$outfile"

  local end_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  local elapsed=$((end_ms - start_ms))

  write_meta "$cond" "$idx" "$run" "$fileidx" "$outfile" "$elapsed" "$resume_session"

  echo "  [$cond] p$idx r$run — ${elapsed}ms"
}

IFS=',' read -ra COND_LIST <<< "$CONDITIONS"

run_condition() {
  local cond="$1"
  echo ""
  echo "=== Condition: $cond (${TOTAL} prompts × ${RUNS} runs) ==="
  describe_condition_isolation "$cond"

  local pids=()
  local batch_num=0
  for ((i=0; i<TOTAL; i++)); do
    for ((r=1; r<=RUNS; r++)); do
      run_one "$cond" "$i" "$r" "${PROMPTS[$i]}" "${PIDXS[$i]}" &
      pids+=($!)

      # Flush batch when we hit batch size
      if [ "$BATCH_SIZE" -gt 0 ] && [ "${#pids[@]}" -ge "$BATCH_SIZE" ]; then
        batch_num=$((batch_num + 1))
        echo "  Batch $batch_num (${#pids[@]} sessions) — waiting..."
        for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
        echo "  Batch $batch_num complete. Pausing ${BATCH_PAUSE}s..."
        sleep "$BATCH_PAUSE"
        pids=()
      fi
    done
  done

  # Wait for any remaining sessions
  if [ "${#pids[@]}" -gt 0 ]; then
    batch_num=$((batch_num + 1))
    echo "  Final batch $batch_num (${#pids[@]} sessions) — waiting..."
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  fi

  echo "  $cond complete."
}

count_condition_completed() {
  local cond="$1"
  local cond_dir_path="$RESULTS_DIR/$cond"
  python3 - "$cond_dir_path" "$REPAIR_INVALID" << 'PYEOF'
import glob
import os
import sys

cond_dir = sys.argv[1]
repair_enabled = sys.argv[2] == "1"

finals = [
    f for f in glob.glob(os.path.join(cond_dir, "prompt-*-run*.json"))
    if not f.endswith(".meta.json")
    and not f.endswith(".validation.json")
    and ".attempt" not in os.path.basename(f)
]

count = 0
for f in finals:
    meta = f[:-5] + ".meta.json"
    if not os.path.exists(meta):
        continue
    if repair_enabled:
        validation = f[:-5] + ".validation.json"
        if not os.path.exists(validation):
            continue
    count += 1

print(count)
PYEOF
}

run_conditions_with_global_limit() {
  local effective_limit="$1"
  # unlock_all=1 → every condition is eligible to dispatch from the start, so conditions
  # interleave (run "in parallel") while total in-flight stays bounded by effective_limit.
  # Default (0) keeps the threshold-gated behavior where the next condition starts only
  # after the previous reaches CONDITION_ADVANCE_THRESHOLD completed runs.
  local unlock_all="${2:-0}"
  local cond_count="${#COND_LIST[@]}"
  local total_runs_for_cond="$((TOTAL * RUNS))"
  local target_threshold="$CONDITION_ADVANCE_THRESHOLD"
  local dispatch_cursor=0
  local -a active_pids=()
  local -a active_cond_indices=()
  local -a cond_started=()
  local -a cond_unlocked=()
  local -a cond_completed=()
  local -a cond_dispatched=()
  local -a cond_announced_complete=()

  if [ "$target_threshold" -gt "$total_runs_for_cond" ]; then
    target_threshold="$total_runs_for_cond"
  fi

  for ((ci=0; ci<cond_count; ci++)); do
    cond_started[$ci]=0
    cond_unlocked[$ci]=0
    cond_completed[$ci]=0
    cond_dispatched[$ci]=0
    cond_announced_complete[$ci]=0
  done
  cond_unlocked[0]=1
  if [ "$unlock_all" = "1" ]; then
    for ((ci=0; ci<cond_count; ci++)); do
      cond_unlocked[$ci]=1
    done
  fi

  dispatch_run_for_condition() {
    local ci="$1"
    local cond="${COND_LIST[$ci]}"
    local seq="${cond_dispatched[$ci]}"
    local prompt_idx=$((seq / RUNS))
    local run_num=$(((seq % RUNS) + 1))
    local fileidx="${PIDXS[$prompt_idx]}"

    if [ "$seq" -ge "$total_runs_for_cond" ]; then
      return 1
    fi

    if [ "${cond_started[$ci]}" -eq 0 ]; then
      echo ""
      echo "=== Condition: $cond (${TOTAL} prompts × ${RUNS} runs) ==="
      describe_condition_isolation "$cond"
      cond_started[$ci]=1
    fi

    run_one "$cond" "$prompt_idx" "$run_num" "${PROMPTS[$prompt_idx]}" "$fileidx" &
    active_pids+=($!)
    active_cond_indices+=($ci)
    cond_dispatched[$ci]=$((seq + 1))
    return 0
  }

  unlock_ready_conditions() {
    local active_count="${#active_pids[@]}"
    for ((ci=1; ci<cond_count; ci++)); do
      if [ "${cond_unlocked[$ci]}" -eq 1 ]; then
        continue
      fi
      local prev=$((ci - 1))
      local prev_name="${COND_LIST[$prev]}"
      local next_name="${COND_LIST[$ci]}"
      if [ "${cond_completed[$prev]}" -ge "$target_threshold" ]; then
        echo "  $prev_name reached ${cond_completed[$prev]} completed runs; starting $next_name ..."
        cond_unlocked[$ci]=1
        continue
      fi
      if [ "${cond_completed[$prev]}" -ge "$total_runs_for_cond" ]; then
        echo "  $prev_name finished before threshold; starting $next_name ..."
        cond_unlocked[$ci]=1
        continue
      fi
      if [ "$effective_limit" -gt 0 ] && [ "${cond_dispatched[$prev]}" -ge "$total_runs_for_cond" ] && [ "$active_count" -lt "$effective_limit" ]; then
        echo "  $prev_name has no more queued runs and only $active_count active sessions remain; starting $next_name ..."
        cond_unlocked[$ci]=1
      fi
    done
  }

  reap_finished_runs() {
    local -a remaining_pids=()
    local -a remaining_cond_indices=()
    for i in "${!active_pids[@]}"; do
      local pid="${active_pids[$i]}"
      local ci="${active_cond_indices[$i]}"
      if kill -0 "$pid" 2>/dev/null; then
        remaining_pids+=("$pid")
        remaining_cond_indices+=("$ci")
        continue
      fi
      wait "$pid" 2>/dev/null || true
      cond_completed[$ci]=$(( ${cond_completed[$ci]} + 1 ))
      if [ "${cond_completed[$ci]}" -ge "$total_runs_for_cond" ] && [ "${cond_announced_complete[$ci]}" -eq 0 ]; then
        echo "  ${COND_LIST[$ci]} complete."
        cond_announced_complete[$ci]=1
      fi
    done
    if [ "${#remaining_pids[@]}" -gt 0 ]; then
      active_pids=("${remaining_pids[@]}")
      active_cond_indices=("${remaining_cond_indices[@]}")
    else
      active_pids=()
      active_cond_indices=()
    fi
  }

  pick_next_condition() {
    local offset candidate
    for ((offset=0; offset<cond_count; offset++)); do
      candidate=$(((dispatch_cursor + offset) % cond_count))
      if [ "${cond_unlocked[$candidate]}" -ne 1 ]; then
        continue
      fi
      if [ "${cond_dispatched[$candidate]}" -ge "$total_runs_for_cond" ]; then
        continue
      fi
      echo "$candidate"
      return 0
    done
    return 1
  }

  while true; do
    reap_finished_runs
    unlock_ready_conditions

    local dispatched_any=0
    while [ "${#active_pids[@]}" -lt "$effective_limit" ]; do
      local next_ci
      next_ci="$(pick_next_condition)" || break
      dispatch_run_for_condition "$next_ci" || break
      dispatch_cursor=$(((next_ci + 1) % cond_count))
      dispatched_any=1
      unlock_ready_conditions
    done

    local all_done=1
    for ((ci=0; ci<cond_count; ci++)); do
      if [ "${cond_completed[$ci]}" -lt "$total_runs_for_cond" ]; then
        all_done=0
        break
      fi
    done
    if [ "$all_done" -eq 1 ]; then
      break
    fi

    if [ "$dispatched_any" -eq 0 ]; then
      sleep 2
    fi
  done
}

GLOBAL_CONCURRENCY_LIMIT="$MAX_IN_FLIGHT_RUNS"
if [ "$BATCH_SIZE" -gt 0 ] && { [ "$GLOBAL_CONCURRENCY_LIMIT" -le 0 ] || [ "$BATCH_SIZE" -lt "$GLOBAL_CONCURRENCY_LIMIT" ]; }; then
  GLOBAL_CONCURRENCY_LIMIT="$BATCH_SIZE"
fi

if [ "$CONDITIONS_PARALLEL" = "1" ]; then
  if [ "$GLOBAL_CONCURRENCY_LIMIT" -gt 0 ]; then
    # Conditions run in parallel (all interleave from the start) BUT total in-flight is
    # held at the global cap. This is the safe meaning of "conditions parallel": e.g.
    # plugin+mcp overlap up to N sessions, never exceeding N. Previously this branch
    # spawned every run of every condition at once, blowing past --max-in-flight-runs
    # (e.g. 2 conditions × 44 prompts = 88 sessions) and overwhelming slower backends.
    echo ""
    echo "=== Running conditions in parallel (global in-flight cap: $GLOBAL_CONCURRENCY_LIMIT) ==="
    run_conditions_with_global_limit "$GLOBAL_CONCURRENCY_LIMIT" 1
  else
    echo ""
    echo "=== WARNING: conditions in parallel with NO in-flight cap ==="
    echo "    (pass --max-in-flight-runs N to bound concurrency; without it ALL sessions launch at once)"
    COND_PIDS=()
    for cond in "${COND_LIST[@]}"; do
      run_condition "$cond" &
      COND_PIDS+=($!)
    done
    for pid in "${COND_PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
  fi
else
  TOTAL_RUNS_FOR_COND=$((TOTAL * RUNS))
  if [ "$CONDITION_ADVANCE_THRESHOLD" -le 0 ]; then
    for ((ci=0; ci<${#COND_LIST[@]}; ci++)); do
      cond="${COND_LIST[$ci]}"
      run_condition "$cond"
      if [ "$ci" -lt $(( ${#COND_LIST[@]} - 1 )) ]; then
        echo "  Pausing 10s before next condition..."
        sleep 10
      fi
    done
  elif [ "$GLOBAL_CONCURRENCY_LIMIT" -gt 0 ]; then
    echo ""
    echo "=== Running with global concurrency limit ($GLOBAL_CONCURRENCY_LIMIT) ==="
    run_conditions_with_global_limit "$GLOBAL_CONCURRENCY_LIMIT"
  else
    TARGET_THRESHOLD="$CONDITION_ADVANCE_THRESHOLD"
    if [ "$TARGET_THRESHOLD" -gt "$TOTAL_RUNS_FOR_COND" ]; then
      TARGET_THRESHOLD="$TOTAL_RUNS_FOR_COND"
    fi

    ACTIVE_PIDS=()
    ACTIVE_CONDS=()

    first_cond="${COND_LIST[0]}"
    run_condition "$first_cond" &
    ACTIVE_PIDS+=($!)
    ACTIVE_CONDS+=("$first_cond")

    for ((ci=1; ci<${#COND_LIST[@]}; ci++)); do
      prev_cond="${COND_LIST[$((ci - 1))]}"
      prev_pid="${ACTIVE_PIDS[$((ci - 1))]}"
      next_cond="${COND_LIST[$ci]}"

      while true; do
        completed_count="$(count_condition_completed "$prev_cond")"
        if [ "$completed_count" -ge "$TARGET_THRESHOLD" ]; then
          echo "  $prev_cond reached $completed_count completed runs; starting $next_cond ..."
          break
        fi
        if ! kill -0 "$prev_pid" 2>/dev/null; then
          echo "  $prev_cond finished before threshold; starting $next_cond ..."
          break
        fi
        sleep 15
      done

      run_condition "$next_cond" &
      ACTIVE_PIDS+=($!)
      ACTIVE_CONDS+=("$next_cond")
    done

    for i in "${!ACTIVE_PIDS[@]}"; do
      wait "${ACTIVE_PIDS[$i]}" 2>/dev/null || true
    done
  fi
fi

echo ""
echo "=== All conditions complete ==="
echo ""

# Aggregate results
python3 "$SCRIPT_DIR/summarize_results.py" "$RESULTS_DIR"

# Auto-ingest into the eval DB so report.py / export_stats.py / the dashboard see
# this run immediately. Idempotent (dedups) and non-fatal — a failed ingest never
# fails the run; re-run manually with: python3 scripts/eval/ingest_runs.py --only <dir>
echo ""
echo "=== Ingesting $RESULTS_BASENAME into eval database ==="
python3 "$SCRIPT_DIR/ingest_runs.py" --only "$RESULTS_BASENAME" \
  || echo "  (auto-ingest failed — non-fatal; run 'python3 scripts/eval/ingest_runs.py --only $RESULTS_BASENAME' manually)"
