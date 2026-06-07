#!/usr/bin/env bash
# ISOLATED eval: Plugin vs n8n-mcp — no Hindsight memories, no global hooks, no cross-contamination.
# Both conditions use --settings to suppress global hooks and --strict-mcp-config for MCP isolation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/../.."
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RESULTS_DIR="$REPO_DIR/out/eval/$TIMESTAMP-isolated"
mkdir -p "$RESULTS_DIR/plugin" "$RESULTS_DIR/mcp"

# Isolation configs — no global hooks, no Hindsight, no cross-talk
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

SYSTEM="You are helping a user build n8n workflows. Answer their question about n8n nodes, configuration, and wiring. Be specific about which nodes to use, what fields to configure, and any gotchas or known issues. Keep it concise."

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
    # Plugin: our hooks fire, but NO global hooks, NO Hindsight, NO other plugins
    claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
      --settings "$CLEAN_SETTINGS" \
      --plugin-dir "$REPO_DIR" \
      --strict-mcp-config --mcp-config "$EMPTY_MCP" \
      --disable-slash-commands \
      --no-session-persistence \
      --dangerously-skip-permissions \
      > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
  else
    # MCP: n8n-mcp tools only, NO global hooks, NO Hindsight, NO plugins
    claude -p "$prompt" --output-format json --system-prompt "$SYSTEM" \
      --settings "$CLEAN_SETTINGS" \
      --strict-mcp-config --mcp-config "$N8N_MCP_CONFIG" \
      --disable-slash-commands \
      --no-session-persistence \
      --dangerously-skip-permissions \
      > "$outfile" 2>/dev/null || echo '{"error":"failed"}' > "$outfile"
  fi

  local end_ms=$(python3 -c "import time; print(int(time.time()*1000))")
  echo "$((end_ms - start_ms))" > "$metafile"
  echo "  [$cond] #$idx done ($((end_ms - start_ms))ms)"
}

echo "=== ISOLATED Eval: Plugin vs n8n-mcp ==="
echo "  Prompts: $LIMIT"
echo "  Isolation: --settings (no hooks) + --strict-mcp-config"
echo "  Output: $RESULTS_DIR"
echo ""

PIDS=()
for ((i=0; i<LIMIT; i++)); do
  run_one "plugin" "$i" "${PROMPTS[$i]}" &
  PIDS+=($!)
  run_one "mcp" "$i" "${PROMPTS[$i]}" &
  PIDS+=($!)
done
echo "Launched ${#PIDS[@]} isolated sessions..."
for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done

echo ""
echo "=== Report ==="

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
    except: return {"result":"","num_turns":0,"total_cost_usd":0}, 0
    return d, elapsed

print(f"{'#':<3} {'Prompt':<16} {'P-node':>6} {'M-node':>6} {'P-cite':>6} {'M-cite':>6} {'P-trn':>5} {'M-trn':>5} {'P-ms':>8} {'M-ms':>8} {'P-\$':>7} {'M-\$':>7}")
print("=" * 105)
pn=0;mn=0;pg=0;mg=0;pc=0;mc=0;pe_s=0;me_s=0;pt_s=0;mt_s=0
for i in range(limit):
    pd, pe = load("plugin", i); md, me = load("mcp", i)
    pr = pd.get("result","").lower(); mr = md.get("result","").lower()
    exp = expected[i]
    p_hit = any(e in pr for e in exp); m_hit = any(e in mr for e in exp)
    if p_hit: pn+=1
    if m_hit: mn+=1
    p_cit = sum(1 for p in real_cite if re.search(p, pd.get("result",""))); m_cit = sum(1 for p in real_cite if re.search(p, md.get("result","")))
    if p_cit>0: pg+=1
    if m_cit>0: mg+=1
    p_t=pd.get("num_turns",0); m_t=md.get("num_turns",0)
    p_c=pd.get("total_cost_usd",0); m_c=md.get("total_cost_usd",0)
    pc+=p_c;mc+=m_c;pe_s+=pe;me_s+=me;pt_s+=p_t;mt_s+=m_t
    print(f"{i+1:<3} {prompts_short[i]:<16} {'Y' if p_hit else 'N':>6} {'Y' if m_hit else 'N':>6} {p_cit:>6} {m_cit:>6} {p_t:>5} {m_t:>5} {pe:>7}ms {me:>7}ms \${p_c:>6.3f} \${m_c:>6.3f}")
print("=" * 105)
print(f"\n{'METRIC':<30} {'Plugin':>12} {'n8n-mcp':>12} {'Delta':>12}")
print("-" * 68)
print(f"{'Node accuracy':<30} {pn}/{limit} ({pn*100//limit}%) {mn}/{limit} ({mn*100//limit}%) {(pn-mn)*100//limit:>+11}%")
print(f"{'Real citations':<30} {pg}/{limit} {mg}/{limit} {pg-mg:>+12}")
print(f"{'Avg turns':<30} {pt_s/limit:>12.1f} {mt_s/limit:>12.1f} {(pt_s-mt_s)/limit:>+12.1f}")
print(f"{'Avg time (ms)':<30} {pe_s/limit:>11.0f}ms {me_s/limit:>11.0f}ms {(pe_s-me_s)/limit:>+11.0f}ms")
print(f"{'Avg cost':<30} \${pc/limit:>11.3f} \${mc/limit:>11.3f} \${(pc-mc)/limit:>+11.3f}")
print(f"{'Total cost':<30} \${pc:>11.2f} \${mc:>11.2f} \${pc-mc:>+11.2f}")
PYEOF
