---
name: n8n-knowledge
description: Deep search of the n8n knowledge base — community solutions, GitHub issues, workarounds, and node specs. Use /n8n-knowledge when auto-recalled context is insufficient.
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

## Workflow validation (when enabled)

When workflow validation is enabled, every Write or Edit of a workflow
`.json` file automatically triggers the validator, which injects targeted
feedback (INVALID issues to fix, or VALID confirmation) after the write.

- The validator budget is capped per session (configurable; you will be told
  when the limit is reached and no further feedback will arrive).
- Each file write spends one validation — batch your fixes into complete
  edits rather than many small writes, and use this skill's deeper search to
  resolve unfamiliar node schemas BEFORE writing, so validations aren't spent
  on guessable errors.

## Coverage

- Official docs (docs.n8n.io), GitHub issues/PRs (nightly), community posts (35k+), node specs (13k+ units), workflow examples
