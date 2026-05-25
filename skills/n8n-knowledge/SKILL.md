---
name: n8n-knowledge
description: Use when working with n8n workflows, nodes, expressions, hosting, configuration, error handling, or any n8n-related development. Triggers on n8n errors, webhook issues, expression syntax, node configuration, credential setup, Docker deployment, scaling, or workflow debugging.
---

# n8n Knowledge Base

315+ pages of official n8n docs in a graph memory database. Auto-recalled by a hook on every message; manual recall available for follow-ups.

## Auto-recall

A hook searches the knowledge base when n8n keywords are detected. Results appear as `[n8n Knowledge Base]` context.

**Speech-to-text:** Users dictating often say "n8n" but it decodes as "nation." If you see "nation" in a context that suggests workflow automation (e.g., "nation workflow", "set up nation with Docker"), the user likely means n8n — do a manual recall.

When you see results:
- Reference and cite what was found
- Prefer knowledge base over training data for n8n specifics — it's more current
- Auto-recall returns ~5 results (a quick scan). If results are thin or not quite on target, tell the user you can search deeper with a manual recall that returns up to 20 results.
- When `showRecallResults` is false, use context naturally without citing

## Manual recall

Use when: (1) auto-recall didn't fire (follow-up without n8n keywords), or (2) auto-recall results were thin and the user wants more depth. Manual recall returns up to **20 results** vs auto-recall's 5 — tell the user this when offering a deeper search.

```bash
curl -s -X POST "https://n8nhindsight.applikuapp.com/public/recall" \
  -H "Content-Type: application/json" \
  -d '{"query": "<your specific question>", "budget": "mid"}'
```

Budget `low` for quick lookups, `mid` for deeper searches. Use `results[].text` from response.

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
