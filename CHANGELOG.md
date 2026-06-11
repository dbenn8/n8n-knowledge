# Changelog

## Unreleased

### Surgical-edit repair mode (experimental, off by default)
- New `workflowEditStyle` option (`rewrite` | `surgical`). In surgical mode, INVALID validator feedback instructs the model to patch only the failing nodes via a scripted JSON edit and a `!!DRAFT!!` draft marker, instead of regenerating the whole file — cutting output tokens on large-workflow repair rounds by ~99% per fix.
- The validation hook skips files whose first line is `!!DRAFT!!` (work-in-progress drafts): no validation, no budget charge, in all modes.

## 0.3.8 (2026-06-11)

### Validator warnings, smarter context injection, multi-node gotcha recall
- Validator warnings now reach the model: a deduped, capped warnings block renders in both VALID and INVALID feedback (previously computed everywhere, shown nowhere).
- Validator budget counter in feedback ("X of Y calls used") with guidance to batch fixes into one re-write instead of thrashing.
- Node-spec injection on validation errors is deterministic and announced: error nodes ordered by error count, with an explicit "(+N more node schemas omitted)" marker instead of silent arbitrary drops.
- Node detection noise fixes: the bare words "n8n" and "workflow" no longer match the n8n meta-node or Workflow Trigger; event phrasing ("added", "created", "updated"…) upgrades a service to its trigger variant only within the same clause, so action targets stay action nodes.
- Gotcha recall covers all detected node types (round-robin, capped), so the first-detected node can no longer hide other nodes' known bugs.
- Hook resilience: a timed-out semantic recall no longer discards gotcha/node-spec results; malformed recall responses degrade to no-results instead of suppressing the entire injection; hook timeouts raised.
- Local validator suppresses false positives on dynamically loaded option lists and expression values.
- The deep-search skill is now model-invocable and teaches validation-budget discipline.
- Eval harness: per-run isolated config dirs, optional transcript preservation, targeted prompt selection, validator call-count reporting.
- State and debug log moved from /tmp to ~/.cache/n8n-knowledge (0700 dir, 0600 log). Set N8N_KNOWLEDGE_RUNTIME_DIR to override. Old /tmp files are no longer read; delete them manually if present.

## 0.3.7 (2026-06-11)

### Plugin-side validator enrichment & deterministic scoring
- Validator preflight guard with scoring parity: fails closed on cloud/local validator mismatch (both engines must be verifiably equivalent).
- Plugin-side validator enrichment: validator node-spec injection decorates results with resource-locator and IF-filter hints for structured patch targeting.
- Deterministic gotcha scorer: scoring now produces consistent results across plugin-vs-MCP delta annotations, enabling reliable fail-closed validation in mixed-mode environments.
- Logical nodes-table content hash for interpreter-independent serialization: workflow validator state now uses content-addressable hashing instead of binary layout, so plugin and MCP validators agree on structural equivalence.
- Allow mixed validator modes when engines are verifiably equivalent: plugin and MCP validators can coexist and cross-verify if both produce identical node specs and execution signatures.

### Validator result refinement
- Validator results now include delta annotations showing plugin-vs-MCP differences for forensic verification.
- Results summarizer highlights equivalence or divergence with confidence scoring for each delta.

## 0.3.6 (2026-06-10)

### Validator node-spec injection & resource-locator hints
- Validator now injects node-spec data on INVALID results: when workflow validation catches an error, the validator populates the relevant node's resource-locator fields and IF-filter hints to enable structured patch targeting.
- Resource-locator and IF-filter hints in validator spec injection: error results include enough metadata to guide Claude on which node property to fix and what values are allowed.
- Plugin-side validator framework: establishes the pattern for validator enrichment and preflight validation before MCP/cloud execution.

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
