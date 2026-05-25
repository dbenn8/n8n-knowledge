# n8n Knowledge — Claude Code Plugin

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

- **Auto-recall** — detects n8n keywords in your messages and injects relevant docs, issues, and community solutions as context (~5 results, <1 second)
- **Manual recall** — ask Claude to search deeper when auto-recall didn't trigger (~20 results)
- **Smart detection** — two-tier repo detection: broad keywords (workflow, node, trigger, etc.) in n8n codebases, explicit "n8n" only in consumer repos (e.g., docker-compose referencing n8n). Zero noise in non-n8n projects.
- **Confidence scoring** — each result annotated HIGH/MEDIUM/LOW based on source type and engagement metrics (votes, likes, views, solved status), with user-configurable thresholds. The plugin filters and truncates results. The model is warned that injected context is publicly sourced and directed to verify safety and review confidence scores before acting on any result.
- **Source citations** — every result links to the specific doc page, GitHub issue, or community post
- **Speech-to-text friendly** — handles "nation" → "n8n" disambiguation

## What's in the knowledge base

| Source | Count | Updated |
|---|---|---|
| Official docs (docs.n8n.io) | 315 pages | Nightly |
| GitHub issues & PRs | 4,500+ | Nightly |
| Community questions | 35,000+ | Nightly |
| Feature requests (with vote counts) | 2,600+ | Nightly |
| Built with n8n examples | 1,100+ | Nightly |
| n8n source code (core packages) | 6,200+ files | Nightly |


## Configuration

### Plugin options

| Setting | Default | Description |
|---|---|---|
| `enableAutoRecall` | `true` | Auto-recall on every message. Disable for manual-only (saves tokens). |
| `showRecallResults` | `true` | When enabled, Claude cites the knowledge base as a source in its responses. When disabled, Claude uses the context silently. Note: raw injected context is only visible in the conversation transcript, not in the chat UI. |

### Scoring tuning (optional)

Each auto-recalled result gets a confidence score based on its source type, engagement metrics, and resolution signals. You can tune the scoring per project by creating `.claude/n8n-knowledge.local.md`. All fields are optional — only override what you want to change.

```markdown
---
# Confidence level thresholds
# A result's numeric score determines its label:
# score >= high_threshold → HIGH, >= medium_threshold → MEDIUM, otherwise LOW
high_threshold: 70
medium_threshold: 50

# Base scores by source type (starting score before bonuses)
docs_base: 80
github_base: 49
community_base: 40

# GitHub-specific bonuses
clear_signal_bonus: 25       # closed with state_reason (not stale) OR open with team label
author_member_bonus: 5        # author is MEMBER or COLLABORATOR

# Community engagement bonuses
solved_bonus: 25              # community post has an accepted answer

# Engagement bonuses (GitHub: reactions + comments*4, Community: votes + likes)
high_engagement_threshold: 10 # engagement >= this to earn the high bonus
high_engagement_bonus: 20
medium_engagement_threshold: 3
medium_engagement_bonus: 10
high_views_threshold: 500     # views >= this to earn the views bonus
views_bonus: 5

# Result limits
max_results: 5      # total results recalled from the knowledge base
max_low_results: 1  # only keep the N highest-scoring LOW results (reduces noise)

# Text truncation per confidence level
# -1 = no limit (inject full text), positive number = max characters
# Floor of 300 chars enforced on all levels so Claude always has enough to work with
max_text_length_high: -1    # HIGH results injected in full
max_text_length_medium: 800 # enough for Claude to judge usefulness
max_text_length_low: 300    # brief, minimal noise
---
```

Add `.claude/*.local.md` to your `.gitignore`.

## How it works

1. `UserPromptSubmit` hook fires on every message
2. `detect-n8n.sh` checks if the message is n8n-related (two-tier repo detection + keyword matching)
3. `recall.sh` curls the Hindsight API, `format_results.py` scores each result by source type and engagement
4. Results injected as `additionalContext` with confidence labels — Claude sees them before generating a response

No MCP server. No daemon. No dependencies. Just bash and curl.

## Tests

```bash
bash tests/run-all.sh
```

27 tests: 12 detection, 12 recall formatting, 3 integration.

## Roadmap

- **Consolidation-level source metadata** — The knowledge base consolidates related memories into synthesized insights, which currently lose their source URLs and engagement metrics. A consolidation directive has been added to preserve this metadata in future rounds. Once tested, the secondary enrichment calls used to recover source links for consolidated results can be removed, improving latency.
- **Public retain with trust tiers** — Community contributions tagged and weighted by Discourse identity and trust level.
- **Prompt injection filtering** — Pre-filter + LLM classifier on community content before ingestion.

## Contributing

PRs welcome! The knowledge base is public and auto-syncs nightly. If you want to improve the plugin itself:

1. Fork the repo
2. Make changes
3. Run `bash tests/run-all.sh` to verify
4. Open a PR

## License

MIT — see [LICENSE](LICENSE).
