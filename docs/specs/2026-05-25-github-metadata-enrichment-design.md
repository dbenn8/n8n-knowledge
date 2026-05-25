# GitHub Metadata Enrichment + Truncation-Aware Metadata Formatting

## Context

The n8n Knowledge Plugin currently syncs GitHub issues with minimal metadata (URL, number, created_at) and labels as tags. Community posts have richer engagement data (votes, likes, views, solved status) that drives confidence scoring, but GitHub issues lack equivalent signals. Additionally, result text truncation can clip important metadata. This spec addresses both issues across all source types.

## Changes

### 1. Sync script (n8n-hindsight/scripts/sync-github.py)

**Issue scope:** Fetch open + closed issues. Fresh issues (< 60 days old) always included. Older closed issues included if they have 2+ comments, any reactions, or labels indicating team assignment (status:in-linear, status:team-assigned). Target ~4,500 total issues (open + recent/high-signal closed).

**New metadata fields per issue:**

| Field | Source | Stored as |
|---|---|---|
| labels | `issue.labels[].name` | tags: `label:{name}` (already done) |
| reactions_total | `issue.reactions.total_count` | metadata: `reactions_total` |
| reactions_plus1 | `issue.reactions["+1"]` | metadata: `reactions_plus1` |
| comments | `issue.comments` | metadata: `comments` |
| state | `issue.state` | metadata: `state` |
| closed_at | `issue.closed_at` | metadata: `closed_at` |
| author_association | `issue.author_association` | metadata: `author_association` |

**Filtering change:** Remove `HIGH_SIGNAL_LABELS` filter for fresh issues (< 60 days). Older issues still filtered by engagement or team-assignment labels.

### 2. Confidence scoring (n8n-knowledge/hooks/lib/format_results.py)

**GitHub engagement formula:**
```
engagement = reactions_total + (comments * 4)
```

Comments are higher friction than reactions, worth 4x.

**New scoring bonuses (configurable via .local.md):**

| Bonus | Default | Condition |
|---|---|---|
| `team_assigned_bonus` | 10 | Issue has `status:in-linear` or `status:team-assigned` label |
| `author_member_bonus` | 5 | `author_association` is MEMBER or COLLABORATOR |

These stack with existing thresholds. A github issue (base 60) with team assignment (+10) and high engagement (+20) scores 90 = HIGH.

Closed issues with a resolution treated like solved community posts: `+solved_bonus` (25).

**Reason string update:** Include labels and engagement in the display.
- Before: `GitHub issue, 7 votes, 15 likes`
- After: `GitHub issue, team:ai, in-linear, 5 reactions, 12 comments, closed`

### 3. Truncation-aware metadata formatting (format_results.py)

Applies to ALL source types (docs, github, community).

**Current behavior:** Text truncated at `max_text_length_{level}`, URL prepended before text.

**New behavior:**
1. Build the metadata suffix for each result:
   - GitHub: `Source: {url} | {state} | {label_summary} | {reactions} reactions, {comments} comments`
   - Community: `Source: {url} | {solved_status} | {votes} votes, {likes} likes, {views} views`
   - Docs: `Source: {url}`
2. Calculate metadata suffix length
3. Truncate text body at `max_text_length - metadata_suffix_length` (floor of 300 chars on text body)
4. Append metadata suffix after text

**Output format:**
```
1. [HIGH — GitHub issue, team:ai, in-linear, closed] MCP Server Trigger discards
   incoming request headers, unlike Webhook Trigger...
   Source: https://github.com/n8n-io/n8n/issues/30926 | closed | team:ai, in-linear | 5 reactions, 12 comments
```

The confidence label and reason on line 1 are the quick-scan signal. The metadata block at the end has the full detail and source link. Text gets the leftover space after reserving room for metadata.

### 4. Consolidation directive update (n8n Hindsight bank)

Update the existing directive to also preserve:
- Labels (especially team assignments: team:ai, team:cats, status:in-linear)
- Reaction counts and comment counts
- Open/closed state
- Author association (MEMBER vs community reporter)

This is in addition to the existing directive for URLs, votes, likes, views, and solved status.

### 5. n8n Pulse chatbot (portfolio/app/chat.py)

Verify that the tool calling response passes through the new metadata fields so Claude can cite labels, engagement, and state in its chatbot responses. The raw recall results include all metadata — just need to confirm `max_chars` (18000) leaves room and the synthesis prompt doesn't strip it.

## Files to modify

| File | Change |
|---|---|
| `n8n-hindsight/scripts/sync-github.py` | Add closed issues, new metadata fields, update filtering |
| `n8n-knowledge/hooks/lib/format_results.py` | New engagement formula, label bonuses, truncation-aware metadata |
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
