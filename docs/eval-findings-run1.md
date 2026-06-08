# Eval Findings — Run 1 (June 8, 2026)

## Setup
- 128 prompts from ground_truth.jsonl (7 categories: node_config, typo, negative, build_with_gotcha, popular_simple/medium/complex, community_informed, gotcha_build)
- 3 conditions: bare Claude, plugin (n8n-knowledge hooks), n8n-mcp (community MCP server)
- N=1 run per prompt per condition
- System prompt requires importable n8n workflow JSON in ```json block
- Post-hoc validation via n8n-mcp's full WorkflowValidator engine (node types, operations, connections, expressions, AI patterns)
- Isolation: --settings with empty hooks, --strict-mcp-config, --no-session-persistence, --disable-slash-commands

## Data Quality Notes
- Run 1 bare: clean data, no rate limiting
- Run 1 plugin: valid costs but inflated timing (avg 452s vs expected ~90s) — rate-limited by concurrent bare sessions
- Run 1 MCP: ALL 128 sessions returned {"error":"failed"} — unusable (launched after rate-limited plugin)
- MCP data sourced from Run 2 which completed separately — valid costs/responses but inflated timing (avg 600s)
- **Timing data not comparable across conditions due to rate limiting. Cost and quality metrics are valid.**

## Results

### Cost (excluding timing)

| Condition | Avg Cost | Avg Turns | Avg Response (chars) | Zero-Cost |
|-----------|----------|-----------|---------------------|-----------|
| Bare      | $0.252   | 1.0       | 11,232              | 0         |
| Plugin    | $0.223   | 1.0       | 11,600              | 0         |
| MCP       | $0.209   | 1.4       | 11,059              | 3         |

With JSON output requirement, costs converge — all conditions in the $0.21-0.25 range. The JSON generation dominates token usage. MCP slightly cheapest but with 3 failed responses.

### Build Quality (n8n-mcp validator)

| Condition | Has JSON | Valid Workflow | Rate  | Avg Errors | Avg Warnings |
|-----------|----------|----------------|-------|------------|--------------|
| Bare      | 126/128  | 24/128         | 18.8% | 3.4        | 16.2         |
| Plugin    | 127/128  | 26/128         | 20.3% | 3.6        | 16.2         |
| MCP       | 124/128  | 26/128         | 20.3% | 3.2        | 15.5         |

**All three conditions produce essentially the same build quality** — ~20% of workflows pass the full n8n-mcp validator. Plugin and MCP tied at 20.3%, bare slightly behind at 18.8%.

### Top Validation Errors (across all conditions)

1. **Invalid operation value** — Claude uses wrong operation enum (e.g., "sendMessage" instead of "post" for Slack)
2. **Expression format errors** — Mixed literal text and expressions in wrong format
3. **Missing required fields** — conditions.options.version, Send Message To, Form Path
4. **Expected object but got string** — Wrong parameter type
5. **Invalid select value** — Wrong dropdown selection

### Key Insight
The ~80% failure rate is driven by config-level details — exact operation enum values, required field formats, expression syntax. Neither injected node specs (plugin) nor tool-based lookups (MCP) provide enough parameter-level schema detail for Claude to produce valid workflows.

## What This Means

### Plugin value proposition (updated)
The plugin does NOT differentiate on build quality — all three conditions perform equally. The plugin's value is:
1. **Speed** — always 1 turn vs MCP's 1.4 avg (up to 10 on complex prompts in earlier runs without JSON requirement)
2. **Gotcha awareness** — designs around known issues (6/8 in earlier 28-prompt eval)
3. **Citation of real issues** — links to actual GitHub issues (inconsistent but present)

### What would actually improve build quality
1. **Validation-and-fix loop** — Let Claude generate, validate, fix, repeat
2. **Richer parameter schemas** — Current node specs don't include operation enum values or required field constraints
3. **Template-based generation** — Start from known-good workflow templates instead of generating from scratch

## Comparison to Earlier Runs (design-only, no JSON requirement)

| Metric | Earlier (28 prompts, design-only) | This Run (128 prompts, JSON required) |
|--------|-----------------------------------|---------------------------------------|
| Plugin avg cost | $0.075 | $0.223 |
| MCP avg cost | $0.133 | $0.209 |
| Plugin turns | 1.0 | 1.0 |
| MCP turns | 3.1 | 1.4 |
| Plugin advantage | 44% cheaper, 18% faster | 7% more expensive, similar quality |

The JSON requirement narrows the gap significantly. Without it, plugin's speed/cost advantage is clear. With it, the story shifts to quality parity + gotcha awareness.

## Files
- Eval runner: scripts/eval/run-eval-v2.sh
- Analysis: scripts/eval/analyze.py
- Validator: scripts/eval/validate_workflow.py + validate-with-mcp.js
- Ground truth: scripts/eval/ground_truth.jsonl (128 prompts)
- Raw results: out/eval/20260608-015052-v2/ (run 1), out/eval/20260608-020733-v2/ (run 2)
- Combined best-of: out/eval/20260608-best-combined/

## Next Steps
1. Run a clean N=1 with no rate limiting (sequential conditions, nothing else running)
2. Validate timing data from clean run
3. Consider validation-fix loop as plugin feature
4. Build LLM-as-judge for task-completion scoring
5. Run N=5 across multiple sessions for statistical rigor
