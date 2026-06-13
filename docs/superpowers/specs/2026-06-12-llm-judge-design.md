# LLM Judge for Eval Results — Design Spec

**Date:** 2026-06-12
**Status:** Approved design, pending implementation plan

## Problem

The eval harness measures schema validity and deterministic gotcha rules
(`node_swap`, `param_check`), but nothing measures whether a generated workflow
actually accomplishes what the user asked for. A workflow can be schema-valid
yet miss half the user's intent. Additionally, `llm_only` gotcha rules fall
back to a weak term-match heuristic. `gotcha_scoring.py` was written
anticipating this: its rules carry human-readable `gotcha`/`workaround` text
"so the rules double as documentation and can feed an LLM judge later."

## Solution

A standalone post-hoc judge script, `scripts/eval/judge_results.py`, that
points at any existing `out/eval/*-v2` result directory and uses Opus (via
headless `claude -p`) to render a per-result verdict on two dimensions:

1. **Intent fit** — does the final workflow, as configured, accomplish the
   user's original request (triggers, routing, fields, outputs)?
2. **Gotcha coverage** — for prompts with a known gotcha, does the design
   avoid the bug / apply the documented workaround?

### Decisions made with Dan

- **Post-hoc only (Option A):** rerunnable on past runs (overnight 84.4% run,
  the other session's repeat). No coupling to `run-eval-v2.sh`. A `--judge`
  flag in v2 may come later but is out of scope.
- **Binary verdicts (Option A) with a per-prompt upgrade path to checklist
  mode (Option C):** any prompt with an entry in a new `judge_criteria.jsonl`
  is judged in checklist mode; all others get binary pass/fail. Upgrading a
  prompt = adding one JSONL line, zero code changes.
- **Invocation via `claude -p --model opus` in an ISOLATED config dir:**
  reuses existing Claude Code auth (no API key), but judge calls must NOT run
  against the user's live config — that would load the Hindsight MCP +
  auto-recall hooks (injecting dan-shared/n8n-bank context into the judge
  prompt → contamination and unblinding) and the n8n-knowledge plugin itself,
  and auto-retain would pollute memory banks with judge chatter. The judge
  creates a scratch `CLAUDE_CONFIG_DIR` with clean settings (no plugins, no
  hooks), an empty MCP config (`--strict-mcp-config`), and the live
  `~/.claude/.credentials.json` **symlinked, never copied** (the eval
  harness's credential `cp` went stale mid-run and 401'd an entire arm on
  2026-06-12 — the symlink always sees the rotating file).
- **Default concurrency 16** (flag-overridable). Judge calls are single-turn,
  no tools, and never touch the Hindsight/validator services, so eval-side
  load incidents don't apply. Back off and retry on rate-limit errors rather
  than dropping verdicts.

## CLI

```
python3 scripts/eval/judge_results.py out/eval/<dir> \
  [--conditions plugin,n8n-mcp]   # default: all condition subdirs found
  [--prompts 000,003,...]          # default: all results
  [--model opus]
  [--concurrency 16]
  [--force]                        # re-judge even if .judge.json exists
  [--dry-run]                      # build judge inputs, print, no claude calls
```

## Per-result flow

For each `prompt-XXX-runYY.json` under each condition dir:

1. **Gather inputs:**
   - Original prompt text (from the result JSON's prompt field; fallback to
     the prompt source files by index).
   - Final workflow JSON: `*.validated.workflow.json`, fallback
     `*.candidate.workflow.json`, fallback the model-written file(s) in the
     `*.workflow/` directory. Record which source was used
     (`validated | candidate | written`).
   - Validation status from `*.validation.json` — extracted fields only
     (`valid`, `error_count`, `warning_count`). The raw file leaks provenance
     (`enrichment_mode`, absolute paths) and must never reach the judge.
     Context only — the judge is told validity does not imply intent fit.
   - Matching `gotcha_rules.jsonl` rule by `prompt_idx` (its `gotcha` +
     `workaround` text), if any.
   - Matching `judge_criteria.jsonl` entry, if any → checklist mode.
2. **Blind the judge:** the constructed input must contain no provenance —
   no condition name (plugin/mcp), no model name, no file paths. Workflow
   `meta`/`notes` fields pass through untouched (they are part of the
   artifact under judgment).
3. **Call Opus:** `claude -p --model opus` with a JSON-only instruction.
   Parse leniently (strip code fences, find first `{`...last `}`). Up to 2
   retries on parse failure, re-prompting with the parse error. Back off and
   retry on rate-limit/transient errors.
4. **Write verdict** to `prompt-XXX-runYY.judge.json` beside the artifacts.
   Existing verdict files are skipped unless `--force` (cache semantics).

## Verdict schema (`*.judge.json`)

```json
{
  "intent_fit": "pass | fail",
  "intent_reasoning": "must cite node names/params from the workflow as evidence",
  "gotcha_handled": "pass | fail | not_applicable",
  "gotcha_reasoning": "...",
  "criteria": [{"criterion": "...", "met": true}],
  "confidence": "high | low",
  "judge_model": "opus",
  "workflow_source": "validated | candidate | written",
  "judged_at": "ISO timestamp"
}
```

- `criteria` present only in checklist mode.
- **Fail-closed:** missing or unparseable workflow → `intent_fit: "fail"`
  with reasoning `"no parseable workflow artifact"` — written locally without
  a claude call.
- `gotcha_handled: "not_applicable"` for prompts with no gotcha rule.
- A judge call that exhausts retries writes NO verdict file (so a rerun picks
  it up) and is reported in the summary as an error, not a fail.

## `judge_criteria.jsonl` (checklist upgrade path)

```json
{"prompt_idx": 20, "must": ["routes by customer tier", "posts to a distinct channel per tier", "avoids dynamic channel expression on a single Slack node"], "nice": ["handles unknown tier"]}
```

- Keyed by `prompt_idx`, same pattern as `gotcha_rules.jsonl`.
- Checklist mode: judge marks each item met/unmet with evidence;
  `intent_fit = pass` iff all `must` items are met. `nice` items are
  informational only.
- File starts empty/absent; absence of the file or of an entry means binary
  mode. Aggregates stay comparable because checklist mode still rolls up to
  pass/fail.

## Judge prompt (sketch)

- Persona: expert n8n workflow reviewer.
- Inputs: the user's request, the workflow JSON, (if applicable) the known
  gotcha + workaround text, (if applicable) the criteria checklist.
- Instructions: judge the artifact only; schema validity does NOT imply
  intent fit; verdicts must quote node names/types/params as evidence;
  respond with a single JSON object matching the schema, nothing else.
- The transcript is deliberately NOT included — the judge evaluates the
  artifact, not the chat's self-description, and it keeps calls cheap.

## Auth handling (lessons from the 2026-06-12 OAuth incidents)

The judge runs in a scratch config dir (for plugin/hook/MCP isolation — see
Decisions) but the credentials file is a **symlink to the live
`~/.claude/.credentials.json`**, never a copy, so the stale-snapshot failure
that 401'd an entire Sonnet eval arm (credentials `cp`'d at launch, token
rotated a minute later) cannot occur here. Two residual risks are handled
explicitly:

