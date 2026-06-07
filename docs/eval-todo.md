# Eval Harness — Remaining Work

## Status as of June 7, 2026 (end of session)

### Completed
- [x] 3-recall architecture (semantic + node specs capped at 5 + gotcha recall)
- [x] Gotchas first in merge order, node specs last
- [x] Citation format instruction with few-shot example and engagement metrics
- [x] Design-around-bugs instruction in format_results.py header
- [x] Skill converted to /n8n-knowledge with disable-model-invocation: true
- [x] Fuzzy matching for node name typos (difflib, 0.85 cutoff, common word stoplist)
- [x] Eval isolation via --settings (no Hindsight memory leakage)
- [x] Eval harness v2: reads from ground_truth.jsonl, 3 conditions, N runs, parallel
- [x] Bootstrap CI analysis script (scripts/eval/analyze.py)
- [x] 48 prompts in ground_truth.jsonl (20 config + 3 typo + 2 negative + 8 gotcha-build + 5 simple + 6 medium + 9 complex)

### In Progress
- [ ] **60+ more prompts** — agent dispatched, writing to scripts/eval/additional_prompts.jsonl. When done: `cat scripts/eval/additional_prompts.jsonl >> scripts/eval/ground_truth.jsonl` to merge.

### Remaining Tasks

#### 1. Merge prompts to 100+ (Task 15)
- Agent output → additional_prompts.jsonl
- Merge: `cat additional_prompts.jsonl >> ground_truth.jsonl`
- Verify: `wc -l ground_truth.jsonl` should be 100+
- Sync cache: `rsync -av hooks/ ~/.claude/plugins/cache/n8n-local/n8n-knowledge/0.3.6/hooks/`

#### 2. Run full v2 eval (100+ prompts × 5 runs × 3 conditions)
```bash
rsync -av hooks/ ~/.claude/plugins/cache/n8n-local/n8n-knowledge/0.3.6/hooks/
bash scripts/eval/run-eval-v2.sh
# Estimated: ~1500 API calls, ~$225 on Opus, ~15 min parallel
```

#### 3. Run analysis
```bash
python3 scripts/eval/analyze.py out/eval/<latest-run-dir> --bootstrap 2000
```

#### 4. LLM-as-judge quality scoring (Task 20)
- **Model options**: DeepSeek API (cheap, Dan has tokens), OpenAI GPT-4o (Dan has $20/mo Codex sub)
- **Rubric** (5 dimensions, 1-5 scale each):
  1. Accuracy: correct nodes, correct operations, valid topology
  2. Completeness: covers all parts of the user's request
  3. Actionability: can the user actually implement this? (specific fields, values)
  4. Citation quality: cites real issues with links vs generic warnings vs nothing
  5. Gotcha awareness: proactively warns about or designs around known issues
- **Run both orderings**: A-vs-B and B-vs-A to control position bias
- **Script needed**: scripts/eval/judge.py that:
  - Loads response pairs from eval results
  - Calls judge model with rubric
  - Runs in both orderings
  - Outputs per-prompt scores + aggregates
- **DeepSeek API setup**: Key from Dan's portfolio/.env or n8n-hindsight env. Endpoint: https://api.deepseek.com/v1/chat/completions. Model: deepseek-chat.

#### 5. Distribution plots (Task 21)
- Python script using matplotlib (if available) or ASCII box plots
- Box plots for: cost, time, turns per condition
- p50/p95/p99 latency
- Histogram of quality scores from judge
- Per-category breakdown (simple/medium/complex/gotcha)

#### 6. Failure analysis
- Identify 5 worst prompts per condition (highest cost, most turns, lowest quality)
- Document why they failed
- Check: are the failures systematic or random?

#### 7. Write eval report (Task 22)
Structure:
1. **Methodology**: test set construction, isolation, 3 conditions, N=5 runs, bootstrap CIs, judge model
2. **Results**: table with CIs, distribution plots, per-category breakdown
3. **Failure analysis**: worst 5 per condition
4. **Limitations**: sample size, author bias, prompt selection, LLM-as-judge reliability
5. **Reproducibility**: all scripts, prompts, raw data provided
6. **References**: Anthropic eval guide, RAGAS, Bowyer et al., DeepEval

### Key Findings So Far (from isolated 28-prompt eval)
- Plugin: 100% node accuracy, 1.0 avg turns, $0.075 avg cost, 53s avg time
- n8n-mcp: 100% node accuracy, 3.1 avg turns, $0.133 avg cost, 65s avg time
- Plugin 44% cheaper, 18% faster, always 1 turn
- MCP spikes to 6-9 turns on complex prompts (costs 3-4x more)
- Plugin designs around issues on 6/8 gotcha prompts vs MCP 4/8
- Citation of real issue numbers: 0/28 both (instruction works in single test but not at scale)
- Gotchas ARE in injected context (14 Slack issues, 19 OpenAI issues returned by gotcha recall)

### Files
- `scripts/eval/run-eval-v2.sh` — main eval runner (v2, production-ready)
- `scripts/eval/run-eval-isolated.sh` — v1 runner (legacy, 2 conditions only)
- `scripts/eval/analyze.py` — bootstrap CI analysis
- `scripts/eval/ground_truth.jsonl` — eval prompts (48 currently, 100+ target)
- `scripts/eval/additional_prompts.jsonl` — agent-generated prompts (pending)
- `scripts/eval/eval.py` — v1 retrieval-only eval (legacy)
- `out/eval/` — raw eval results (gitignored)

### Available Resources
- Claude Max subscription: ~76% weekly cap remaining (resets Thursday 4pm)
- DeepSeek API tokens (cheap, for judge)
- OpenAI/Codex $20/mo subscription (GPT-4o for judge alternative)
- n8n-mcp installed at user scope: `claude mcp add n8n-mcp --scope user -- npx -y n8n-mcp`

### Critical Methodology Notes
- **Isolation**: MUST use `--settings clean.json` to suppress global Hindsight hooks. Without this, dan-shared memories leak into ALL conditions.
- **--bare breaks auth**: Don't use it. Use --settings with empty hooks instead.
- **Env var casing**: Claude Code UPPERCASES all plugin option env var names (DEBUGRECALL not debugRecall).
- **10K char inline cap**: additionalContext >10K spills to file that Claude may skip. Cap node specs at 5.
- **Gotchas first**: Semantic results before node specs for primacy effect.
- **Prior eval runs (1-6) contaminated**: Only run-eval-isolated.sh and run-eval-v2.sh results are trustworthy.
