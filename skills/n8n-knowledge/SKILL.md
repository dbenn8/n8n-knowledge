---
name: n8n-knowledge
description: Use when working with n8n workflows, nodes, expressions, hosting, configuration, error handling, or any n8n-related development. Triggers on n8n errors, webhook issues, expression syntax, node configuration, credential setup, Docker deployment, scaling, or workflow debugging.
---

# n8n Knowledge Base

400k+ data points from n8n's ecosystem in a graph memory database — official docs, GitHub issues with resolution state, community solutions, node specifications, and workflow examples. Auto-recalled by a hook on every message; manual recall available for follow-ups.

## Auto-recall (check first — don't duplicate)

A hook automatically searches the knowledge base when n8n keywords are detected. Results appear as `n8n Knowledge Base` context in the system reminders above this message.

**IMPORTANT: Check your system reminders before doing anything.** If you see `n8n Knowledge Base` context with `<result>` blocks already injected, USE THAT CONTEXT DIRECTLY. Do not call recall-cli.sh — it would return the same or similar results and waste time/tokens. The auto-recall hook already ran for this prompt.

When you see auto-recalled results:
- Use them directly in your response — they are current and authoritative
- Prefer knowledge base over training data for n8n specifics
- Pay attention to GitHub issue state tags: `[CLOSED·completed]` means already fixed, `[CLOSED·not_planned]` means n8n won't fix it, `[OPEN]` means still active
- Node-spec results (`kind="node-spec"`) contain real field names, types, and defaults — use them for accurate configuration advice
- If the auto-recalled results don't cover the user's question, THEN do a manual recall for deeper results

**Speech-to-text:** Users dictating often say "n8n" but it decodes as "nation." If you see "nation" in a context that suggests workflow automation, the user likely means n8n.

## Manual recall

Use ONLY when: (1) auto-recall didn't fire (no `n8n Knowledge Base` in system reminders), (2) auto-recall results are thin and you need more depth on a specific topic, or (3) the user asks a follow-up question on a new topic not covered by the initial auto-recall.

Do NOT use manual recall if auto-recall already returned relevant node specs and context for the current question.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/hooks/lib/recall-cli.sh" "<your specific question>" high 8000
```

The output is `<result>…</result>` blocks. Prefer cited sources over machine-distilled synthesis on conflict; for solved/high-confidence items, fetch the source URL for the full thread.

## Coverage

- **Official docs** (docs.n8n.io): advanced AI, hosting, code, data, flow logic, courses, API, credentials
- **GitHub** (n8n-io/n8n): open issues and PRs, auto-synced nightly
- **Community** (community.n8n.io): real-world examples from "Built with n8n"

## When NOT to use

- General workflow automation questions not specific to n8n
- Questions about Zapier, Make, or other platforms (unless comparing to n8n)

## Configuration

- `enableAutoRecall` (default: true) — disable for manual-only, saves tokens
- `showRecallResults` (default: true) — disable for silent context injection
