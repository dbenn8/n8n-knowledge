# n8n Knowledge — Claude Code Plugin

**v0.3.7**

Stop babysitting web search permissions. Get instant, curated n8n answers with source links.

This Claude Code plugin connects to a centralized [Hindsight](https://hindsight.vectorize.io) knowledge base with 42,000+ curated data points from n8n's ecosystem — official docs, GitHub issues with status, community solutions and workarounds, feature requests with vote counts, and the n8n source code. It works out of the box with no setup, API keys, or configuration required.

Hindsight's [TEMPR recall](https://hindsight.vectorize.io/developer/retrieval) (Temporal Entity Memory Priming Retrieval) runs four search strategies in parallel — semantic, BM25 keyword, graph traversal, and temporal — merged via Reciprocal Rank Fusion and cross-encoder reranking. It's the first agent memory system to [surpass 90% accuracy on LongMemEval](https://hindsight.vectorize.io/developer/performance), with zero LLM cost per recall and 100-600ms typical latency. Every result includes a source link back to the specific doc page, GitHub issue, or community post so you or the model can quickly verify the original context.

## Install

```bash
/plugin install n8n-knowledge@n8n-local
```

Or clone and install locally:

```bash
git clone https://github.com/dbenn8/n8n-knowledge.git
# Add as local marketplace in Claude Code:
# /plugin marketplace add /path/to/n8n-knowledge
# /plugin install n8n-knowledge@n8n-knowledge-local
```

## What it does

### Design layer (gotchas, issues, docs)

- **Auto-recall** — detects n8n keywords in your messages and injects relevant docs, issues, and community solutions as context (~5 results, <1 second)
- **Manual recall** — ask Claude to search deeper when auto-recall didn't trigger (~20 results)
- **Confidence scoring** — each result annotated HIGH/MEDIUM/LOW based on source type and engagement metrics (votes, likes, views, solved status), with user-configurable thresholds
- **GitHub issue state** — every GitHub result is prefixed with its canonical state, e.g. `[OPEN]` or `[CLOSED·completed·2026-02-26]` / `[CLOSED·not_planned·…]`. The model is warned that `[CLOSED·completed]` usually means already fixed, `[CLOSED·not_planned]` means n8n won't fix it, and versions in result text are the reporter's environment — so it never builds a workaround for a bug that's already resolved.
- **Backstop recall** — refreshes n8n context during an agentic session (after Edit/Write/Task), not just on your prompt — gated, deduped, and capped. See [Backstop recall](#backstop-recall-mid-turn-context).
- **Source citations** — every result links to the specific doc page, GitHub issue, or community post

### Build layer (node specs, workflow examples)

- **Node-name detection** — identifies n8n node names mentioned in prompts (3,591-entry dictionary covering all official and community nodes). Handles trigger-intent detection ("listen for Gmail events" → gmailTrigger), camelCase splitting ("httpRequest" → "http request"), and compound service names ("sentryIo" → "sentry").
- **Structured node-spec recall** — when a node name is detected, issues a parallel tag-filtered recall (`type:node-spec` + `node:<type>`) that returns the node's operations, fields, types, and defaults. Rendered as compact `kind="node-spec"` blocks at HIGH confidence, separate from regular results.
- **13,000+ node specifications** — every n8n node (1,851 nodes) split into per-resource, per-operation units. Large multi-resource nodes like Slack (44 ops), Salesforce (65 ops), and Gmail (26 ops) are split so each operation's fields are individually recallable.
- **28 official workflow examples** — node-level wiring context, topology maps, and full importable JSON. Sticky notes and source JSON suppressed from auto-recall (available via manual recall to avoid context bloat).

### Project detection

Two-tier repo detection with multiple signals:

- **n8n codebase** — `package.json` with n8n dependency, `.n8n.json` config files, README mentioning "n8n", or workflow JSON files (`{"name":"...","nodes":[...}`)
- **n8n consumer** — `docker-compose.yml` referencing n8n
- **Keyword gating** — broad keywords (workflow, node, trigger, webhook, etc.) fire in n8n projects; only explicit "n8n" fires in consumer repos. Zero noise in non-n8n projects.

### Debug mode

See exactly what context is injected into Claude's prompt:

```bash
# In another terminal:
tail -f /tmp/n8n-knowledge-debug.log
```

Set `debugRecall` to control output:
- `summary` (default) — first 30 lines with line count
- `full` — complete injected context
- `off` — no debug output

## What's in the knowledge base

| Source | Count | Updated |
|---|---|---|
| Official docs (docs.n8n.io) | 315 pages | Nightly |
| GitHub issues & PRs | 4,500+ | Nightly |
| Community questions | 35,000+ | Nightly |
| Feature requests (with vote counts) | 2,600+ | Nightly |
| Built with n8n examples | 1,100+ | Nightly |
| n8n source code (core packages) | 6,200+ files | Nightly |
| Node specifications | 13,000+ units | On release |
| Workflow examples | 28 workflows | On release |


## Configuration

### Plugin options

| Setting | Default | Description |
|---|---|---|
| `enableAutoRecall` | `true` | Auto-recall on every message. Disable for manual-only (saves tokens). |
| `showRecallResults` | `true` | When enabled, Claude cites the knowledge base. When disabled, Claude uses the context silently. |
| `debugRecall` | `summary` | Show injected context in `/tmp/n8n-knowledge-debug.log`. Options: `off`, `summary`, `full`. |

### Backstop recall (mid-turn context)

Auto-recall only fires on **your** message (the `UserPromptSubmit` hook). But a long agentic turn drifts: by the time Claude has read files, edited code, and spun up subagents, the original recall context may be stale or about a different topic than what it's now working on.

Backstop recall fills that gap by refreshing n8n knowledge-base context **during** the agent's reasoning turn:

- **After `Edit`/`Write`/`Task` tool calls** — a `PostToolUse` hook inspects what Claude just wrote, extracts a fresh-keyword-anchored query, and injects a new `<result>` block as `additionalContext`. Topics already covered this session are skipped, and recalls are capped per session so it stays quiet once Claude has what it needs.
- **Into `Task` subagents** — an optional `PreToolUse` hook can prepend the recalled context directly into a subagent's prompt so dispatched agents start with the relevant n8n knowledge. This is **off by default** (experimental).

It complements auto-recall rather than replacing it: auto-recall covers the user's question, backstop recall covers where the work actually goes.

#### Options

| Setting | Default | Description |
|---|---|---|
| `enableBackstopRecall` | `true` | Refresh n8n context during agent reasoning (after Edit/Write/Task). Disable to save tokens. |
| `backstopRecallCap` | `4` | Max backstop recalls per session. |
| `backstopRecallMaxTokens` | `8000` | Returned-context size cap per backstop recall. |
| `backstopRecallBudget` | `high` | Hindsight recall effort: `low`, `mid`, or `high`. |
| `enableSubagentInjection` | `false` | Prepend n8n context into Task subagent prompts (experimental). |
| `triggerKeywords` | `""` (defaults) | Comma-separated broad keywords that trigger recall inside n8n codebases. See below. |

#### Trigger keywords and the `DEFAULTS` sentinel

Inside an n8n codebase, recall fires on a set of broad keywords (in consumer repos, only the explicit token `n8n` triggers it). The current built-in default list is:

```
workflow, node, trigger, webhook, credential, expression, execution
```

`triggerKeywords` lets you customize this list. The special token `DEFAULTS` expands inline to the built-in list above, so you can extend it without retyping every keyword:

- **Extend** — `DEFAULTS, mynode` → the built-ins **plus** `mynode`.
- **Replace** — `workflow, node, mything` → exactly these three; the rest of the built-ins are dropped.
- **Reset** — leave the field blank (or include `DEFAULTS`) to use the built-in list.

### Scoring tuning (optional)

Each auto-recalled result gets a confidence score based on its source type, engagement metrics, and resolution signals. You can tune the scoring per project by creating `.claude/n8n-knowledge.local.md`. All fields are optional — only override what you want to change.

```markdown
---
# Confidence level thresholds
high_threshold: 70
medium_threshold: 50

# Base scores by source type
docs_base: 80
github_base: 49
community_base: 40

# GitHub-specific bonuses
clear_signal_bonus: 25
author_member_bonus: 5

# Community engagement bonuses
solved_bonus: 25

# Engagement bonuses
high_engagement_threshold: 10
high_engagement_bonus: 20
medium_engagement_threshold: 3
medium_engagement_bonus: 10
high_views_threshold: 500
views_bonus: 5

# Result limits
max_results: 5
max_low_results: 1

# Text truncation per confidence level (-1 = no limit)
max_text_length_high: -1
max_text_length_medium: 800
max_text_length_low: 300
---
```

Add `.claude/*.local.md` to your `.gitignore`.

## Refreshing node specs

When a new n8n version ships with updated nodes:

```bash
bash scripts/refresh-node-lookup.sh
```

This fetches the latest `n8n-mcp` package, regenerates the node dictionary, and runs validation tests. The dictionary is checked into the repo so users don't need to run this themselves.

## How it works

1. `UserPromptSubmit` hook fires on every message
2. `detect-n8n.sh` checks if the message is n8n-related (multi-signal repo detection + keyword matching)
3. `node_lookup.py` identifies node names in the prompt for structured recall
4. `recall.sh` curls the Hindsight API (semantic), `structured_recall.sh` curls with tag filters (node specs)
5. Results merged (node specs prepended), scored by `format_results.py`, and injected as `additionalContext`
6. Debug output written to `/tmp/n8n-knowledge-debug.log` if `debugRecall` is not `off`

No MCP server. No daemon. No dependencies. Just bash, curl, and Python stdlib.

## Tests

```bash
bash tests/run-all.sh
```

165 tests across 9 test files: detection, recall formatting, node lookup, structured recall, lookup integrity, GitHub state, observation scoring, backstop recall, and integration.

## Roadmap

- **Workflow scoring** — workflow example units currently score LOW in auto-recall; need their own scoring path or tag-based boosting
- **Richer workflow tags** — add trigger type, complexity, use-case, and integration tags to workflow units for better semantic matching
- **More workflow sources** — expand beyond the 28 official docs examples to the n8n template library
- **Public retain with trust tiers** — community contributions tagged and weighted by Discourse identity and trust level
- **Prompt injection filtering** — pre-filter + LLM classifier on community content before ingestion

## Contributing

PRs welcome! The knowledge base is public and auto-syncs nightly. If you want to improve the plugin itself:

1. Fork the repo
2. Make changes
3. Run `bash tests/run-all.sh` to verify
4. Open a PR

## License

MIT — see [LICENSE](LICENSE).
