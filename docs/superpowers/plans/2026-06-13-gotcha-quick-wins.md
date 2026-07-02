# Gotcha Handling Quick Wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 4 root causes identified in the gotcha handling RCA to improve gotcha detection from ~25% to ~65%+.

**Architecture:** Four independent fixes targeting: (1) recall retry on empty gotcha results, (2) GitHub issue visibility threshold, (3) node detection for verb forms like "merges", (4) visual bug warning prefix on GitHub issues in formatted output.

**Tech Stack:** Bash (recall_common.sh, structured_recall.sh), Python (plugin_config.py, format_results.py, node_lookup.py), bash test harness (test-*.sh)

---

### Task 1: Raise github_base from 49 to 55

**Files:**
- Modify: `hooks/lib/plugin_config.py:20`
- Modify: `tests/test-observation-scoring.sh`

This is the simplest fix. GitHub issues currently score 49 (below the 50 medium_threshold), which means they default to LOW confidence and are capped at 1 result shown. Raising to 55 ensures all GitHub issues are at least MEDIUM, surviving the max_low_results filter.

- [ ] **Step 1: Write the failing test**

Add a new assertion to `tests/test-observation-scoring.sh` that verifies a bare GitHub issue (no engagement bonuses) scores MEDIUM, not LOW.

Insert before the final `echo "" / echo "=== Results..."` block at the end of the file:

```bash
# --- GitHub base score threshold ---
gh_bare_level=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
cfg = fr.DEFAULTS
r = {'type':'world','text':'Bug in node X','tags':['type:github-issue','source:github-issues'],
     'metadata':{'state':'open','reactions_total':'0','comments':'0'}}
level,_,_ = fr.score_result(r, cfg)
print(level)
")
assert_eq "bare GitHub issue (no engagement) scores MEDIUM" "MEDIUM" "$gh_bare_level"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-observation-scoring.sh`
Expected: FAIL — bare GitHub issue scores LOW (base 49 < medium_threshold 50)

- [ ] **Step 3: Change github_base from 49 to 55**

In `hooks/lib/plugin_config.py`, change line 20:
```python
    "github_base": 55,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-observation-scoring.sh`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/plugin_config.py tests/test-observation-scoring.sh
git commit -m "raise github_base to 55 so bare issues score MEDIUM

GitHub issues with 0 engagement scored 49 (LOW) and were filtered
by max_low_results=1. Gotcha-relevant bugs were invisible unless
they had team labels or high engagement. Base 55 ensures all GitHub
issues are at least MEDIUM confidence."
```

---

### Task 2: Add retry with backoff for gotcha recall

**Files:**
- Modify: `hooks/lib/structured_recall.sh:26-42`
- Modify: `tests/test-recall-resilience.sh`

Under eval concurrency (16+ parallel calls), the gotcha recall channel silently returns empty results due to 8s curl timeout. Add a single retry after 1s if the initial gotcha recall returns empty/invalid JSON.

- [ ] **Step 1: Write the failing test**

Add a new test case to `tests/test-recall-resilience.sh`. The test needs a stub server mode that fails the first gotcha request then succeeds on retry. We'll add a `gotcha-fail-once` mode to the stub server and a test that verifies retry behavior.

First, add the `gotcha-fail-once` mode to `tests/fixtures/stub_recall_server.py`. In the `Handler.do_POST` method, add tracking for gotcha request count and fail on the first one:

Add a module-level counter after the `BODY_LOG` line:

```python
GOTCHA_REQUEST_COUNT = 0
```

In the `do_POST` method, add a new mode check after the `if MODE == "slow":` block and before `if MODE == "sem-fail"`:

```python
        if MODE == "gotcha-fail-once" and kind == "gotcha":
            global GOTCHA_REQUEST_COUNT
            GOTCHA_REQUEST_COUNT += 1
            if GOTCHA_REQUEST_COUNT == 1:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "gotcha recall failed (transient)"}')
                return
