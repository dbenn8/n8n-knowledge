#!/usr/bin/env bash
# Head-to-head eval: n8n-knowledge plugin vs n8n-mcp
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/../.."
OUT_DIR="$REPO_DIR/out/eval"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_DIR="$OUT_DIR/$TIMESTAMP"
mkdir -p "$RESULTS_DIR/plugin" "$RESULTS_DIR/mcp"

LIMIT_N="${1:-20}"

PROMPTS=(
  "hey how do I get slack to message my team when a new row shows up in sheets"
  "i need to send an email through gmail whenever a webhook fires, how do I wire that up"
  "whats the best way to create a jira ticket from n8n when something breaks"
  "can you help me set up a postgres query that runs on a schedule"
  "I want to upload files to google drive from my workflow, which node do I use"
  "how do I configure the sentry node to create releases"
  "need to send telegram messages from my bot when certain events happen"
  "I want to read data from airtable and transform it before sending to slack"
  "how do I set up discord notifications when my workflow detects an error"
  "can I use hubspot to automatically create deals from webhook data"
  "whats the right way to use the HTTP request node to call an external REST API"
  "I need a webhook endpoint that receives JSON and processes it"
  "how do I merge data from two different branches in my workflow"
  "can you show me how to use the code node to transform JSON data"
  "I want to listen for new gmail messages and create notion pages from them"
  "configure microsoft teams to send a message when a deployment finishes"
  "how do I set up salesforce to sync contacts with my database"
  "I need to split a big list into batches and process them one at a time"
  "what node should I use to filter items based on a condition"
  "how do I handle errors in my n8n workflow so it doesnt just stop"
)

MCP_CONFIG="$RESULTS_DIR/mcp-config.json"
cat > "$MCP_CONFIG" << 'EOF'
{"mcpServers":{"n8n-mcp":{"command":"npx","args":["-y","n8n-mcp"]}}}
EOF

