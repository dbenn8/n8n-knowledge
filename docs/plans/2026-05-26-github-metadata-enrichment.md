# GitHub Metadata Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich GitHub issue sync with full metadata, update confidence scoring with github_base 49 and clear_signal_bonus 25, and implement truncation-aware metadata suffixes for all source types.

**Architecture:** The sync script (n8n-hindsight) fetches all open + newest closed issues up to ~4,500, capturing labels, reactions, comments, state, state_reason, and author_association. The plugin's format_results.py (n8n-knowledge) scores GitHub issues using a new engagement formula and resolution buckets, then formats all results with metadata suffixes that survive text truncation.

**Tech Stack:** Python 3, bash, curl, Hindsight API

---

### Task 1: Update sync-github.py — fetch closed issues and capture new metadata

**Files:**
- Modify: `n8n-hindsight/scripts/sync-github.py`

- [ ] **Step 1: Write the test script for new metadata capture**

Create a test that verifies format_item captures all new fields:

```bash
# Run from n8n-hindsight repo
python3 -c "
from scripts import sync_github  # won't work as module, test inline instead
"
```

Since sync-github.py is a standalone script without a test harness, we'll test via `--dry-run` after changes. Skip to implementation.

- [ ] **Step 2: Add fetch_closed_issues function**

Add after the existing `fetch_issues` function in `n8n-hindsight/scripts/sync-github.py`:

```python
def fetch_closed_issues(target_total, open_count):
    """Fetch newest closed issues until we reach target_total combined with open."""
    remaining = target_total - open_count
    if remaining <= 0:
        return []
    print(f"Fetching up to {remaining} closed issues...")
    params = {"state": "closed", "per_page": "100", "sort": "updated", "direction": "desc"}
    all_items = []
    page = 1
    while len(all_items) < remaining:
        paged_params = dict(params, page=str(page))
        batch = github_api(f"repos/{REPO}/issues", paged_params)
        issues = [i for i in batch if "pull_request" not in i]
        if not issues:
            break
        all_items.extend(issues)
        page += 1
    result = all_items[:remaining]
    print(f"  Fetched {len(result)} closed issues")
    return result
```

- [ ] **Step 3: Update format_item to capture new metadata**

Replace the existing `format_item` function:

```python
def format_item(item, item_type):
    number = item["number"]
    title = item["title"]
    body = (item.get("body") or "")[:4000]
    url = item["html_url"]
    labels = [l["name"] for l in item.get("labels", [])]
    created = item.get("created_at", "")
    reactions = item.get("reactions", {})
    state = item.get("state", "open")

    content = f"GitHub {item_type} #{number}: {title}\n\n{body}".strip()
    if len(content) > 5000:
        content = content[:5000] + "..."

    context = f"github {item_type} #{number} - {title} ({url})"
    tags = [f"type:github-{item_type}", "source:github"]
    for label in labels:
        tags.append(f"label:{label}")
    if state == "closed":
        tags.append("state:closed")

    metadata = {
        "url": url,
        "number": str(number),
        "created_at": created,
        "reactions_total": str(reactions.get("total_count", 0)),
        "of_those_plus1": str(reactions.get("+1", 0)),
        "comments": str(item.get("comments", 0)),
        "state": state,
        "author_association": item.get("author_association", "NONE"),
    }
    if item.get("state_reason"):
        metadata["state_reason"] = item["state_reason"]
    if item.get("closed_at"):
        metadata["closed_at"] = item["closed_at"]

    return {
        "content": content,
        "context": context,
        "tags": tags,
        "metadata": metadata,
    }
```

- [ ] **Step 4: Update main() to remove filtering and add closed issues**

Replace the fetch/filter/format section in `main()`:

```python
    # Fetch
    TARGET_TOTAL = 4500
    issues = fetch_issues(since)
    closed_issues = [] if since else fetch_closed_issues(TARGET_TOTAL, len(issues))
    prs = fetch_prs(since)

    all_issues = issues + closed_issues
    print(f"\nTotal: {len(issues)} open + {len(closed_issues)} closed issues, {len(prs)} PRs")

    # Format (no filtering — ingest everything, let consolidation handle it)
    formatted = []
    for issue in all_issues:
        formatted.append(format_item(issue, "issue"))
    for pr in prs:
        formatted.append(format_item(pr, "pr"))

    print(f"Total to ingest: {len(formatted)}")
```