```

- [ ] **Step 2: Write the retry test in test-recall-resilience.sh**

Add a new test section before the final results summary. This test launches the stub in `gotcha-fail-once` mode, runs `do_gotcha_recall`, and checks that valid results come back (proving retry worked).

```bash
# --- C5: gotcha recall retries on transient failure ---
echo ""
echo "--- C5: gotcha recall retries on empty/failed response ---"

STUB_PORT_C5=""
STUB_PID_C5=""
BODY_LOG_C5="$RUNTIME_DIR/c5-body.log"

launch_stub_c5() {
  local out_file
  out_file="$(mktemp)"
  python3 "$STUB" gotcha-fail-once 0 "$BODY_LOG_C5" > "$out_file" 2>/dev/null &
  STUB_PID_C5=$!
  local tries=0
  while [ "$tries" -lt 40 ]; do
    if grep -q '^PORT=' "$out_file" 2>/dev/null; then
      STUB_PORT_C5=$(grep '^PORT=' "$out_file" | head -1 | cut -d= -f2)
      rm -f "$out_file"
      return 0
    fi
    sleep 0.1
    tries=$((tries + 1))
  done
  rm -f "$out_file"
  return 1
}

launch_stub_c5

# Source the recall libs with our stub URL
c5_result=$(
  N8N_KNOWLEDGE_RUNTIME_DIR="$RUNTIME_DIR" \
  RECALL_URL="http://127.0.0.1:$STUB_PORT_C5" \
  bash -c '
    source "'"$REPO"'/hooks/lib/structured_recall.sh"
    do_gotcha_recall "nodes-base.openAi"
  '
)

kill "$STUB_PID_C5" 2>/dev/null || true
wait "$STUB_PID_C5" 2>/dev/null || true

if echo "$c5_result" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
  pass "gotcha recall returns results after retry on transient failure"
else
  fail "gotcha recall returns results after retry on transient failure"
fi

# Verify two requests were logged (initial + retry)
c5_count=$(wc -l < "$BODY_LOG_C5" 2>/dev/null | tr -d ' ')
if [ "$c5_count" = "2" ]; then
  pass "gotcha recall made exactly 2 requests (initial + retry)"
else
  fail "gotcha recall made exactly 2 requests (expected 2, got $c5_count)"
