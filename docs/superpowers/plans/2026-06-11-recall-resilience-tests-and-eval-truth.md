# Recall Resilience Tests + Eval Truth + Renderer Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **ENVIRONMENT CONSTRAINT (Dan's machine):** dispatched subagents CANNOT run Bash.
> Subagents author files via Write/Edit and report the exact commands; the
> ORCHESTRATOR runs every test (red→green) and makes every commit. Commits only
> with Dan's explicit go-ahead; never include AI attribution in messages.

**Goal:** Pin the 2026-06-11 recall-resilience fixes with regression tests, update the README eval section with post-contamination-fix numbers, and clean up the renderer (strip usernames from synthesized prose, demote sourceless synthesis to LOW).

**Architecture:** New bash test file with a tiny Python stub HTTP server fixture exercises the real hooks end-to-end (closed-port, slow-port, garbage-payload, selective-failure cases). Renderer changes are two small pure functions in `hooks/lib/format_results.py` with pytest coverage. README edits are text-only.

**Tech Stack:** bash, curl, Python 3 stdlib (`http.server`), pytest (existing suite under `tests/python/`).

**Context for the implementer (read first):**
- `hooks/lib/recall_common.sh` — `recall_post()` now has `--connect-timeout 2 --max-time ${RECALL_CURL_MAX_TIME:-8}`, logs failures to `/tmp/n8n-knowledge-debug.log`, and ALWAYS returns 0 (callers run under `set -euo pipefail`; a propagated failure used to kill the whole hook silently).
- `hooks/lib/format_results.py` — `format_results()` returns `None` on empty/unparseable payload (used to raise, which killed the hook at the `RESULT=$(...)` assignment).
- `hooks/auto-recall.sh` — gotcha recall fans out over up to 3 detected node types (`CLAUDE_PLUGIN_OPTION_GOTCHANODECAP`), results merged round-robin with dedup, cap 5; the sem/struct/gotcha merge loads each stream independently (a dead semantic recall must not discard gotcha results).
- NONE of these behaviors currently has a test. That is what Tasks 1–4 fix.

---

### Task 1: Stub recall server fixture

**Files:**
- Create: `tests/fixtures/stub_recall_server.py`

- [ ] **Step 1 (subagent): Write the fixture**

```python
#!/usr/bin/env python3
"""Stub Hindsight recall server for hook resilience tests.

Modes (selected by REQUEST BODY content, so one server handles all cases):
- body contains "source_facts"  -> this is the SEMANTIC recall call
- otherwise                      -> structured/gotcha call

Behavior switches via CLI arg:
  ok        : valid results for every request
  sem-fail  : HTTP 500 for semantic calls, valid results otherwise
  slow      : sleep 30s before answering (for curl --max-time tests)

Each request body is appended to the file given as argv[3] (one JSON per line)
so tests can assert how many calls of each kind were made.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8731
BODY_LOG = sys.argv[3] if len(sys.argv) > 3 else "/dev/null"

def results_for(body: str):
    if "source_facts" in body:
        kind = "semantic"
    elif '"max_tokens": 2000' in body:
        kind = "gotcha"
    else:
        kind = "struct"
    return {
        "results": [
            {
                "id": f"{kind}-{i}",
                "type": "fact",
                "text": f"{kind} result {i} for test",
                "tags": ["source:github"],
                "metadata": {"source_url": f"https://example.com/{kind}/{i}"},
            }
            for i in range(3)
        ]
    }

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        with open(BODY_LOG, "a") as f:
            f.write(body.replace("\n", " ") + "\n")
        if MODE == "slow":
            time.sleep(30)
        if MODE == "sem-fail" and "source_facts" in body:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        payload = json.dumps(results_for(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # quiet
        pass

HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
```

- [ ] **Step 2 (orchestrator): Smoke the fixture**

Run: `python3 tests/fixtures/stub_recall_server.py ok 8731 /tmp/stub-bodies.log & sleep 1; curl -s -X POST http://127.0.0.1:8731 -d '{"query":"x","max_tokens":2000}' | python3 -m json.tool | head -5; kill %1`
Expected: JSON with `gotcha-0` result id.