Remove the `HIGH_SIGNAL_LABELS` constant and the `filter_high_signal` function entirely.

- [ ] **Step 5: Test with --dry-run**

Run: `python3 scripts/sync-github.py --dry-run`
Expected: Shows open + closed issue counts, no filtering messages, all new metadata fields present.

- [ ] **Step 6: Commit**

```bash
git add scripts/sync-github.py
git commit -m "Fetch open+closed issues up to 4500, capture reactions/comments/state/author metadata"
```

---

### Task 2: Update test fixtures with new metadata fields

**Files:**
- Modify: `n8n-knowledge/tests/fixtures/recall-response.json`
- Modify: `n8n-knowledge/tests/fixtures/recall-response-consolidated.json`

- [ ] **Step 1: Update main fixture with GitHub metadata**

Replace `n8n-knowledge/tests/fixtures/recall-response.json`:

```json
{
  "results": [
    {
      "id": "aaa",
      "text": "To install n8n with Docker Compose, create a docker-compose.yml file with the n8n service and a PostgreSQL database.",
      "tags": ["type:docs", "source:docs"],
      "metadata": {"url": "https://docs.n8n.io/hosting/installation/server-setups/docker-compose/", "section": "hosting"}
    },
    {
      "id": "bbb",
      "text": "MCP Server Trigger discards incoming request headers, unlike Webhook Trigger, preventing valid MCP server use cases.",
      "tags": ["type:github-issue", "source:github", "label:status:in-linear", "label:team:ai"],
      "metadata": {
        "url": "https://github.com/n8n-io/n8n/issues/12345",
        "reactions_total": "15",
        "of_those_plus1": "12",
        "comments": "8",
        "state": "open",
        "author_association": "NONE"
      }
    },
    {
      "id": "ccc",
      "text": "User reports webhook from VAPI to n8n not working despite correct setup.",
      "tags": ["type:community-post", "source:discourse", "category:questions", "outcome:solved"],
      "metadata": {"url": "https://community.n8n.io/t/webhook-to-vapi/119383", "views": "569", "like_count": "3", "has_accepted_answer": "True"}
    },
    {
      "id": "ddd",
      "text": "Built an automated invoice processing workflow using n8n with Google Sheets and Stripe integration.",
      "tags": ["type:community-post", "source:discourse", "category:built-with-n8n", "outcome:unsolved"],
      "metadata": {"url": "https://community.n8n.io/t/invoice-workflow/98765", "views": "1200", "like_count": "22", "vote_count": "8"}
    },
    {
      "id": "eee",
      "text": "New user asking about how to connect n8n to their local database.",
      "tags": ["type:community-post", "source:discourse", "category:questions", "outcome:unsolved"],
      "metadata": {"url": "https://community.n8n.io/t/connect-db/55555", "views": "45", "like_count": "0"}
    },
    {
      "id": "fff",
      "text": "Job finished leaks EventEmitter listeners in queue mode.",
      "tags": ["type:github-issue", "source:github", "label:Stale"],
      "metadata": {
        "url": "https://github.com/n8n-io/n8n/issues/99999",
        "reactions_total": "1",
        "of_those_plus1": "1",
        "comments": "2",
        "state": "closed",
        "state_reason": "completed",
        "author_association": "NONE"
      }
    },
    {
      "id": "ggg",
      "text": "Webhook endpoint changes not planned for current architecture.",
      "tags": ["type:github-issue", "source:github"],
      "metadata": {
        "url": "https://github.com/n8n-io/n8n/issues/88888",
        "reactions_total": "3",
        "of_those_plus1": "2",
        "comments": "4",
        "state": "closed",
        "state_reason": "not_planned",
        "author_association": "MEMBER"
      }
    },
    {
      "id": "hhh",
      "text": "No response issue with zero engagement.",
      "tags": ["type:github-issue", "source:github"],
      "metadata": {
        "url": "https://github.com/n8n-io/n8n/issues/77777",
        "reactions_total": "0",
        "of_those_plus1": "0",
        "comments": "0",
        "state": "open",
        "author_association": "NONE"
      }
    }
  ]
}
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/
git commit -m "Update test fixtures with GitHub metadata fields"
```

