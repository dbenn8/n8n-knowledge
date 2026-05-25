# GitHub Metadata Enrichment + Truncation-Aware Metadata Formatting

## Context

The n8n Knowledge Plugin currently syncs GitHub issues with minimal metadata (URL, number, created_at) and labels as tags. Community posts have richer engagement data (votes, likes, views, solved status) that drives confidence scoring, but GitHub issues lack equivalent signals. Additionally, result text truncation can clip important metadata. This spec addresses both issues across all source types.

## Changes

### 1. Sync script (n8n-hindsight/scripts/sync-github.py)

**Issue scope:** Fetch all open issues + newest closed issues, no filtering by engagement or labels. Sort closed by updated descending, stop when total (open + closed) reaches ~4,500. All issues are valuable — stale, incomplete, won't-fix, and duplicates all provide signal. Hindsight's consolidation synthesizes patterns across them (e.g., frequently reported issues, common workarounds).

**New metadata fields per issue:**

| Field | Source | Stored as |
|---|---|---|
| labels | `issue.labels[].name` | tags: `label:{name}` (already done) |
| reactions_total | `issue.reactions.total_count` | metadata: `reactions_total` |
| of_those_plus1 | `issue.reactions["+1"]` | metadata: `of_those_plus1` |
| state_reason | `issue.state_reason` | metadata: `state_reason` (completed/not_planned/duplicate) |
| comments | `issue.comments` | metadata: `comments` |
| state | `issue.state` | metadata: `state` |
| closed_at | `issue.closed_at` | metadata: `closed_at` |
| author_association | `issue.author_association` | metadata: `author_association` |

**Filtering change:** Remove all engagement-based filtering. Ingest everything up to the ~4,500 cap. The `HIGH_SIGNAL_LABELS` filter and `min_comments` check are removed.

### 2. Confidence scoring (n8n-knowledge/hooks/lib/format_results.py)

**Base score change:** `github_base` lowered from 60 to 49. GitHub issues start at LOW and must earn their way up via engagement or clear signals — same philosophy as community posts.

**GitHub engagement formula:**
```
engagement = reactions_total + (comments * 4)
```

Comments are higher friction than reactions, worth 4x.

**New scoring bonuses (configurable via .local.md):**

| Bonus | Default | Condition |
|---|---|---|
| `clear_signal_bonus` | 25 | Closed with `state_reason` present AND no `Stale` label, OR open with `status:in-linear` or `status:team-assigned` label |
| `author_member_bonus` | 5 | `author_association` is MEMBER or COLLABORATOR |

The old `team_assigned_bonus` (10) is removed — subsumed by `clear_signal_bonus` (25).

**Scoring examples:**

| Scenario | Score | Level |
|---|---|---|
| GitHub, no signals | 49 | LOW |
| GitHub + medium engagement | 59 | MEDIUM |
| GitHub + author member | 54 | MEDIUM |
| GitHub + clear signal | 74 | HIGH |
| GitHub + clear signal + high engagement | 94 | HIGH |

**Reason string update:** Include bucket label, labels, and engagement in the display.
- Before: `GitHub issue, 7 votes, 15 likes`
- After: `GitHub issue, team:ai, in-linear, completed, 5 reactions, 12 comments`

### 3. Truncation-aware metadata formatting (format_results.py)

Applies to ALL source types (docs, github, community).

**Current behavior:** Text truncated at `max_text_length_{level}`, URL prepended before text.

**New behavior:**
1. Build the metadata suffix for each result. GitHub suffixes include a contextual hint based on the issue's resolution bucket (see below). Community and docs use a simpler format.
   - GitHub: `Source: {url} | {bucket_hint} | {label_summary} | {reactions} reactions, {comments} comments`
   - Community: `Source: {url} | {solved_status} | {votes} votes, {likes} likes, {views} views`
   - Docs: `Source: {url}`
2. Calculate metadata suffix length
3. Truncate text body at `max_text_length - metadata_suffix_length` (floor of 300 chars on text body)
4. Append metadata suffix after text

**GitHub resolution buckets and suffix hints:**

