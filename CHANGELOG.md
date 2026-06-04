# Changelog

## 0.3.5

### Backstop recall (mid-turn context injection)
- New `PostToolUse` hook refreshes n8n knowledge-base context **during** an agentic session — after `Edit`/`Write`/`Task`, not just on the user's prompt. Gated, deduped by topic, and capped per session (`backstopRecallCap`, default 4).
- New `PreToolUse`-on-`Task` hook can prepend context into a subagent's prompt. **⚠️ WORK IN PROGRESS — UNTESTED. Ships dormant and disabled** (`enableSubagentInjection=false`). The `updatedInput` mechanism it relies on has not been verified at runtime; do not enable it until a future release confirms it works.
- Smart query windowing keeps the recall query under Hindsight's 500-token cap and anchors it on the first not-yet-recalled keyword, so successive tool calls surface new topics.
- New config: `enableBackstopRecall`, `backstopRecallCap`, `backstopRecallMaxTokens` (default 8000), `backstopRecallBudget` (default `high`), `enableSubagentInjection`.
- All recall consumers (auto-recall hook, backstop, manual skill) now share one rendering path via `recall-cli.sh`; the manual recall skill now returns the same first-class `<result>` format with source metadata.
- `triggerKeywords` is now configurable with a `DEFAULTS` sentinel (extend, replace, or reset the broad-keyword list).
- Hooks never block a tool call: any failure exits cleanly with no injection.

### GitHub issue state
- Every GitHub result is now prefixed with its canonical state — `[OPEN]`, `[CLOSED·completed·DATE]`, `[CLOSED·not_planned·DATE]`, `[CLOSED·duplicate]` — derived from raw `state`/`state_reason`/`closed_at`. Observations inherit the state from their primary source fact.
- Header guidance clarifies state semantics (closed·completed ≈ usually fixed; closed·not_planned ≈ won't be fixed by upgrading) and that versions in result text are the reporter's environment, not the fixed-in version — verify live state before designing around an item.
- Removed the over-asserting legacy phrase "fixed — update n8n for the fix"; `closed·completed` no longer implies a release fix exists (it can be a resolved/duplicate/not-a-bug closure).
- Reconciled the stale-vs-completed contradiction: a `closed·completed` issue carrying a `Stale` label is now framed honestly ("likely auto-closed/abandoned, not necessarily fixed; verify") instead of the contradictory "stale — no resolution".

## 0.3.3
- Source-aware observation scoring: synthesized observations score from their primary source fact's engagement (instead of empty own-metadata), with a raw≥synthesis tie-break.
- Results wrapped in `<result>` tags with a `kind="synthesis|post"` label and a verify-against-sources note.
- Recall requests include `source_facts`; observations surface their cited source post's engagement and URL.