---

### Task 3: Update score_result for github_base 49, clear_signal_bonus, engagement formula

**Files:**
- Modify: `n8n-knowledge/hooks/lib/format_results.py`
- Modify: `n8n-knowledge/tests/test-recall-format.sh`

- [ ] **Step 1: Write failing tests for new GitHub scoring**

Add to `tests/test-recall-format.sh` before the empty results test:

```bash
# GitHub with in-linear label (clear signal) should be HIGH
assert_contains "github with in-linear is HIGH" "HIGH.*GitHub issue" "$context"

# GitHub with no signals should be LOW
assert_contains "github no signals is LOW" "LOW.*GitHub issue" "$context"

# GitHub closed with Stale label should NOT get clear_signal_bonus
# (Stale + completed = no clear signal, stays at base 49 + engagement only)
assert_contains "stale github is LOW" "LOW.*GitHub issue.*stale" "$context"

# GitHub closed not_planned with MEMBER author should be MEDIUM or HIGH
assert_not_contains "not_planned member is not LOW" "LOW.*not_planned" "$context"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `bash tests/run-all.sh`
Expected: New assertions fail because scoring still uses github_base 60 and has no clear_signal_bonus.

- [ ] **Step 3: Update DEFAULTS**

In `format_results.py`, update DEFAULTS:

```python
DEFAULTS = {
    "high_threshold": 70,
    "medium_threshold": 50,
    "docs_base": 80,
    "github_base": 49,
    "community_base": 40,
    "solved_bonus": 25,
    "clear_signal_bonus": 25,
    "author_member_bonus": 5,
    "high_engagement_threshold": 10,
    "high_engagement_bonus": 20,
    "medium_engagement_threshold": 3,
    "medium_engagement_bonus": 10,
    "high_views_threshold": 500,
    "views_bonus": 5,
    "max_results": 5,
    "max_low_results": 1,
    "max_text_length_high": -1,
    "max_text_length_medium": 800,
    "max_text_length_low": 300,
}
```

- [ ] **Step 4: Update score_result for GitHub-specific scoring**

Replace the scoring logic in `score_result`:

```python
def score_result(r, cfg):
    tags = r.get("tags", [])
    meta = r.get("metadata", {}) or {}
    tag_set = set(tags)

    source = "unknown"
    if any("source:docs" in t for t in tags):
        source = "docs"
    elif any("source:github" in t for t in tags):
        source = "github"
    elif any("source:discourse" in t for t in tags):
        source = "community"

    # Base score
    if source == "docs":
        score = cfg["docs_base"]
    elif source == "github":
        score = cfg["github_base"]
    else:
        score = cfg["community_base"]

    # Community scoring (unchanged)
    if source == "community":
        solved = "outcome:solved" in tag_set
        votes = int(meta.get("vote_count", 0))
        likes = int(meta.get("like_count", 0))
        views = int(meta.get("views", 0))
        engagement = votes + likes

        if solved:
            score += cfg["solved_bonus"]
        if engagement >= cfg["high_engagement_threshold"]:
            score += cfg["high_engagement_bonus"]
        elif engagement >= cfg["medium_engagement_threshold"]:
            score += cfg["medium_engagement_bonus"]
        if views >= cfg["high_views_threshold"]:
            score += cfg["views_bonus"]

    # GitHub scoring (new)
    elif source == "github":
        reactions = int(meta.get("reactions_total", 0))
        comments = int(meta.get("comments", 0))
        engagement = reactions + (comments * 4)
        state = meta.get("state", "open")
        state_reason = meta.get("state_reason", "")
        author_assoc = meta.get("author_association", "NONE")
        has_stale = any("label:Stale" in t for t in tags)

        # Clear signal: closed with state_reason (not Stale) OR open with team labels
        has_team_label = any("label:status:in-linear" in t or "label:status:team-assigned" in t for t in tags)
        if has_team_label or (state == "closed" and state_reason and not has_stale):
            score += cfg["clear_signal_bonus"]

        if author_assoc in ("MEMBER", "COLLABORATOR"):
            score += cfg["author_member_bonus"]

        if engagement >= cfg["high_engagement_threshold"]:
            score += cfg["high_engagement_bonus"]
        elif engagement >= cfg["medium_engagement_threshold"]:
            score += cfg["medium_engagement_bonus"]

    # Level
    if score >= cfg["high_threshold"]:
        level = "HIGH"
    elif score >= cfg["medium_threshold"]:
        level = "MEDIUM"
    else:
        level = "LOW"

    # Reason string
    parts = []
    if source == "docs":
        parts.append("Official docs")
    elif source == "github":
        parts.append("GitHub issue")
        # Add team labels
        for t in tags:
            if t.startswith("label:team:"):
                parts.append(t.replace("label:", ""))
            elif t in ("label:status:in-linear", "label:status:team-assigned"):
                parts.append(t.replace("label:", ""))
        state_reason = meta.get("state_reason", "")
        if state_reason:
            parts.append(state_reason)
        reactions = int(meta.get("reactions_total", 0))
        comments = int(meta.get("comments", 0))
        if reactions:
            parts.append(f"{reactions} reactions")
        if comments:
            parts.append(f"{comments} comments")
    else:
        parts.append("Community")
        category = ""
        for t in tags:
            if t.startswith("category:"):
                category = t.replace("category:", "")
                break
        if category:
            parts.append(category.replace("-", " "))
        solved = "outcome:solved" in tag_set
        if solved:
            parts.append("solved")
        votes = int(meta.get("vote_count", 0))
        likes = int(meta.get("like_count", 0))
        views = int(meta.get("views", 0))
        if votes:
            parts.append(f"{votes} votes")
        if likes:
            parts.append(f"{likes} likes")
        if views >= 100:
            parts.append(f"{views} views")

    return level, ", ".join(parts), score
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `bash tests/run-all.sh`
Expected: All tests pass including new scoring assertions.

