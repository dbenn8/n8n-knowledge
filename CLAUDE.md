# n8n-knowledge — repo instructions

## ⚠️ EVAL: VERIFY THE DEEPSEEK SERVED MODEL — EVERY RUN, NO EXCEPTIONS

**CRITICAL RULE:** A DeepSeek eval is only "Pro" if the **transcripts prove it**. Passing a Claude
alias (`--model claude-sonnet-4-6`) through `scripts/eval/deepseek.sh` and trusting
`ANTHROPIC_DEFAULT_SONNET_MODEL` to remap it **does NOT route to Pro** — Claude Code keeps the alias
as a label but sends the actual agent turns to the **haiku default = `deepseek-v4-flash`**.

Proven 2026-06-24: **every DeepSeek run before that date actually ran on Flash** (0 Pro responses in
transcripts) while being labeled and priced as Pro (~3× cost overstatement). `deepseek.sh` is now
fixed (it rewrites `--model` to the concrete id), **but you must still verify after every run:**

```bash
find <run_dir> -name '*transcript*.jsonl' -exec grep -ho '"model":"[^"]*"' {} + | sort | uniq -c
```

- A **real Pro response** is the literal `"model":"deepseek-v4-pro"` (NO `[1m]`).
- The `"resolvedModel":"deepseek-v4-pro[1m]"` field is **intent only** — ignore it for verification.
- Wanted Pro but see `deepseek-v4-flash`? The run is **mislabeled** → reprice at flash rates
  (0.14 / 0.0028 / 0.28 per 1M), set `cost_model=deepseek-flash`, re-ingest, and re-run on the fixed
  wrapper for genuine Pro.

Full runbook + this warning: `scripts/eval/README.md`. Backend (deepseek-vs-claude) identification is
separate — check `ANTHROPIC_BASE_URL` via `ps eww`, not the `--model` flag.

## General
Follow the global and `codeNew/.claude/CLAUDE.md` instructions (message timestamps, restate stats in
message text, no AI attribution in commits, never silently change unrequested functionality).
