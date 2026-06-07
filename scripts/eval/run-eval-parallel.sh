#!/usr/bin/env bash
# Fully parallel eval: all 40 sessions (20 plugin + 20 MCP) launch simultaneously
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/../.."
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_DIR="$REPO_DIR/out/eval/$TIMESTAMP"
mkdir -p "$RESULTS_DIR/plugin" "$RESULTS_DIR/mcp"

SYSTEM="You are helping a user build n8n workflows. Answer their question about n8n nodes, configuration, and wiring. Be specific about which nodes to use, what fields to configure, and any gotchas or known issues. Keep it concise."

MCP_CONFIG="$RESULTS_DIR/mcp-config.json"
cat > "$MCP_CONFIG" << 'EOF'
{"mcpServers":{"n8n-mcp":{"command":"npx","args":["-y","n8n-mcp"]}}}
EOF

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

LIMIT="${1:-${#PROMPTS[@]}}"

run_one() {
  local cond="$1" idx="$2" prompt="$3"
  local outfile="$RESULTS_DIR/$cond/prompt-$(printf '%02d' "$idx").json"
  local metafile="$RESULTS_DIR/$cond/meta-$(printf '%02d' "$idx").txt"
  local start_ms=$(python3 -c "import time; print(int(time.time()*1000))")

  if [ "$cond" = "plugin" ]; then
    claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
      --plugin-dir "$REPO_DIR" --no-session-persistence --dangerously-skip-permissions \
      > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
  else
    claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
      --strict-mcp-config --mcp-config "$MCP_CONFIG" --disable-slash-commands \
      --no-session-persistence --dangerously-skip-permissions \
      > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
  fi

  local end_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  local elapsed=$((end_ms - start_ms))
  echo "$elapsed" > "$metafile"
  echo "  [$cond] #$idx done (${elapsed}ms)"
}

echo "=== Eval: Fixed Plugin vs n8n-mcp — $LIMIT prompts, fully parallel ==="
echo "Output: $RESULTS_DIR"
echo ""

# Launch ALL sessions simultaneously
PIDS=()
for ((i=0; i<LIMIT; i++)); do
  run_one "plugin" "$i" "${PROMPTS[$i]}" &
  PIDS+=($!)
  run_one "mcp" "$i" "${PROMPTS[$i]}" &
  PIDS+=($!)
done

echo "Launched ${#PIDS[@]} sessions. Waiting..."
echo ""

# Wait for all
for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

echo ""
echo "=== All done. Generating report... ==="
echo ""

# Generate report from individual files (no CSV race condition)
python3 << PYEOF
import json, os, re

results_dir = "$RESULTS_DIR"
limit = $LIMIT

expected = [
  ["slack", "sheet"], ["gmail", "webhook"], ["jira"], ["postgres"],
  ["google drive", "drive"], ["sentry"], ["telegram"], ["airtable", "slack"],
  ["discord"], ["hubspot"], ["http request", "httprequest"], ["webhook"],
  ["merge"], ["code"], ["gmail", "notion"], ["teams", "microsoft teams"],
  ["salesforce"], ["batch", "split", "loop"], ["filter", "if node"], ["error"],
]

prompts_short = [
  "slack+sheets", "gmail+webhook", "jira ticket", "postgres sched",
  "gdrive upload", "sentry release", "telegram msg", "airtable→slack",
  "discord notif", "hubspot deals", "HTTP request", "webhook endpt",
  "merge branch", "code node", "gmail→notion", "ms teams msg",
  "salesforce", "split batches", "filter/cond", "error handling",
]

real_cite = [r"#\d{4,}", r"github\.com/n8n", r"community\.n8n\.io", r"CLOSED", r"\[OPEN\]", r"not_planned"]

def load(cond, idx):
    fp = os.path.join(results_dir, cond, f"prompt-{idx:02d}.json")
    mp = os.path.join(results_dir, cond, f"meta-{idx:02d}.txt")
    try:
        d = json.load(open(fp))
        elapsed = int(open(mp).read().strip()) if os.path.exists(mp) else 0
    except:
        return {"result":"","num_turns":0,"total_cost_usd":0,"usage":{}}, 0
    return d, elapsed

print(f"{'#':<3} {'Prompt':<16} {'P-node':>6} {'M-node':>6} {'P-cite':>6} {'M-cite':>6} {'P-trn':>5} {'M-trn':>5} {'P-ms':>8} {'M-ms':>8} {'P-\$':>7} {'M-\$':>7}")
print("=" * 105)

pn=0;mn=0;pg=0;mg=0;pc=0;mc=0;pt_sum=0;mt_sum=0;pe_sum=0;me_sum=0

for i in range(limit):
    pd, pe = load("plugin", i)
    md, me = load("mcp", i)
    pr = pd.get("result","").lower()
    mr = md.get("result","").lower()
    
    exp = expected[i]
    p_hit = any(e in pr for e in exp)
    m_hit = any(e in mr for e in exp)
    if p_hit: pn+=1
    if m_hit: mn+=1
    
    p_cit = sum(1 for p in real_cite if re.search(p, pd.get("result","")))
    m_cit = sum(1 for p in real_cite if re.search(p, md.get("result","")))
    if p_cit>0: pg+=1
    if m_cit>0: mg+=1
    
    p_trn = pd.get("num_turns",0); m_trn = md.get("num_turns",0)
    p_cost = pd.get("total_cost_usd",0); m_cost = md.get("total_cost_usd",0)
    pc+=p_cost; mc+=m_cost; pt_sum+=p_trn; mt_sum+=m_trn; pe_sum+=pe; me_sum+=me
    
    pns="Y" if p_hit else "N"; mns="Y" if m_hit else "N"
    print(f"{i+1:<3} {prompts_short[i]:<16} {pns:>6} {mns:>6} {p_cit:>6} {m_cit:>6} {p_trn:>5} {m_trn:>5} {pe:>7}ms {me:>7}ms \${p_cost:>6.3f} \${m_cost:>6.3f}")

print("=" * 105)
print()
print(f"{'METRIC':<30} {'Plugin':>12} {'n8n-mcp':>12} {'Delta':>12}")
print("-" * 68)
print(f"{'Node accuracy':<30} {pn}/{limit} ({pn*100//limit}%) {mn}/{limit} ({mn*100//limit}%) {(pn-mn)*100//limit:>+11}%")
print(f"{'Real citations':<30} {pg}/{limit} {mg}/{limit} {pg-mg:>+12}")
print(f"{'Avg turns':<30} {pt_sum/limit:>12.1f} {mt_sum/limit:>12.1f} {(pt_sum-mt_sum)/limit:>+12.1f}")
print(f"{'Avg time (ms)':<30} {pe_sum/limit:>11.0f}ms {me_sum/limit:>11.0f}ms {(pe_sum-me_sum)/limit:>+11.0f}ms")
print(f"{'Avg cost':<30} \${pc/limit:>11.3f} \${mc/limit:>11.3f} \${(pc-mc)/limit:>+11.3f}")
print(f"{'Total cost':<30} \${pc:>11.2f} \${mc:>11.2f} \${pc-mc:>+11.2f}")

# Save
with open(os.path.join(results_dir, "report.txt"), "w") as f:
    f.write(f"Results: {results_dir}\n")
print(f"\nResults: {results_dir}")
PYEOF