- [ ] **Step 6: Commit**

```bash
git add hooks/lib/format_results.py tests/test-recall-format.sh
git commit -m "GitHub scoring: base 49, clear_signal_bonus 25, engagement = reactions + comments*4"
```

---

### Task 4: Implement resolution buckets and truncation-aware metadata suffixes

**Files:**
- Modify: `n8n-knowledge/hooks/lib/format_results.py`
- Modify: `n8n-knowledge/tests/test-recall-format.sh`

- [ ] **Step 1: Write failing tests for metadata suffixes**

Add to `tests/test-recall-format.sh`:

```bash
# GitHub result with in-linear should show acknowledged hint
assert_contains "acknowledged hint in suffix" "acknowledged" "$context"

# GitHub closed not_planned should show won't fix hint
assert_contains "wont fix hint in suffix" "won.t fix" "$context"

# Stale github should show stale hint
assert_contains "stale hint in suffix" "stale.*no resolution" "$context"

# No-signal github should show no resolution
assert_contains "no resolution hint" "no resolution yet" "$context"

# Community result should show votes/likes/views in suffix
assert_contains "community suffix has views" "views" "$context"

# Docs result should have Source URL in suffix
assert_contains "docs suffix has source" "Source.*docs.n8n.io" "$context"

# Metadata suffix appears AFTER text, not before
# (test that Source: line comes after the text line)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `bash tests/run-all.sh`
Expected: Suffix-related assertions fail.

- [ ] **Step 3: Add get_github_bucket function**

Add to `format_results.py` after `extract_url`:

```python
GITHUB_BUCKETS = [
    # (condition_fn, suffix_hint)  — checked in priority order, first match wins
]

