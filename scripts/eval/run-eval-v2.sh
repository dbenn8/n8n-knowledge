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
#   bash scripts/eval/run-eval-v2.sh --runs 3           # 3 runs per prompt
#   bash scripts/eval/run-eval-v2.sh --conditions plugin,mcp  # skip bare
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/../.."
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_DIR="$REPO_DIR/out/eval/$TIMESTAMP-v2"

# Defaults
LIMIT=""
RUNS=5
CONDITIONS="plugin,mcp,bare"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --conditions) CONDITIONS="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Read prompts from ground_truth.jsonl
GT_FILE="$SCRIPT_DIR/ground_truth.jsonl"
if [ ! -f "$GT_FILE" ]; then
  echo "ERROR: $GT_FILE not found"
  exit 1
fi

PROMPTS=()
IDS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  prompt=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin)['prompt'])")
  id=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  PROMPTS+=("$prompt")
  IDS+=("$id")
done < "$GT_FILE"

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

SYSTEM="You are helping a user build n8n workflows. Answer their question about n8n nodes, configuration, and wiring. Be specific about which nodes to use, what fields to configure, and any gotchas or known issues.

IMPORTANT: Always end your response with the complete, importable n8n workflow JSON inside a \`\`\`json code block. This JSON must be a valid n8n workflow object with 'nodes' array and 'connections' object that the user can directly paste into n8n's workflow import dialog. Use full node type names (e.g. 'n8n-nodes-base.slack', not just 'Slack'). Include typeVersion, position, and all required parameters for each node."

echo "=== PRODUCTION EVAL v2 ==="
echo "  Prompts: $TOTAL"
echo "  Runs per prompt: $RUNS"
echo "  Conditions: $CONDITIONS"
echo "  Output: $RESULTS_DIR"
echo ""

run_one() {
  local cond="$1" idx="$2" run="$3" prompt="$4"
  local dir="$RESULTS_DIR/$cond"
  mkdir -p "$dir"
  local outfile="$dir/prompt-$(printf '%03d' "$idx")-run$(printf '%02d' "$run").json"
  local start_ms=$(python3 -c "import time; print(int(time.time()*1000))")

  case "$cond" in
    bare)
      claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
        --settings "$CLEAN_SETTINGS" \
        --strict-mcp-config --mcp-config "$EMPTY_MCP" \
        --disable-slash-commands \
        --no-session-persistence \
        --dangerously-skip-permissions \
        > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
      ;;
    plugin)
      claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
        --settings "$CLEAN_SETTINGS" \
        --plugin-dir "$REPO_DIR" \
        --strict-mcp-config --mcp-config "$EMPTY_MCP" \
        --disable-slash-commands \
        --no-session-persistence \
        --dangerously-skip-permissions \
        > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
      ;;
    mcp)
      claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
        --settings "$CLEAN_SETTINGS" \
        --strict-mcp-config --mcp-config "$N8N_MCP_CONFIG" \
        --disable-slash-commands \
        --no-session-persistence \
        --dangerously-skip-permissions \
        > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
      ;;
  esac

  local end_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  local elapsed=$((end_ms - start_ms))

  # Write metadata
  python3 -c "
import json
try:
    d = json.load(open('$outfile'))
    u = d.get('usage', {})
    meta = {
        'condition': '$cond',
        'prompt_idx': $idx,
        'run': $run,
        'time_ms': $elapsed,
        'input_tokens': u.get('input_tokens', 0),
        'cache_creation': u.get('cache_creation_input_tokens', 0),
        'cache_read': u.get('cache_read_input_tokens', 0),
        'output_tokens': u.get('output_tokens', 0),
        'cost_usd': d.get('total_cost_usd', 0),
        'num_turns': d.get('num_turns', 0),
        'response_chars': len(d.get('result', '')),
        'is_error': d.get('is_error', False),
    }
    with open('${outfile%.json}.meta.json', 'w') as f:
        json.dump(meta, f)
except Exception as e:
    with open('${outfile%.json}.meta.json', 'w') as f:
        json.dump({'condition':'$cond','prompt_idx':$idx,'run':$run,'time_ms':$elapsed,'error':str(e)}, f)
" 2>/dev/null

  echo "  [$cond] p$idx r$run — ${elapsed}ms"
}

# Run conditions sequentially, prompts parallel within each
IFS=',' read -ra COND_LIST <<< "$CONDITIONS"

for cond in "${COND_LIST[@]}"; do
  echo ""
  echo "=== Condition: $cond (${TOTAL} prompts × ${RUNS} runs) ==="

  PIDS=()
  for ((i=0; i<TOTAL; i++)); do
    for ((r=1; r<=RUNS; r++)); do
      run_one "$cond" "$i" "$r" "${PROMPTS[$i]}" &
      PIDS+=($!)
    done
  done

  echo "  Launched ${#PIDS[@]} sessions for $cond. Waiting..."
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
  echo "  $cond complete."
  echo "  Pausing 10s before next condition..."
  sleep 10
done

echo ""
echo "=== All conditions complete ==="
echo ""

# Aggregate results
RESULTS_DIR_EXPORT="$RESULTS_DIR" python3 << 'PYEOF'
import json, os, glob, re
from collections import defaultdict

results_dir = os.environ.get("RESULTS_DIR_EXPORT", "")
if not results_dir:
    print("ERROR: RESULTS_DIR_EXPORT not set")
    import sys; sys.exit(1)

# Load all meta files
data = defaultdict(lambda: defaultdict(list))
for meta_file in sorted(glob.glob(os.path.join(results_dir, "*", "*.meta.json"))):
    try:
        m = json.load(open(meta_file))
        cond = m.get("condition", "?")
        idx = m.get("prompt_idx", -1)
        data[cond][idx].append(m)
    except:
        pass

if not data:
    print("No results found")
    sys.exit(1)

conditions = sorted(data.keys())
n_prompts = max(max(d.keys()) for d in data.values()) + 1

# Compute per-condition aggregates
print(f"{'METRIC':<25}", end="")
for c in conditions:
    print(f" {c:>14}", end="")
print()
print("-" * (25 + 15 * len(conditions)))

metrics = [
    ("Avg cost ($)", "cost_usd"),
    ("Avg time (ms)", "time_ms"),
    ("Avg turns", "num_turns"),
    ("Avg output tokens", "output_tokens"),
    ("Avg response (chars)", "response_chars"),
    ("Error rate", "is_error"),
]

for label, key in metrics:
    print(f"{label:<25}", end="")
    for c in conditions:
        all_vals = [m[key] for runs in data[c].values() for m in runs if key in m]
        if key == "is_error":
            val = sum(1 for v in all_vals if v) / max(len(all_vals), 1) * 100
            print(f" {val:>13.1f}%", end="")
        elif key == "cost_usd":
            val = sum(all_vals) / max(len(all_vals), 1)
            print(f" ${val:>13.3f}", end="")
        elif key == "time_ms":
            val = sum(all_vals) / max(len(all_vals), 1)
            print(f" {val:>12.0f}ms", end="")
        else:
            val = sum(all_vals) / max(len(all_vals), 1)
            print(f" {val:>14.1f}", end="")
    print()

# Total runs and cost
print("-" * (25 + 15 * len(conditions)))
print(f"{'Total runs':<25}", end="")
for c in conditions:
    total = sum(len(runs) for runs in data[c].values())
    print(f" {total:>14}", end="")
print()
print(f"{'Total cost ($)':<25}", end="")
for c in conditions:
    total = sum(m["cost_usd"] for runs in data[c].values() for m in runs if "cost_usd" in m)
    print(f" ${total:>13.2f}", end="")
print()

print(f"\nResults: {results_dir}")
PYEOF

