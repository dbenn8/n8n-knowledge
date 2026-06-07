---
name: n8n-knowledge
description: Deep search of the n8n knowledge base — community solutions, GitHub issues, workarounds, and node specs. Use /n8n-knowledge when auto-recalled context is insufficient.
disable-model-invocation: true
---

# n8n Knowledge Base — Deep Search

The auto-recall hook already injects node specs and relevant context on every message. This skill is for **deeper manual searches** when you need more.

## When to use

Only invoke `/n8n-knowledge` when:
- Auto-recalled context (in system reminders) doesn't cover the topic
- You need community workarounds for a specific bug
- You want to check GitHub issue state for a known problem
- The user explicitly asks for a deeper search

## Manual recall

```bash
bash "${CLAUDE_PLUGIN_ROOT}/hooks/lib/recall-cli.sh" "<your specific question>" high 8000
```

Returns `<result>…</result>` blocks with source URLs, engagement metrics, and GitHub issue state tags.

## Reading auto-recalled context

If `n8n Knowledge Base` context appears in system reminders:
- `kind="node-spec"` — real field names, types, defaults from n8n's node catalog
- `[CLOSED·completed]` — bug is fixed, don't add workarounds
- `[CLOSED·not_planned]` — n8n won't fix, suggest workaround
- `[OPEN]` — active issue, warn the user
- Prefer knowledge base over training data for n8n specifics

## Coverage

- Official docs (docs.n8n.io), GitHub issues/PRs (nightly), community posts (35k+), node specs (13k+ units), workflow examples
