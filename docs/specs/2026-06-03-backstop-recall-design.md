# Backstop Recall (Mid-Turn Context Injection) — Design

**Date:** 2026-06-03
**Task:** #81
**Plugin:** n8n-knowledge (Claude Code)

## Problem

Auto-recall fires only on `UserPromptSubmit`. In agentic/design sessions — the "n8n builder using an LLM harness" use case — context is injected once at the user's prompt and never during the agent's multi-turn reasoning. So the knowledge base is least available exactly when the agent is doing the heavy lifting (writing n8n code, spawning subagents). Asking the model to *remember* to pull (a skill / CLAUDE.md rule) is forgettable by construction; the only un-forgettable layer is the harness. This feature pushes recall **during the agent's turn**, enforced by hooks.

## Goals

- Inject fresh, first-class n8n context during the agent's reasoning turn, not just at the user prompt.
- Keep it cheap and quiet: recall only on genuinely new/stale topics, gated and capped.
- Identical first-class output to the user-prompt path (0.3.3 `<result>` format, source-fact metadata).
- Never block or break a tool call under any failure.
- Unify all three recall consumers (user-prompt hook, backstop hook, manual skill) on one rendering path.

## Non-goals

- No change to the scoring/format logic of `format_results.py` beyond an output-mode flag and an event-name arg.
- No real tokenizer in the hook (query length is approximated by characters).
- No second hook for Bash/read tools.

---

## The mechanism constraint (drives the architecture)

Per Claude Code's hook-output schema:
- `PreToolUse` may return `permissionDecision` / `permissionDecisionReason` / `updatedInput` — **not** `additionalContext`.
- `PostToolUse` **may** return `additionalContext`.