### Task 2: Resilience test file — recall_post contracts

**Files:**
- Create: `tests/test-recall-resilience.sh`
- Modify: `tests/run-all.sh` (add the new file to the list, matching its existing style)

- [ ] **Step 1 (subagent): Write the failing test**

```bash
#!/usr/bin/env bash
set -uo pipefail
# Pins the 2026-06-11 recall-resilience contracts:
#  C1 recall_post NEVER returns nonzero (callers run under set -e)
#  C2 recall_post honors RECALL_CURL_MAX_TIME (slow endpoint cut off)
#  C3 recall failures are LOGGED, not silent
#  C4 format_results.py tolerates empty/garbage payloads (exit 0, no output)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$SCRIPT_DIR/../hooks/lib"
PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== recall resilience tests ==="

# --- C1: closed port -> rc 0, empty stdout ---
OUT=$(RECALL_URL="http://127.0.0.1:9" bash -c '
  set -euo pipefail
  source "'"$LIB"'/recall_common.sh"
  recall_post "{\"query\":\"x\"}"
  echo "SURVIVED"')
[ $? -eq 0 ] && echo "$OUT" | grep -q SURVIVED \
  && ok "C1 recall_post is non-fatal under set -e on connection failure" \
  || bad "C1 recall_post killed a set -e caller (or rc != 0)"

# --- C2: slow endpoint cut off by max-time ---
PORT=8732
python3 "$SCRIPT_DIR/fixtures/stub_recall_server.py" slow $PORT /dev/null &
SRV=$!
sleep 1
START=$(date +%s)
RECALL_URL="http://127.0.0.1:$PORT" RECALL_CURL_MAX_TIME=2 bash -c '
  source "'"$LIB"'/recall_common.sh"; recall_post "{\"query\":\"x\"}"' >/dev/null
ELAPSED=$(( $(date +%s) - START ))
kill $SRV 2>/dev/null
[ "$ELAPSED" -le 6 ] \
  && ok "C2 slow endpoint cut off in ${ELAPSED}s (max-time honored)" \
  || bad "C2 took ${ELAPSED}s — max-time not honored"

# --- C3: failure is logged ---
DBG_BEFORE=$(grep -c "recall_post FAIL" /tmp/n8n-knowledge-debug.log 2>/dev/null || echo 0)
RECALL_URL="http://127.0.0.1:9" bash -c '
  source "'"$LIB"'/recall_common.sh"; recall_post "{\"query\":\"x\"}"' >/dev/null
DBG_AFTER=$(grep -c "recall_post FAIL" /tmp/n8n-knowledge-debug.log 2>/dev/null || echo 0)
[ "$DBG_AFTER" -gt "$DBG_BEFORE" ] \
  && ok "C3 failure logged to debug log" \
  || bad "C3 failure NOT logged"

# --- C4: formatter tolerates bad payloads ---
T=$(mktemp)
python3 "$LIB/format_results.py" "$T" "" >/dev/null 2>&1 \
  && ok "C4a empty payload -> exit 0" || bad "C4a empty payload crashed formatter"
echo "not json {{{" > "$T"
python3 "$LIB/format_results.py" "$T" "" >/dev/null 2>&1 \
  && ok "C4b garbage payload -> exit 0" || bad "C4b garbage payload crashed formatter"
rm -f "$T"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2 (orchestrator): Run it — must PASS against current code** (these contracts already exist; the test pins them). If any check FAILS, the implementation regressed — stop and investigate before proceeding.

Run: `bash tests/test-recall-resilience.sh`
Expected: `Results: 5 passed, 0 failed`

- [ ] **Step 3 (orchestrator): Prove the test bites** — temporarily revert the contract (`sed -i '' 's/return 0$/return "$rc"/' hooks/lib/recall_common.sh`), rerun, expect C1 FAIL, then restore (`git checkout -- hooks/lib/recall_common.sh` only if file has no other uncommitted edits — otherwise undo the sed by hand).

- [ ] **Step 4 (orchestrator): Commit** (with Dan's go-ahead)

```bash
git add tests/test-recall-resilience.sh tests/fixtures/stub_recall_server.py tests/run-all.sh
git commit -m "test: pin recall_post non-fatal/timeout/logging and formatter tolerance contracts"
```

### Task 3: End-to-end test — merge independence + multi-node gotcha fan-out

**Files:**
- Create: `tests/test-auto-recall-resilience.sh`
- Modify: `tests/run-all.sh`

- [ ] **Step 1 (subagent): Write the failing test**

```bash
#!/usr/bin/env bash
set -uo pipefail
# E2E pins for hooks/auto-recall.sh (2026-06-11 fixes):
#  E1 dead SEMANTIC recall must NOT discard gotcha/struct results (merge independence)
#  E2 gotcha recall fans out over multiple detected node types (>=2 gotcha calls)
# Uses the stub server; runs the real hook with a node-name-bearing prompt and
# cwd set to the repo (so detect-n8n fires).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }
PROMPT='read a google sheet and merge results with zerobounce'