def get_github_bucket(r):
    """Determine resolution bucket for a GitHub issue. Returns suffix hint string."""
    tags = r.get("tags", [])
    meta = r.get("metadata", {}) or {}
    tag_set = set(tags)
    state = meta.get("state", "open")
    state_reason = meta.get("state_reason", "")

    if state == "closed" and state_reason == "completed" and "label:Stale" not in tag_set:
        return "fixed — update n8n for the fix"
    if state == "open" and any(t in tag_set for t in ("label:status:in-linear", "label:status:team-assigned")):
        return "acknowledged — n8n is tracking internally"
    if state_reason == "not_planned" or "label:closed:working-as-expected" in tag_set:
        return "won't fix — search for workarounds"
    if "label:closed:support-issue" in tag_set:
        return "support issue — check docs or community"
    if state_reason == "duplicate" or "label:closed:duplicate" in tag_set:
        return "duplicate — search for the original issue"
    if "label:Stale" in tag_set:
        return "stale — no resolution, but others reported this"
    if "label:closed:incomplete-template" in tag_set:
        return "incomplete report — problem may be real but unconfirmed"
    return "no resolution yet"
```

- [ ] **Step 4: Add build_metadata_suffix function**

Add to `format_results.py`:

```python
def build_metadata_suffix(r, url):
    """Build the metadata suffix line for a result. Varies by source type."""
    tags = r.get("tags", [])
    meta = r.get("metadata", {}) or {}
    source = "unknown"
    if any("source:docs" in t for t in tags):
        source = "docs"
    elif any("source:github" in t for t in tags):
        source = "github"
    elif any("source:discourse" in t for t in tags):
        source = "community"

    parts = []
    if url:
        parts.append(f"Source: {url}")

    if source == "github":
        bucket_hint = get_github_bucket(r)
        parts.append(bucket_hint)
        # Team labels
        team_labels = [t.replace("label:", "") for t in tags if t.startswith("label:team:") or t in ("label:status:in-linear", "label:status:team-assigned")]
        if team_labels:
            parts.append(", ".join(team_labels))
        reactions = meta.get("reactions_total", "0")
        comments = meta.get("comments", "0")
        parts.append(f"{reactions} reactions, {comments} comments")

    elif source == "community":
        solved = "outcome:solved" in set(tags)
        parts.append("solved" if solved else "unsolved")
        votes = meta.get("vote_count", "0")
        likes = meta.get("like_count", "0")
        views = meta.get("views", "0")
        parts.append(f"{votes} votes, {likes} likes, {views} views")

    if not parts:
        return ""
    return "   " + " | ".join(parts)
```

- [ ] **Step 5: Update format_results to use truncation-aware suffixes**

Replace the formatting loop in `format_results`:

```python
    for i, (r, level, reason, _) in enumerate(filtered, 1):
        text = r.get("text", "").strip()
        url = extract_url(r) or enriched_urls.get(i - 1, "")
        if not url and (i - 1) in enrichment_failed:
            url = ""  # will show unavailable hint via bucket

        # Build metadata suffix
        suffix = build_metadata_suffix(r, url)
        if not url and (i - 1) in enrichment_failed:
            # Replace Source line with unavailable hint
            suffix = "   Source unavailable — use manual recall to find the original"

        # Truncation-aware: reserve space for suffix, floor 300 chars for text
        length_key = f"max_text_length_{level.lower()}"
        max_len = cfg.get(length_key, -1)
        if max_len >= 0:
            max_len = max(max_len, 300)
            text_budget = max(300, max_len - len(suffix))
            if len(text) > text_budget:
                text = text[:text_budget] + "..."

        entry = f"{i}. [{level} — {reason}] {text}"
        if suffix:
            entry += f"\n{suffix}"
        lines.append(entry)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `bash tests/run-all.sh`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add hooks/lib/format_results.py tests/test-recall-format.sh
git commit -m "Add resolution bucket hints and truncation-aware metadata suffixes for all source types"
```

---

### Task 5: Update consolidation directive

**Files:**
- None (API call to n8n Hindsight)

- [ ] **Step 1: Update the existing directive via API**

```bash
# First, get the existing directive ID
curl -s "https://n8nhindsight.applikuapp.com/v1/default/banks/n8n/directives" \
  -H "Authorization: Bearer $HINDSIGHT_KEY" | python3 -m json.tool