Therefore:
- **Context the agent reads** is injected via **`PostToolUse` `additionalContext`** (fires right after Edit/Write/Task; context lands for the agent's next reasoning step).
- **Injecting context *into* a subagent before it runs** uses **`PreToolUse` on `Task` → `updatedInput`** to prepend the recalled block to the subagent's `prompt`.

`updatedInput` behavior must be verified with a 5-minute probe before the subagent path is enabled (it ships gated off by default).

---

## Components

| File | Responsibility |
|---|---|
| `hooks/backstop-recall.sh` (new) | PostToolUse hook (matcher `*`). Counts tool calls; on Edit/Write/Task runs the decision logic and injects `additionalContext`. |
| `hooks/backstop-subagent.sh` (new) | PreToolUse hook (matcher `Task`). Gated by `enableSubagentInjection`. Recalls and prepends the block to the subagent prompt via `updatedInput`. |
| `hooks/lib/backstop_state.py` (new) | Per-session state: read/update counters and the recalled-topic map. |
| `hooks/lib/query_window.py` (new) | Extracts a `<500`-token recall query from tool input using fresh-keyword anchoring (below). |
| `hooks/lib/recall-cli.sh` (new) | Shared entry: `recall-cli.sh <query> [budget] [max_tokens]` → `do_recall` (with `include.source_facts`) → `format_results.py` bare mode → prints the 0.3.3 `<result>` block. Used by the manual skill and the backstop. |
| `hooks/lib/recall.sh` (modify) | `do_recall` gains `budget` + `max_tokens` params (already sends `include.source_facts`). |
| `hooks/lib/detect-n8n.sh` (modify) | `should_recall` keyword list becomes configurable (`triggerKeywords` + `DEFAULTS` sentinel). Shared by all paths. |
| `hooks/lib/format_results.py` (modify) | Add (a) an event-name arg so output can be `PostToolUse` (vs `UserPromptSubmit`), and (b) a "bare" mode that prints just the `<result>` context string (no hook-JSON wrapper) for `recall-cli.sh`/skill. |
| `hooks/hooks.json` (modify) | Register the new PostToolUse (`*`) and PreToolUse (`Task`) hooks; keep existing UserPromptSubmit. |
| `skills/n8n-knowledge/SKILL.md` (modify) | Manual recall calls `recall-cli.sh` instead of raw curl → returns 0.3.3 format with metadata. |
| `.claude-plugin/plugin.json` (modify) | New `userConfig` knobs. |
| `README.md` (modify) | Document the backstop, the `triggerKeywords` `DEFAULTS` sentinel, and the current default keyword list. |

---

## Decision logic (shared by PostToolUse and PreToolUse-Task)

1. **Enabled?** `enableBackstopRecall` (default true). If false → exit 0.
2. **Trigger tool?** `tool_name ∈ {Edit, Write, Task}`. Non-trigger → bump `total_calls`, save, exit 0.
3. **Extract query** (see Query Windowing). If empty → save, exit 0.
4. **Gate:** `should_recall(query, cwd)` (reuse `detect-n8n.sh`). No → save, exit 0.
5. **Topic signature:** the sorted set of fresh n8n keywords found in the windowed query, joined — a stable per-topic key.
6. **Fire?** Recall if the signature is **new** (not in `topics`), OR **stale** = last recall of it was `> 15 total_calls` ago **OR** `> 5 trigger_calls` ago. Otherwise skip (save, exit 0).
7. **Cap:** if `recalls_done >= backstopRecallCap` (default 4) → skip.
8. **Recall + inject:** `recall-cli.sh <query> high 8000` → 0.3.3 `<result>` block. PostToolUse → `additionalContext`; PreToolUse-Task → `updatedInput` prepend. Record `topics[sig] = {at_total, at_trigger}`, `recalls_done += 1`.

Pre- and Post- hooks share the state file, so a `Task` recalled in the PreToolUse path marks its signature; the matching PostToolUse sees it fresh and skips (no double-inject).

---

## Query windowing (handle Hindsight's 500-token query cap)

Hindsight rejects recall queries `> 500` tokens (HTTP 400). Edit/Write content and Task prompts can be far longer, so we extract a `<500`-token window biased toward **fresh** material (keywords whose signature hasn't been recalled this session), so each successive tool call surfaces *new* topics and cumulative coverage improves across long writes.

Algorithm (`query_window.py`), given `content`, the keyword list, and the recalled-topic set:
1. Find n8n keyword occurrences in `content`. Classify each as **fresh** (signature not yet recalled) or **stale**.
2. If no fresh keyword → no new topic; return empty (decision logic will skip).
3. Find the **first fresh** keyword's offset.
   - If no stale keyword precedes it (content is all-fresh from the start) → window start = 0.
   - Else → back up to the **last break before that offset** (newline, then sentence punctuation `.?!`, then whitespace) so the window opens with the full sentence/paragraph containing the fresh keyword.
4. Window = `content[start : start + CHAR_BUDGET]`, where `CHAR_BUDGET ≈ 1600` chars (~4 chars/token, conservative under the 500-token cap).
5. Return the window **and** a boolean `more_fresh_after` = (any fresh keyword occurs at an offset `>= start + CHAR_BUDGET`).

**Truncation note:** when the query was windowed (content longer than the budget) **and** `more_fresh_after` is true, append one line to the injected context:
`note: this recall covered the first new n8n topic in your edit; the query was capped at 500 tokens, so other topics you just wrote may be uncovered — run a manual recall (or they'll be picked up on a later tool call).`

---

## Recall + output

- `do_recall(query, budget, max_tokens)` sends `{"query", "budget", "max_tokens", "include": {"source_facts": {}}}` to `public/recall`.
- Defaults for the backstop: `budget = backstopRecallBudget` (default `high`), `max_tokens = backstopRecallMaxTokens` (default `8000`).
- Rendered by `format_results.py` 0.3.3 → `<result>` XML with synthesis labels, source-fact engagement, fetch nudge.
- Header marked **"context refresh"** so the agent knows it's a mid-turn injection: `*** n8n Knowledge Base — context refresh (after <tool>) ***`.

### Output wrapping
- **Backstop PostToolUse:** `format_results.py` emits hook JSON with `hookEventName: "PostToolUse"`, `additionalContext: <block>`.
- **Backstop PreToolUse-Task:** the script reads the bare `<result>` block (CLI mode), and returns `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": { ...originalInput, "prompt": "<block>\n\n" + originalPrompt }}}`.
- **Manual skill / `recall-cli.sh`:** bare `<result>` block (no hook wrapper).

---

## Session state (`backstop_state.py`)

- File: `${TMPDIR:-/tmp}/n8n-knowledge-backstop/<session_id>.json`
- Schema: `{ "total_calls": int, "trigger_calls": int, "recalls_done": int, "topics": { "<sig>": {"at_total": int, "at_trigger": int} } }`
- PostToolUse `*` increments `total_calls` on every tool call; `trigger_calls` only on Edit/Write/Task. Tool calls serialize per session → no concurrent writes. Missing/corrupt file → treat as empty state.
- `session_id` comes from the hook stdin payload. Stale files are harmless (per-session); a best-effort prune of files older than N days may be added later (not required for v1).

---

## Config (`plugin.json` `userConfig`)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `enableBackstopRecall` | bool | `true` | Master switch for the backstop. |
| `backstopRecallCap` | int | `4` | Max backstop recalls per session. |
| `backstopRecallMaxTokens` | int | `8000` | Returned-context size cap. |
| `backstopRecallBudget` | enum(low,mid,high) | `high` | Hindsight TEMPR effort tier. |
| `enableSubagentInjection` | bool | `false` | Enable PreToolUse-Task `updatedInput` (after verification). |
| `triggerKeywords` | string | (unset → built-in) | Comma list of broad trigger keywords. Supports the `DEFAULTS` sentinel. |

### `triggerKeywords` semantics
- Unset → built-in defaults: `workflow, node, trigger, webhook, credential, expression, execution`.
- `DEFAULTS` token expands to the built-in list inline. Examples:
  - `triggerKeywords: "DEFAULTS, mynode, mytrigger"` → defaults **plus** additions.
  - `triggerKeywords: "workflow, node, mything"` → exactly that (replace).
  - Reset = clear the override or include `DEFAULTS`.
- README documents this and lists the current defaults.

---

## Error handling (never block)

Absolute rule (lesson from the claude-slack lockout): a backstop must never block or break a tool call.
- Any failure — malformed stdin, missing/corrupt state file, recall HTTP error/timeout, query-too-long, formatting error — results in **exit 0 with no injection**.
- Short hook timeout (8s) so a slow recall can't stall the agent (`do_recall` already uses a curl timeout; the hook adds its own guard).
- PostToolUse non-zero is non-blocking by design, but we still exit 0 cleanly. PreToolUse must especially never return a deny/block — on any error it returns empty (allow, unchanged input).

---

## Testing (TDD, existing bash harness `tests/*.sh`)

- **Query windowing** (`query_window.py`): all-fresh → start 0; stale-before-fresh → starts at the break before the first fresh keyword; window length ≤ budget; `more_fresh_after` true when a fresh keyword sits past the window; no-fresh → empty.
- **Decision logic**: new topic → fire; repeat same topic → skip; staleness by `>15 total` → refire; staleness by `>5 trigger` → refire; cap reached → skip; `enableBackstopRecall=false` → no-op.
- **State** (`backstop_state.py`): counters increment correctly; topics record `at_total`/`at_trigger`; corrupt file → empty state.
- **Keyword config**: `DEFAULTS` expansion (extend, replace, unset) in `should_recall`.
- **Unify**: `recall-cli.sh` returns the 0.3.3 `<result>` block (contains `<result`, source engagement) and the skill path produces the same.
- **Output wrapping**: PostToolUse JSON has `hookEventName: PostToolUse` + `additionalContext`; PreToolUse-Task JSON has `updatedInput.prompt` prefixed with the block.
- **Never-block**: malformed stdin and simulated recall failure → exit 0, empty; PreToolUse error path returns no deny.

---

## Build order (for the plan)

1. Shared plumbing: `recall.sh` params, `format_results.py` event-name + bare mode, `recall-cli.sh`, unify `SKILL.md`. (Ships the skill fix independently.)
2. `detect-n8n.sh` configurable keywords + `DEFAULTS` sentinel + README.
3. `backstop_state.py` + `query_window.py` (pure units, fully testable).
4. `backstop-recall.sh` (PostToolUse) + `hooks.json` registration + `plugin.json` config.
5. Verify `updatedInput`; then `backstop-subagent.sh` (PreToolUse-Task), gated off by default.
6. Version bump (incremental, 0.3.x+1) + README + release.