- **Preflight expiry check:** before launching a pass, read
  `~/.claude/.credentials.json` `expiresAt`. If the token expires within the
  estimated pass duration (calls remaining ÷ concurrency × ~30s, generous),
  print a warning advising a `/login` first; `--yes`/interactive confirm to
  proceed anyway.
- **No credential bytes at rest in scratch dirs:** the scratch config dir is
  created with `mktemp -d` (mode 0700) and the credentials entry is a symlink
  only. A hard kill (SIGKILL — EXIT traps don't fire) therefore orphans a
  dangling pointer at worst, never a copy of the secret; access through the
  symlink is still governed by the 0600 perms on the real file. This closes
  the loose-credential-left-behind failure observed with the eval harness's
  `cp` approach on 2026-06-11.
- **401 = halt, never retry:** any judge call returning an auth error stops
  the WHOLE pass immediately (all workers drained, no refresh attempts —
  concurrent processes racing a single-use refresh token is what killed the
  credential family on 2026-06-12). Completed verdicts are already on disk
  (`.judge.json` cache), so after the user re-logs-in, rerunning the same
  command resumes exactly where it stopped.

## Aggregation & output

- `judge-summary.json` written at the result-dir root: per condition —
  counts, intent-fit %, gotcha-handled % (denominator = applicable prompts
  only), n/a count, error count, list of fails with one-line reasons.
- Printed table in the same style as existing validity tables (per-condition
  columns, % rows) so it can sit beside `summarize_results.py` output.
- Per CLAUDE.md, the orchestrator restates the table in message text after
  running.

## Testing (TDD)

All unit tests mock the claude invocation (inject a fake runner returning
fixture verdicts/garbage):

1. **Parser:** clean JSON, fenced JSON, prose-wrapped JSON, malformed →
   retry path, retries exhausted → error (no verdict file).
2. **Artifact gathering:** validated → candidate → embedded fallback chain;
   missing workflow → local fail-closed verdict without a claude call.
3. **Blinding:** constructed judge input contains no condition names, model
   names, or file paths for a fixture result.
4. **Isolation:** the constructed invocation sets a scratch
   `CLAUDE_CONFIG_DIR` containing clean settings (no plugins/hooks), passes
   `--strict-mcp-config` with an empty MCP config, and the scratch
   credentials file is a symlink to the live one (assert `os.path.islink`),
   never a copy.
5. **Mode switching:** prompt with criteria entry → checklist prompt +
   roll-up logic (all must met = pass; one unmet = fail; nice ignored).
6. **Cache semantics:** existing `.judge.json` skipped; `--force` re-judges.
7. **Aggregation math:** percentages, n/a exclusion from gotcha denominator,
   errors counted separately from fails.
8. **Auth handling:** preflight expiry math (token expiring inside the
   estimated window → warning path); a mocked 401 mid-pass halts all workers,
   writes no partial verdict, and a rerun resumes from the cache.
9. **Live smoke (manual gate):** judge 2–3 results from the 2026-06-12
   defaults smoke dir (`out/eval/20260612-075953-v2`) with real Opus before
   any full pass — orchestrator-run, with Dan's go-ahead per the standing
   eval-approval rule.

## Cost / runtime

~2–8k input tokens per call. Overnight dir = 128 prompts × 2 conditions =
256 calls; at concurrency 16 ≈ 8–15 minutes. Covered by Claude plan (no API
key spend).

## Out of scope

- `--judge` flag in `run-eval-v2.sh` (future).
- Judging historical runs whose output files were already cleaned up.
- Authoring `judge_criteria.jsonl` entries for all prompts (incremental,
  as time allows; drafts can be generated from prompt text + gotcha rules
  for Dan to skim).