fi
```

- [ ] **Step 3: Run test to verify it fails**

Run: `bash tests/test-recall-resilience.sh`
Expected: FAIL — do_gotcha_recall currently makes only 1 request and returns the error

- [ ] **Step 4: Implement retry in do_gotcha_recall**

Replace the `do_gotcha_recall` function in `hooks/lib/structured_recall.sh` (lines 26-42) with:

```bash
do_gotcha_recall() {
  local node_type="$1"
  local service
  service=$(echo "$node_type" | sed 's/.*\.//' | sed 's/Trigger$//' | sed 's/Tool$//')

  local query_escaped
  query_escaped=$(printf '%s node bug issue workaround error' "$service" | recall_json_escape)

  local body
  body=$(printf '{"query": %s, "budget": "low", "max_tokens": 2000, "include": {"source_facts": {}}}' \
    "$query_escaped")

  local result
  result=$(recall_post "$body")

  # Retry once after 1s if the initial call returned empty/invalid JSON.
  # Under eval concurrency (16+ parallel calls hitting one Hindsight instance),
  # the 8s curl timeout frequently fires. A single retry recovers most transient
  # failures without adding meaningful latency to the happy path.
  if ! echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
    sleep 1
    nk_debug_log_write "[$(date +%H:%M:%S)] gotcha_recall retry for $service (initial empty/failed)"
    result=$(recall_post "$body")
  fi

  echo "$result"
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `bash tests/test-recall-resilience.sh`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add hooks/lib/structured_recall.sh tests/test-recall-resilience.sh tests/fixtures/stub_recall_server.py
git commit -m "add retry with backoff for gotcha recall

Under 16-way eval concurrency, the 8s curl timeout fires on the
gotcha recall channel, silently returning empty results. A single
retry after 1s recovers most transient failures. Proven needed by
eval run 20260612-104311-v2 where 3/8 gotcha prompts got zero
recall results from all channels."
```

---

### Task 3: Fix node detection for verb forms ("merges" → Merge node)

**Files:**
- Modify: `hooks/lib/node_lookup.py:187-200` (fuzzy fallback)
- Modify: `tests/fixtures/node-lookup-queries.json`
- Modify: `tests/test-node-lookup.sh`

The prompt "build a workflow that merges data from two different API sources" detects `workflowTrigger` instead of `merge`. Two bugs: (1) "merges" (verb) doesn't exact-match "merge" (noun), (2) the fuzzy fallback doesn't respect `_DEMOTED_BARE_TOKENS`, so "workflow" sneaks through.

- [ ] **Step 1: Add test fixtures and regression tests**

Add two entries to `tests/fixtures/node-lookup-queries.json`:

```json
{"query": "merges data from two different API sources", "expect": "nodes-base.merge"},
{"query": "build a workflow that splits items into batches", "expect": "nodes-base.splitInBatches"}
```

Add regression tests to `tests/test-node-lookup.sh` in the detection noise regression section (after the existing defect F tests, before `for name, ok in cases:`):

```python
# Defect G: verb forms of node names must resolve to the node.
# "merges" -> merge, "splits" -> split. The fuzzy fallback must also
# respect _DEMOTED_BARE_TOKENS so "workflow" doesn't sneak through.
cases.append(('G_merges_finds_merge',
    'nodes-base.merge' in types('merges data from two different API sources')))
cases.append(('G_merges_no_workflow',
    'nodes-base.workflowTrigger' not in types('merges data from two different API sources')))
cases.append(('G_splits_finds_split',
    'nodes-base.splitInBatches' in types('splits items into batches')))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-node-lookup.sh`
Expected: FAIL — "merges" doesn't match, "workflow" leaks through fuzzy

- [ ] **Step 3: Fix node_lookup.py fuzzy fallback**

In `hooks/lib/node_lookup.py`, modify the fuzzy fallback section (Pass 2, starting around line 187). The fix has two parts:

**Part A:** Strip common English verb suffixes (-s, -es, -ed, -ing) before fuzzy matching so "merges" → "merge", "splits" → "split".

**Part B:** Skip fuzzy matches for words in `_DEMOTED_BARE_TOKENS`.

Replace the Pass 2 block (from `if not hits:` through the end of the fuzzy section) with:

```python
    # Pass 2: fuzzy fallback for unmatched words (catches typos + verb forms)
    if not hits:
        words = re.findall(r"\b[a-z]{3,}\b", pl)
        for w in words:
            if w in _COMMON_WORDS or w in _DEMOTED_BARE_TOKENS:
                continue
            # Strip common verb suffixes to match node names that are bare nouns
            # (e.g. "merges" → "merge", "splits" → "split", "filtered" → "filter").
            stems = [w]
            if w.endswith("es") and len(w) > 4:
                stems.append(w[:-2])
            if w.endswith("s") and len(w) > 3:
                stems.append(w[:-1])
            if w.endswith("ed") and len(w) > 4:
                stems.append(w[:-2])
            if w.endswith("ing") and len(w) > 5:
                stems.append(w[:-3])
            for stem in stems:
                if stem in lookup:
                    nt = lookup[stem]
                    hits.append((stem, nt))
                    break
            if hits:
                break
            # Original fuzzy similarity check (for typos)
            best, best_score = None, 0.0
            for name in lookup:
                if len(name) < 3 or " " in name:
                    continue
                if name in _COMMON_WORDS or name in _DEMOTED_BARE_TOKENS:
                    continue
                ratio = _similarity(w, name)
                if ratio > best_score:
                    best, best_score = name, ratio
            if best_score >= 0.85:
                hits.append((best, lookup[best]))
                break
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-node-lookup.sh`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `bash tests/run-all.sh`
Expected: ALL PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add hooks/lib/node_lookup.py tests/fixtures/node-lookup-queries.json tests/test-node-lookup.sh
git commit -m "fix node detection for verb forms and fuzzy demotion

'merges data from two API sources' detected workflowTrigger instead
of Merge node. Two fixes: (1) strip verb suffixes (-s/-es/-ed/-ing)
before lookup so 'merges' matches 'merge', (2) fuzzy fallback now
respects _DEMOTED_BARE_TOKENS so 'workflow' can't sneak through."
```

---

### Task 4: Add bug warning prefix to GitHub issues in formatted output

**Files:**
- Modify: `hooks/lib/format_results.py:508-519` (render_result, non-observation branch)
- Modify: `tests/test-observation-scoring.sh`

When the LLM has a GitHub bug report injected alongside 4-5 other results, it has no visual cue that the bug is specifically dangerous for its current task. Add a `⚠️ KNOWN BUG:` prefix to open GitHub issues so they stand out.

- [ ] **Step 1: Write the failing test**

Add to `tests/test-observation-scoring.sh` before the final results block:

```bash
# --- GitHub bug warning prefix ---
gh_open_block=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
cfg = fr.DEFAULTS
r = {'type':'world','text':'OpenAI credential fails with setContentType error',
     'tags':['type:github-issue','source:github-issues'],
     'metadata':{'state':'open','reactions_total':'2','comments':'5',
                 'url':'https://github.com/n8n-io/n8n/issues/31659'}}
print(fr.render_result(1, r, 'HIGH', False, [], cfg))
")
assert_contains "open GitHub issue has bug warning" "KNOWN BUG" "$gh_open_block"

gh_closed_block=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
cfg = fr.DEFAULTS
r = {'type':'world','text':'Fixed a typo in the docs',
     'tags':['type:github-issue','source:github-issues'],
     'metadata':{'state':'closed','state_reason':'completed','reactions_total':'0','comments':'1',
                 'url':'https://github.com/n8n-io/n8n/issues/99999'}}
print(fr.render_result(2, r, 'MEDIUM', False, [], cfg))
")
assert_not_contains "closed-completed GitHub issue has no bug warning" "KNOWN BUG" "$gh_closed_block"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-observation-scoring.sh`
Expected: FAIL — no "KNOWN BUG" prefix exists yet

- [ ] **Step 3: Implement bug warning prefix**

In `hooks/lib/format_results.py`, in the `render_result` function's non-observation branch (the `else:` block starting around line 508), add the bug warning prefix after the github_state_tag is prepended. Find these lines:

```python
        tag = github_state_tag(r.get("metadata"), r.get("tags"))
        if tag:
            text = f"{tag} {text}"
```

Replace with:

```python
        tag = github_state_tag(r.get("metadata"), r.get("tags"))
        if tag:
            text = f"{tag} {text}"
        is_github = any(
            t.startswith("source:github") or t in ("type:github-issue", "type:github-pr")
            for t in (r.get("tags") or [])
        )
        meta = r.get("metadata") or {}
        is_open_or_wontfix = (
            meta.get("state") == "open"
            or meta.get("state_reason") in ("not_planned", "")
        )
        if is_github and is_open_or_wontfix and meta.get("state_reason") != "completed":
            text = f"KNOWN BUG: {text}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-observation-scoring.sh`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `bash tests/run-all.sh`
Expected: ALL PASS (check test-structured-recall.sh and test-recall-format.sh for regressions)

- [ ] **Step 6: Commit**

```bash
git add hooks/lib/format_results.py tests/test-observation-scoring.sh
git commit -m "add KNOWN BUG prefix to open GitHub issues in recall output

When bug reports are mixed with 4-5 other recall results, the LLM
has no visual cue that a specific issue is dangerous for its current
task. The KNOWN BUG prefix on open/wontfix GitHub issues makes them
stand out. Closed-completed issues don't get the prefix."
```

---

### Verification

After all 4 tasks are complete:

- [ ] **Run full test suite**: `bash tests/run-all.sh`
- [ ] **Verify no regressions in existing 27 tests**
- [ ] **Manual smoke test**: Run a recall query for "classify support tickets using OpenAI" and verify GitHub issues appear with KNOWN BUG prefix at MEDIUM+ confidence