run_hook() { # $1=mode $2=port $3=bodylog
  python3 "$SCRIPT_DIR/fixtures/stub_recall_server.py" "$1" "$2" "$3" &
  SRV=$!
  sleep 1
  printf '{"prompt": "%s", "cwd": "%s"}' "$PROMPT" "$REPO" \
    | RECALL_URL="http://127.0.0.1:$2" bash "$REPO/hooks/auto-recall.sh" 2>/dev/null
  kill $SRV 2>/dev/null
}

echo "=== auto-recall resilience E2E ==="

# --- E1: semantic 500s, gotcha/struct succeed -> output still has results ---
OUT=$(run_hook sem-fail 8733 /dev/null)
echo "$OUT" | grep -q "gotcha result" \
  && ok "E1 gotcha results survive a dead semantic recall" \
  || bad "E1 dead semantic recall discarded gotcha results"

# --- E2: multi-node fan-out -> at least 2 gotcha-shaped request bodies ---
BLOG=$(mktemp)
run_hook ok 8734 "$BLOG" >/dev/null
GOTCHA_CALLS=$(grep -c '"max_tokens": 2000' "$BLOG")
[ "$GOTCHA_CALLS" -ge 2 ] \
  && ok "E2 gotcha recall fanned out over multiple nodes ($GOTCHA_CALLS calls)" \
  || bad "E2 only $GOTCHA_CALLS gotcha call(s) — fan-out regressed"
rm -f "$BLOG"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2 (orchestrator): Run** — expected `2 passed, 0 failed` against current code. Note: the prompt detects two nodes (zerobounce → `@zerobounce/...zeroBounce`, merge → `nodes-base.merge`); if detection changes upstream this test's prompt may need a second guaranteed node name.

- [ ] **Step 3 (orchestrator): Prove it bites** — change `GOTCHA_NODE_CAP` default to 1 in `hooks/auto-recall.sh`, rerun (E2 must FAIL), restore.

- [ ] **Step 4 (orchestrator): Commit** (with go-ahead)

```bash
git add tests/test-auto-recall-resilience.sh tests/run-all.sh
git commit -m "test: pin auto-recall merge independence and multi-node gotcha fan-out"
```

### Task 4: Renderer — strip usernames from synthesized prose

**Files:**
- Modify: `hooks/lib/format_results.py` (add `redact_handles()`; apply ONLY to observation/synthesis text, NOT to citation lines or source URLs)
- Test: `tests/python/test_format_results_redaction.py` (new; follow the existing pytest layout under `tests/python/`)

- [ ] **Step 1 (subagent): Write the failing tests**

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks", "lib"))
from format_results import redact_handles

def test_user_prefixed_handle_redacted():
    assert redact_handles("user Chrisyk resolved by running trigger through both nodes") \
        == "a community user resolved by running trigger through both nodes"

def test_underscore_handle_redacted():
    assert redact_handles("Julia_Solias_Huelamo reports Merge Node losing lines") \
        == "a community user reports Merge Node losing lines"

def test_plain_prose_untouched():
    s = "Merge node in Combine mode silently loses rows when branches mismatch"
    assert redact_handles(s) == s