PROMPT_COUNT=$LIMIT_N
if [ "$PROMPT_COUNT" -gt "${#PROMPTS[@]}" ]; then
  PROMPT_COUNT=${#PROMPTS[@]}
fi

echo "=== n8n-knowledge Eval Harness ==="
echo "Prompts: $PROMPT_COUNT"
echo "Output: $RESULTS_DIR"
echo ""

SYSTEM="You are helping a user build n8n workflows. Answer their question about n8n nodes, configuration, and wiring. Be specific about which nodes to use, what fields to configure, and any gotchas or known issues. Keep it concise."

echo "condition|idx|time_ms|input_tokens|cache_create|cache_read|output_tokens|cost_usd|turns|response_chars" > "$RESULTS_DIR/metrics.csv"

run_one() {
  local cond="$1" idx="$2" prompt="$3"
  local outfile="$RESULTS_DIR/$cond/prompt-$(printf '%02d' "$idx").json"
  local start_ms=$(python3 -c "import time; print(int(time.time()*1000))")

  if [ "$cond" = "plugin" ]; then
    claude -p "$prompt" \
      --output-format json \
      --system-prompt "$SYSTEM" \
      --plugin-dir "$REPO_DIR" \
      --no-session-persistence \
      --dangerously-skip-permissions \
      > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
  else
    claude -p "$prompt" \
      --output-format json \
      --system-prompt "$SYSTEM" \
      --strict-mcp-config --mcp-config "$MCP_CONFIG" \
      --disable-slash-commands \
      --no-session-persistence \
      --dangerously-skip-permissions \
      > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
  fi

  local end_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  local elapsed=$((end_ms - start_ms))

  python3 -c "
import json
try:
    d = json.load(open('$outfile'))
    u = d.get('usage', {})
    inp = u.get('input_tokens', 0)
    cc = u.get('cache_creation_input_tokens', 0)
    cr = u.get('cache_read_input_tokens', 0)
    out = u.get('output_tokens', 0)
    cost = d.get('total_cost_usd', 0)
    turns = d.get('num_turns', 0)
    rlen = len(d.get('result', ''))
    print(f'$cond|$idx|$elapsed|{inp}|{cc}|{cr}|{out}|{cost:.4f}|{turns}|{rlen}')
except:
    print(f'$cond|$idx|$elapsed|0|0|0|0|0|0|0')
" >> "$RESULTS_DIR/metrics.csv"

  echo "  [$cond] #$idx — ${elapsed}ms"
}

for ((i=0; i<PROMPT_COUNT; i++)); do
  prompt="${PROMPTS[$i]}"
  echo ""
  echo "--- $((i+1))/$PROMPT_COUNT: ${prompt:0:65}..."

  run_one "plugin" "$i" "$prompt" &
  P_PID=$!
  run_one "mcp" "$i" "$prompt" &
  M_PID=$!
  wait $P_PID $M_PID
done

echo ""
echo "=== Report ==="
echo ""

python3 << PYEOF
import csv, os

metrics = {"plugin": [], "mcp": []}
with open("$RESULTS_DIR/metrics.csv") as f:
    reader = csv.DictReader(f, delimiter="|")
    for row in reader:
        cond = row["condition"]
        metrics[cond].append({
            "idx": int(row["idx"]),
            "time_ms": int(row["time_ms"]),
            "input": int(row["input_tokens"]),
            "cache_create": int(row["cache_create"]),
            "cache_read": int(row["cache_read"]),
            "output": int(row["output_tokens"]),
            "cost": float(row["cost_usd"]),
            "turns": int(row["turns"]),
            "chars": int(row["response_chars"]),
        })

n = len(metrics["plugin"])
if n == 0:
    print("No results"); exit()

def avg(lst, key): return sum(m[key] for m in lst) / n
def total(lst, key): return sum(m[key] for m in lst)

hdr = f"{'Metric':<30} {'Plugin':>12} {'n8n-mcp':>12} {'Delta':>12}"
sep = "-" * 68
print(hdr)
print(sep)

pairs = [
    ("Avg time (ms)", "time_ms", "ms"),
    ("Avg input tokens", "input", ""),
    ("Avg cache create", "cache_create", ""),
    ("Avg cache read", "cache_read", ""),
    ("Avg output tokens", "output", ""),
    ("Avg cost (USD)", "cost", ""),
    ("Avg turns", "turns", ""),
    ("Avg response (chars)", "chars", ""),
]

for label, key, suffix in pairs:
    p = avg(metrics["plugin"], key)
    m = avg(metrics["mcp"], key)
    d = p - m
    if key == "cost":
        print(f"{label:<30} \${p:>11.4f} \${m:>11.4f} \${d:>+11.4f}")
    elif suffix:
        print(f"{label:<30} {p:>11.0f}{suffix} {m:>11.0f}{suffix} {d:>+11.0f}{suffix}")
    else:
        print(f"{label:<30} {p:>12.0f} {m:>12.0f} {d:>+12.0f}")

print(sep)
print(f"{'Total cost':<30} \${total(metrics['plugin'],'cost'):>11.4f} \${total(metrics['mcp'],'cost'):>11.4f}")
print(f"{'Total prompts':<30} {n:>12}")

# Per-prompt detail
print()
print(f"{'#':<4} {'Plugin ms':>10} {'MCP ms':>10} {'P-tok':>8} {'M-tok':>8} {'P-cost':>8} {'M-cost':>8} {'P-turns':>8} {'M-turns':>8}")
print("-" * 80)
for i in range(n):
    p = metrics["plugin"][i]
    m = metrics["mcp"][i]
    pt = p["input"] + p["cache_create"] + p["cache_read"] + p["output"]
    mt = m["input"] + m["cache_create"] + m["cache_read"] + m["output"]
    print(f"{i+1:<4} {p['time_ms']:>9}ms {m['time_ms']:>9}ms {pt:>8} {mt:>8} \${p['cost']:>7.3f} \${m['cost']:>7.3f} {p['turns']:>8} {m['turns']:>8}")

# Save report
report_path = "$RESULTS_DIR/report.txt"
with open(report_path, "w") as f:
    f.write(f"Eval results saved to {report_path}\n")
print(f"\nResults: $RESULTS_DIR")
PYEOF
