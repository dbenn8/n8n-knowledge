# Observation Scoring, Synthesis Labeling & Result Boundaries — Design

**Date:** 2026-06-03
**File touched:** `hooks/lib/format_results.py` (single file; additive — raw-result behavior preserved)
**Related tasks:** #73 (this), #74 (retrieval eval — resolves synthesis-vs-raw empirically), #75 (re-ingest GitHub resolutions)

## Problem

The plugin auto-injects recall results into the harness (Claude Code) on every n8n prompt. A result's **confidence level** controls two things:
- **Inclusion:** all HIGH/MEDIUM kept; only the **top-1 LOW** survives (`max_low_results`).
- **Text length:** HIGH = full, MEDIUM = 800 chars, LOW = 300 chars.

`score_result()` reads each result's **own** metadata. But synthesized **observations** carry *empty* own-metadata (their engagement lives on their source posts, returned in `source_facts`). So observations always score on base alone — community 40 / github 49 = **LOW** — regardless of how strong the threads they distill. Result: the best cross-thread answer gets **truncated to 300 chars or dropped**, while a single raw post with a few likes outranks it.

Two secondary problems:
- **Weak result boundaries.** Results are joined with a single `\n`, no separator (`format_results.py:362,366`). With observations running 550–1,650 chars (vs ~200 for raw posts) and containing their own newlines, the harness can misattribute a warning or metric to the wrong result.
- **No synthesis signal.** The harness can't tell a machine-distilled observation from a verbatim source post, so it can't weigh them on conflict.

## Goals