def test_node_names_untouched():
    s = "Use Loop Over Items with Google Sheets and HTTP Request"
    assert redact_handles(s) == s

def test_urls_untouched():
    s = "see https://community.n8n.io/t/some-thread/123 for details"
    assert redact_handles(s) == s
```

- [ ] **Step 2 (orchestrator): Run to verify red**

Run: `python3 -m pytest tests/python/test_format_results_redaction.py -v`
Expected: FAIL — `ImportError: cannot import name 'redact_handles'`

- [ ] **Step 3 (subagent): Implement**

```python
_HANDLE_AFTER_USER = re.compile(r"\buser[s]?\s+[A-Z][A-Za-z0-9_]{2,}\b")
_UNDERSCORE_HANDLE = re.compile(r"\b[A-Z][a-z0-9]+(?:_[A-Z]?[a-z0-9]+){1,}\b")

def redact_handles(text):
    """Strip community usernames from machine-distilled prose.

    Public-forum handles are fine in linked citations (attribution), but add
    nothing inside synthesized summaries injected into a user's context — and
    read as surveillance when members find themselves named. Conservative by
    design: only 'user <Handle>' phrases and Discourse-style underscore
    handles. URLs are never touched (redaction is applied to prose text
    fields only, never to source/citation lines).
    """
    text = _HANDLE_AFTER_USER.sub("a community user", text)
    return _UNDERSCORE_HANDLE.sub("a community user", text)
```

Wire it in at the point where observation/synthesis result text is rendered (the branch where `is_observation(r)` is true — apply to the text body only). Do NOT apply to raw fact results (their text is often a quoted issue title where the reporter name is attribution) or to any line containing `Source:` or `sources:`.

- [ ] **Step 4 (orchestrator): Run to verify green**

Run: `python3 -m pytest tests/python/test_format_results_redaction.py -v && bash tests/test-recall-format.sh`
Expected: all PASS (the second command guards against breaking existing rendering).

- [ ] **Step 5 (orchestrator): Commit** (with go-ahead)

```bash
git add hooks/lib/format_results.py tests/python/test_format_results_redaction.py
git commit -m "feat: redact community usernames from synthesized prose at render time"
```

### Task 5: Source facts for ALL recall channels (replaces the demotion idea)

**Decision history (2026-06-11, Dan):** do NOT blanket-demote sourceless synthesis.
Diagnosis showed the Hindsight source-fact feature works — `include: {"source_facts": {}}`
returns full source facts (URLs, usernames, views, solved status) and without the flag
the API strips `source_fact_ids` to zero. The "sources: unavailable" renders happen because
(a) `do_gotcha_recall` and `do_structured_recall` never send the include flag, and
(b) the merge in `auto-recall.sh` keeps only the SEMANTIC response's top-level
`source_facts` dict, discarding the others. Fix the plumbing; measure what remains;
any scoring/demotion change after that REQUIRES A DISCUSSION WITH DAN FIRST.

**Files:**
- Modify: `hooks/lib/structured_recall.sh` (add include flag to both request bodies)
- Modify: `hooks/auto-recall.sh` (merge `source_facts` dicts across all responses — both the multi-node gotcha combine and the sem/struct/gotcha merge)
- Modify: `hooks/lib/format_results.py` (render TOTAL consolidation strength: `sources="<len(source_fact_ids)>"` with the first 3 resolved links, instead of capping the displayed count at the 3 resolved pairs — an observation consolidated from 24 high-engagement posts should LOOK stronger than one built from 2)
- Test: extend `tests/python/` with a renderer test (observation with 24 ids, 3 resolvable -> tag shows sources="24", 3 links listed) and extend `tests/test-auto-recall-resilience.sh` stub to return a `source_facts` dict on gotcha responses and assert the rendered output cites a source URL for a gotcha-channel observation.

- [ ] **Step 1 (subagent):** Add `"include": {"source_facts": {}}` to the JSON bodies in `do_structured_recall` and `do_gotcha_recall` (mirror the exact syntax in `hooks/lib/recall.sh` line ~11).
- [ ] **Step 2 (subagent):** In both merge pythons in `auto-recall.sh`, accumulate `source_facts`: `merged_sf = {}` then `merged_sf.update(payload.get("source_facts") or {})` for every loaded stream; write it back as `sem["source_facts"] = {**(sem.get("source_facts") or {}), **merged_sf}`.
- [ ] **Step 3 (subagent):** Renderer change + tests as described above.
- [ ] **Step 4 (orchestrator):** Red->green on the new tests, then full `bash tests/run-all.sh`.
- [ ] **Step 5 (orchestrator):** Measurement, not action: run 5 representative recalls (merge, IF node, schedule trigger, webhook, google sheets), count observation renders still showing `sources="0"`. Report the number to Dan. NO demotion or scoring change without his explicit go-ahead.
- [ ] **Step 6 (orchestrator): Commit** (with go-ahead)

```bash
git add hooks/lib/structured_recall.sh hooks/auto-recall.sh hooks/lib/format_results.py tests/
git commit -m "fix: source facts flow through all recall channels; render consolidation strength"
```

### Task 6: README eval section — post-fix numbers + methodology note

**Files:**
- Modify: `README.md` (the `## Eval results (honest comparison)` section ONLY — Team 2 owns the version line at the top; do not touch it)