# Then update it (use the ID from above)
curl -s -X PUT "https://n8nhindsight.applikuapp.com/v1/default/banks/n8n/directives/{DIRECTIVE_ID}" \
  -H "Authorization: Bearer $HINDSIGHT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "When consolidating memories, preserve key metadata from the original memories in the consolidated text. Include: (1) Source URLs — append the most relevant URL as \"Source: <url>\" at the end. If consolidating multiple sources, include up to 3 URLs. (2) Engagement metrics — preserve vote counts, like counts, view counts, reaction counts, comment counts, and solved/accepted answer status. Format as parenthetical after the main content. (3) For feature requests, always preserve the vote count. (4) For community posts, preserve whether there is an accepted answer. (5) For GitHub issues, preserve labels (especially team assignments like team:ai, status:in-linear), state (open/closed), state_reason (completed/not_planned/duplicate), and author_association (MEMBER vs community). (6) Recognize patterns across related issues — frequently reported problems closed as stale, duplicate, or incomplete should be synthesized into observations like \"this is a common issue with no official fix\" or \"multiple users report this; n8n tracks it internally.\" This metadata is critical for downstream confidence scoring.",
    "priority": 10
  }'
```

- [ ] **Step 2: Verify directive is active**

```bash
curl -s "https://n8nhindsight.applikuapp.com/v1/default/banks/n8n/directives" \
  -H "Authorization: Bearer $HINDSIGHT_KEY" | python3 -m json.tool
```

---

### Task 6: Update config defaults and README

**Files:**
- Modify: `n8n-knowledge/.claude/n8n-knowledge.local.md`
- Modify: `n8n-knowledge/README.md`

- [ ] **Step 1: Update local config with new defaults**

Add to `.claude/n8n-knowledge.local.md` frontmatter:

```yaml
github_base: 49
clear_signal_bonus: 25
author_member_bonus: 5
```

- [ ] **Step 2: Update README scoring tuning section**

Add the new config keys to the README's scoring config block with comments explaining each.

- [ ] **Step 3: Commit**

```bash
git add .claude/n8n-knowledge.local.md README.md
git commit -m "Document new GitHub scoring config: github_base, clear_signal_bonus, author_member_bonus"
```

---

### Task 7: Verify n8n Pulse chatbot metadata passthrough

**Files:**
- Read: `portfolio/app/chat.py`

- [ ] **Step 1: Check that tool result serialization preserves metadata**

Read the `_n8n_mode_response` function in `chat.py`. Verify that the recall results are passed through to Claude with metadata intact. The `max_chars` (18000) should accommodate the additional metadata fields without truncation at the chatbot layer.

- [ ] **Step 2: Test live**

Ask the n8n Pulse chatbot a question about a GitHub issue and verify it cites labels, engagement, and state in its response.

---

### Task 8: Run full sync and verify end-to-end

- [ ] **Step 1: Run sync with --dry-run first**

```bash
cd /Users/danielbennett/codeNew/n8n-hindsight
python3 scripts/sync-github.py --dry-run
```

Expected: Shows ~436 open + ~4064 closed = ~4500 total issues, all metadata fields present.

- [ ] **Step 2: Run actual sync**

```bash
python3 scripts/sync-github.py --full
```

- [ ] **Step 3: Test recall with new metadata**

```bash
curl -s -X POST "https://n8nhindsight.applikuapp.com/public/recall" \
  -H "Content-Type: application/json" \
  -d '{"query": "webhook trigger bug", "budget": "low", "max_tokens": 3000}' | python3 -m json.tool | head -30
```

Verify results include reactions_total, comments, state, state_reason, author_association in metadata.

- [ ] **Step 4: Test plugin output format**

In a Claude Code session with the plugin installed, ask an n8n question and verify the injected context shows resolution bucket hints and metadata suffixes.

- [ ] **Step 5: Run full test suite**

```bash
bash tests/run-all.sh
```

Expected: All tests pass.

- [ ] **Step 6: Final commit and push**

```bash
git push
```