1. Stop strong observations being unfairly scored LOW / clipped (fairness — **not** a boost).
2. Keep the ground-truth solved source at-or-above its own synthesis (raw-first).
3. Give the harness unambiguous result boundaries and a synthesis flag, so it can attribute and trust correctly.
4. Let the harness pull the full thread when it needs it (the full conversation isn't in the bank — see Non-Goals).

## Non-Goals / Explicitly Out of Scope

- **No synthesis text cap** beyond the existing per-level truncation — depth comes from fetching the source, not from cramming.
- **No synthesis boost** above equivalent raw sources.
- **No new scoring constants** — reuse the already-tuned thresholds in `DEFAULTS`.
- **No instant Hindsight/plugin pre-fetch (b2).** The bank stores only the question for GitHub (`sync-github.py` content = title+body, no comments) and question+accepted-answer for community (`sync-community.py`, no intermediate replies). The full "what was tried / worked / why" lives only at the live URL. Pulling Hindsight's stored doc returns the question (GitHub) or content already in recall (community) — so the fetch path is the **live URL via the harness (b1)**, not Hindsight.
- **No machine-named synthesizer label** (e.g. "DeepSeek V4 Flash") — the consolidation model drifts and isn't on the observation; a generic flag is correct. (Stamping the model at consolidation time is a possible future Hindsight-side task.)

## Design

### 1. Fairness scoring

When a result is an observation (`r.get("type") == "observation"`, i.e. no own `metadata.url`), score it using the engagement of its **primary source fact** — the same fact already resolved for display via `resolve_source_facts()`. Build a synthetic `meta`/`tags` from that source fact and run the **existing** `score_result()` logic unchanged.

- **Primary** (first resolved source), not strongest or sum: conservative, raw-first, and consistent with the single citation displayed. If the eval (#74) shows multi-source observations under-rank, revisit then.
- Raw results (own metadata present): **unchanged.**

Effect: an observation built on a 3,062-view / 13-like / solved thread scores like that thread (→ HIGH, full text); an observation built on a dead thread stays LOW.

### 2. Tie-break: raw ≥ synthesis

Where results are selected/sorted by score (the top-1 LOW slot today), a raw result outranks an observation of **equal** score. Implemented as a sort key `(score, is_raw)` so the ground-truth source is never displaced by its own synthesis. Recall order is otherwise preserved (it encodes relevance; Hindsight already returns observations before raw facts).

### 3. Result boundaries + synthesis label (output format)

Wrap **each result** in `<result>…</result>` tags with **prose interior** (no inner `<cite>`/`<note>` tags — they add ~no value since the harness reads prose attributes fine and we never machine-parse its output; the value is the outer boundary + explicit close marker).

**The full result text goes inside the tags verbatim, as plain prose** — exactly the `text` that recall returns (an observation is typically several sentences, up to ~1,650 chars), followed by plain `sources:`/`source:` and (for synthesis) `note:` lines. None of the interior content is tagged. The examples below show the *complete* text, not abbreviations:

```
<result n="1" kind="synthesis" confidence="HIGH" sources="11">
MCP Server on n8n Cloud failing to connect from Claude Desktop is most often caused by gzipped responses from the MCP endpoint or by an OAuth-vs-access-token auth mismatch. The connection succeeds from Cursor and the MCP inspector but errors in Claude Desktop with "Could not connect to your MCP server". The fix that resolved it across multiple reports was to disable gzip on the MCP responses (and, for the OAuth path, to use the access-token connector rather than the OAuth custom connector, which has a known Claude-side bug where the Bearer token isn't attached to follow-up requests).
sources: https://community.n8n.io/t/mcp-server-n8n-cloud-not-working-in-claude-desktop/118647 (solved, 13 likes, 3062 views) | also: https://community.n8n.io/t/claude-ai-mcp-connector-failing-to-connect-to-my-self-hosted-n8n/ (solved)
note: machine-distilled — verify against the sources above; prefer them on conflict; fetch a source URL for the full thread (what was tried, what worked, why).
</result>
<result n="2" kind="post" confidence="HIGH" source="community">
User Martijn has a simple MCP server on n8n Cloud that works with Cursor and the MCP inspector but cannot connect from Claude Desktop, returning a supergate error. The accepted answer was that the n8n Cloud MCP endpoint was returning gzipped data; switching the server to send uncompressed responses let Claude Desktop connect successfully.
source: https://community.n8n.io/t/claude-desktop-connection-supergate-error/111674 | solved | 6 likes, 563 views
</result>
```

Key points for the interior, to be unambiguous for implementation:
- The text between the open tag and the `sources:`/`source:` line is the **verbatim recall `text`** (after the existing per-level truncation), as prose — no inner tags, no reformatting.
- `sources:` (synthesis, may list several) vs `source:` (single-source post) is the existing suffix content, just relocated inside the tag.
- The `note:` line appears **only** for `kind="synthesis"`.

- Open-tag attributes carry the few machine-useful routing signals: `n`, `kind` (`synthesis`|`post`), `confidence` (HIGH|MEDIUM|LOW), and either `sources="N"` (synthesis) or `source="docs|github|community"` (post).
- Interior stays prose: the result text, a `sources:`/`source:` line (URL + solved/engagement), and — for synthesis only — the `note:` verify/fetch line.
- The existing `*** n8n Knowledge Base ***` header/legend/SAFETY block is retained and updated to explain the schema briefly: results are delimited by `<result>` tags; `kind="synthesis"` is machine-distilled (prefer cited sources on conflict); fetch a source URL for the full thread when needed.
- Token cost ≈ the lean hybrid (just an open/close line per result); not the ~10–20% of full XML.

### 4. b1 fetch nudge

Add one line to the injected-context header inviting the harness to fetch the source URL for high-confidence / solved items when it needs the full thread (what was tried, what worked, why). URLs already render (prior display fix). Zero cost until the harness chooses to fetch.

## Data Flow

`recall.sh` (already sends `include.source_facts`) → response → `format_results.py`:
`load_config` → `score_result` (now source-aware for observations) → filter (non-LOW + top-1 LOW, tie-break raw≥synthesis) → per-result render inside `<result>` tags with prose interior → join (no extra `\n` separator needed; tags delimit) → header + SAFETY + body.

## Testing

- **Fix** the 3 pre-existing stale tests that monkeypatch the removed `enrich_missing_urls` (in `tests/test-recall-format.sh`) to assert the current `source_facts` behavior instead.
- **Add:**
  - observation with strong primary source → scored MEDIUM/HIGH (not LOW); with weak source → LOW.
  - tie-break: raw result and observation at equal score → raw selected/ordered first.
  - output: each result wrapped in `<result …>…</result>`; `kind="synthesis"` present with `sources="N"` and the `note:` verify/fetch line; raw post has `kind="post"` and no note.
  - header contains the fetch-nudge line and schema explanation.
- Run `bash tests/run-all.sh` — expect all green (no remaining stale failures).

## Risks

- **Modest context increase:** promoted observations un-clip (300 → ≤800/full). Bounded by reusing the noise-tuned thresholds + tie-break + raw-first stance. The eval (#74) will measure whether this helps or hurts answer accuracy and at what token cost.
- **Format change** is visible to the harness; the header schema note mitigates any parsing ambiguity.