| Bucket | Condition | Suffix hint |
|---|---|---|
| Fixed | `state_reason=completed`, no `Stale` label | `fixed — update n8n for the fix` |
| Acknowledged | Open + `status:in-linear` or `status:team-assigned` | `acknowledged — n8n is tracking internally` |
| Won't fix | `state_reason=not_planned` or label `closed:working-as-expected` | `won't fix — search for workarounds` |
| Support redirect | Label `closed:support-issue` | `support issue — check docs or community` |
| Duplicate | `state_reason=duplicate` or label `closed:duplicate` | `duplicate — search for the original issue` |
| Stale | Label `Stale` | `stale — no resolution, but others reported this` |
| Incomplete | Label `closed:incomplete-template` | `incomplete report — problem may be real but unconfirmed` |
| No signal | Open, no team labels, no state_reason | `no resolution yet` |

Bucket detection is checked in priority order (top to bottom). First match wins.

**Output examples:**
```
1. [HIGH — GitHub issue, team:ai, completed] MCP Server Trigger drops headers...
   Source: https://...issues/30926 | fixed — update n8n for the fix | team:ai | 5 reactions, 12 comments

2. [MEDIUM — GitHub issue, acknowledged] OAuth state payload too large...
   Source: https://...issues/30853 | acknowledged — n8n is tracking internally | team:cats | 3 reactions, 8 comments

3. [MEDIUM — GitHub issue, not_planned] Webhook URLs change on reload...
   Source: https://...issues/12345 | won't fix — search for workarounds | 3 reactions, 8 comments

4. [LOW — GitHub issue, stale] Timeout with large payloads...
   Source: https://...issues/99999 | stale — no resolution, but others reported this | 1 reactions, 0 comments

5. [LOW — GitHub issue] New issue with no response yet...
   Source: https://...issues/77777 | no resolution yet | 0 reactions, 0 comments
```

The confidence label and reason on line 1 are the quick-scan signal. The metadata suffix at the end has the full detail, source link, and contextual hint. Text gets the leftover space after reserving room for metadata.

**Note:** `state_reason` is GitHub-specific. Community and docs results do not include it.

### 4. Consolidation directive update (n8n Hindsight bank)

Update the existing directive to also preserve:
- Labels (especially team assignments: team:ai, team:cats, status:in-linear)
- Reaction counts and comment counts
- Open/closed state and state_reason
- Author association (MEMBER vs community reporter)

Add guidance for the consolidator to recognize patterns across related issues — e.g., frequently reported problems that get closed as stale/duplicate/incomplete should be synthesized into observations like "this is a common issue with no official fix" or "multiple users report this; n8n tracks it internally."

This is in addition to the existing directive for URLs, votes, likes, views, and solved status.

### 5. n8n Pulse chatbot (portfolio/app/chat.py)

Verify that the tool calling response passes through the new metadata fields so Claude can cite labels, engagement, and state in its chatbot responses. The raw recall results include all metadata — just need to confirm `max_chars` (18000) leaves room and the synthesis prompt doesn't strip it.

## Files to modify

| File | Change |
|---|---|
| `n8n-hindsight/scripts/sync-github.py` | Add closed issues, new metadata fields, remove filtering |
| `n8n-knowledge/hooks/lib/format_results.py` | github_base to 49, clear_signal_bonus, engagement formula, truncation-aware metadata |
| `n8n-knowledge/.claude/n8n-knowledge.local.md` | Add new config defaults |
| `n8n-knowledge/README.md` | Document new config options |
| `n8n-knowledge/tests/test-recall-format.sh` | Tests for new scoring and metadata formatting |
| `n8n-knowledge/tests/fixtures/recall-response.json` | Update fixture with new metadata fields |
| `n8n-hindsight` consolidation directive | Update via API call |
| `portfolio/app/chat.py` | Verify metadata passthrough (likely no changes needed) |

## Verification

1. Run `sync-github.py --dry-run` to verify new fields are captured
2. Run `bash tests/run-all.sh` — all tests pass including new scoring/formatting tests
3. Test live recall to verify metadata appears in injected context
4. Test n8n Pulse chatbot to verify it cites engagement and labels
5. Verify consolidation directive is active via API