- [ ] **Step 1 (subagent): Replace the section body with:**

```markdown
## Eval results (honest comparison)

Benchmarked against the community **n8n-mcp** server on a 128-prompt
workflow-generation benchmark. The validated-workflow metric is "does the
generated workflow pass n8n-mcp's full validation engine."

**Methodology note (June 11, 2026):** earlier published numbers came from a
harness that we later found had two validity bugs — user-scope plugins leaked
context into the comparison conditions, and the plugin's own recall hook
failed silently under load. Both were found, fixed, and verified with
transcript audits; the table below is from post-fix runs (DeepSeek Pro,
no timeout, single run per prompt — treat ±1 run as noise).

| Run (clean harness)        | Plugin (validated) | n8n-mcp | Plugin real cost vs MCP |
|----------------------------|--------------------|---------|-------------------------|
| Group C (40 complex builds)| 80.0%              | 82.5%   | −31%                    |
| Groups A+B (88 prompts)*   | 78.4%              | 75.0%   | −33%                    |

\* A+B ran before the final harness fixes landed; its validity numbers carry
that caveat and will be refreshed.

**Read this honestly:** on raw validation pass rate the plugin and the MCP
server are **statistically tied** (±1 run). The plugin is not a
validation-quality silver bullet. Where it differs:

- **Cost / tokens:** ~31–33% cheaper end-to-end, ~65% fewer input tokens —
  context is injected instead of fetched through a tool-call loop.
- **Turns:** roughly half the tool round-trips.
- **Gotcha awareness:** with multi-node gotcha recall (v0.3.8), injected
  known-bug context measurably changes designs — e.g. avoiding the Merge
  node's positional-combine row-loss mode. The MCP condition earns its gotcha
  coverage differently (live docs fetching), at higher token cost.
```

Keep the existing closing line linking `docs/eval-findings-run1.md` with its "older, not directly comparable" caveat.

- [ ] **Step 2 (orchestrator): Review rendered markdown** (`grep -A40 "## Eval results" README.md`), confirm no stale "64.8%/66.4%" numbers remain anywhere in the file (`grep -n "64.8\|66.4\|62.5\|60.9" README.md` → no hits in the eval section).

- [ ] **Step 3 (orchestrator): SHOW DAN THE DIFF** — run `git diff README.md`, paste the full diff into the conversation, and WAIT for his explicit approval. Dan asked (2026-06-11) to review all README diffs before they are committed.

- [ ] **Step 4 (orchestrator): Commit** (only after Dan approves)

```bash
git add README.md
git commit -m "docs: post-fix eval numbers with methodology note"
```

## Self-review notes
- Task ordering: 1→2→3 share the stub fixture; 4→5 share the pytest file; 6 is independent.
- All test-RUNNING steps are orchestrator-only (subagents cannot Bash on this machine).
- Conflict boundary with Team 2: this plan touches `README.md` eval section only; Team 2 touches the version line only.
