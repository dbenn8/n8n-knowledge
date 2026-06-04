# Backstop Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. NOTE: in this environment dispatched subagents CANNOT run Bash — the orchestrator (main session) runs every test/commit step; subagents only author files via Edit/Write.

**Goal:** Inject first-class n8n recall context *during* the agent's reasoning turn (after Edit/Write/Task, and into Task subagents), gated/deduped/capped, and unify all three recall consumers on one rendering path.

**Architecture:** A `PostToolUse` hook (`backstop-recall.sh`) fires after every tool, counts calls, and on Edit/Write/Task extracts a fresh-keyword-anchored `<500`-token query, runs the shared `recall-cli.sh` (→ `do_recall` with `include.source_facts` → `format_results.py` 0.3.3 `<result>` XML), and injects via `additionalContext`. A gated `PreToolUse`-on-`Task` hook prepends the block into the subagent prompt via `updatedInput`. Per-session JSON state tracks counters + recalled topics for new-topic/staleness decisions. Failures always exit 0 (never block).

**Tech Stack:** bash hooks + Python 3 stdlib helpers; bash test harness (`tests/*.sh`, `assert_contains`/`assert_eq`); Hindsight public recall endpoint.

**Spec:** `docs/specs/2026-06-03-backstop-recall-design.md`

---

## File Structure

- **Modify** `hooks/lib/recall.sh` — `do_recall` gains `budget`+`max_tokens` params (default low/3000, keep `include.source_facts`).
- **Modify** `hooks/lib/format_results.py` — `main()` accepts `--event <name>` (default `UserPromptSubmit`) and `--bare` (print just the `<result>` context string).
- **Create** `hooks/lib/recall-cli.sh` — `recall-cli.sh <query> [budget] [max_tokens]` → bare `<result>` block.
- **Modify** `skills/n8n-knowledge/SKILL.md` — manual recall calls `recall-cli.sh`.
- **Modify** `hooks/lib/detect-n8n.sh` — keyword list configurable via `CLAUDE_PLUGIN_OPTION_triggerKeywords` with `DEFAULTS` sentinel.
- **Create** `hooks/lib/backstop_state.py` — load/save per-session state; `decide()`.
- **Create** `hooks/lib/query_window.py` — fresh-keyword-anchored query windowing.
- **Create** `hooks/backstop-recall.sh` — PostToolUse orchestrator.
- **Create** `hooks/backstop-subagent.sh` — PreToolUse-Task orchestrator (gated).
- **Modify** `hooks/hooks.json` — register PostToolUse `*` and PreToolUse `Task`.
- **Modify** `.claude-plugin/plugin.json` — new `userConfig` keys + version bump.
- **Modify** `README.md` — document backstop + `triggerKeywords`/`DEFAULTS` + default keyword list.
- **Create** `tests/test-backstop.sh` — all new tests.

Token note used throughout: `CHAR_BUDGET = 1600` (~4 chars/token, conservative under Hindsight's 500-token query cap).

---

## Task 1: `do_recall` budget + max_tokens params

**Files:** Modify `hooks/lib/recall.sh`; Test: `tests/test-backstop.sh` (create).

- [ ] **Step 1: Write the failing test** — create `tests/test-backstop.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"
PASS=0; FAIL=0
assert_contains(){ local d="$1" n="$2" h="$3"; if echo "$h"|grep -q "$n"; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$n')"; FAIL=$((FAIL+1)); fi; }
assert_eq(){ local d="$1" e="$2" a="$3"; if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$e' got '$a')"; FAIL=$((FAIL+1)); fi; }

echo "=== backstop tests ==="

# Task 1: do_recall accepts budget + max_tokens and builds the right payload.
payload=$(
  source "$LIB_DIR/recall.sh"
  # shadow curl so do_recall prints its JSON body instead of calling the network
  curl(){ for a in "$@"; do prev="${prev:-}"; if [ "$prev" = "-d" ]; then printf '%s' "$a"; fi; prev="$a"; done; }
  export -f curl 2>/dev/null || true
  do_recall "test query" "high" "8000"
)
assert_contains "do_recall sends budget high" '"budget": "high"' "$payload"
assert_contains "do_recall sends max_tokens 8000" '"max_tokens": 8000' "$payload"
assert_contains "do_recall keeps source_facts" '"source_facts"' "$payload"

echo ""; echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — current `do_recall` hardcodes `"budget": "%s"` from arg 2 but ignores `max_tokens` (fixed 3000) so `"max_tokens": 8000` is absent.

- [ ] **Step 3: Write minimal implementation** — replace the body of `do_recall` in `hooks/lib/recall.sh` with:

```bash
do_recall() {
  local query="$1"
  local budget="${2:-low}"
  local max_tokens="${3:-3000}"
  curl -s -X POST "$RECALL_URL" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"query": %s, "budget": "%s", "max_tokens": %s, "include": {"source_facts": {}}}' \
      "$(printf '%s' "$query" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')" \
      "$budget" "$max_tokens")"
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh`
Expected: PASS (3/3 for Task 1).

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/recall.sh tests/test-backstop.sh
git commit -m "do_recall: budget + max_tokens params"
```

---

## Task 2: `format_results.py` `--event` + `--bare`

**Files:** Modify `hooks/lib/format_results.py`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append before the Results line in `tests/test-backstop.sh`:

```bash
# Task 2: format_results.py supports --event and --bare.
FIX="$SCRIPT_DIR/fixtures/recall-with-source-facts.json"
bare=$(python3 "$LIB_DIR/format_results.py" "$FIX" --bare 2>/dev/null)
assert_contains "bare mode emits <result> tags" "<result" "$bare"
assert_contains "bare mode omits hook json wrapper" "n8n Knowledge Base" "$bare"
case "$bare" in *hookSpecificOutput*) echo "  FAIL: bare should not wrap in hook json"; FAIL=$((FAIL+1));; *) echo "  PASS: bare has no hook json"; PASS=$((PASS+1));; esac
evt=$(python3 "$LIB_DIR/format_results.py" "$FIX" --event PostToolUse 2>/dev/null)
assert_contains "event arg sets PostToolUse" '"hookEventName": "PostToolUse"' "$evt"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — `format_results.py` ignores extra args; bare/event unsupported.

- [ ] **Step 3: Write minimal implementation** — replace the `main()` function at the bottom of `hooks/lib/format_results.py` with:

```python
def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    args = sys.argv[1:]
    bare = "--bare" in args
    event = "UserPromptSubmit"
    if "--event" in args:
        i = args.index("--event")
        if i + 1 < len(args):
            event = args[i + 1]
    positional = [a for a in args if not a.startswith("--") and a not in (event,)]
    response_file = positional[0]
    project_dir = positional[1] if len(positional) > 1 else None

    context = format_results(response_file, project_dir)
    if not context:
        sys.exit(0)

    if bare:
        print(context)
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh` then `bash tests/run-all.sh`
Expected: Task 2 asserts PASS; full suite still green (existing UserPromptSubmit default unchanged).

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/format_results.py tests/test-backstop.sh
git commit -m "format_results.py: --event and --bare output modes"
```

---

## Task 3: `recall-cli.sh` shared entry

**Files:** Create `hooks/lib/recall-cli.sh`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append:

```bash
# Task 3: recall-cli.sh returns a bare <result> block (mock do_recall to fixture).
cli=$(
  RECALL_FIXTURE="$SCRIPT_DIR/fixtures/recall-with-source-facts.json" \
  bash -c '
    LIB="'"$LIB_DIR"'"
    # stub recall.sh do_recall to cat the fixture instead of curling
    source_stub(){ cat "$RECALL_FIXTURE"; }
    export -f source_stub
    RECALL_CLI_TEST=1 bash "'"$LIB_DIR"'/recall-cli.sh" "connect Claude Desktop n8n MCP" high 8000
  ' 2>/dev/null )
assert_contains "recall-cli emits <result>" "<result" "$cli"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — `recall-cli.sh` does not exist.

- [ ] **Step 3: Write minimal implementation** — create `hooks/lib/recall-cli.sh`:

```bash
#!/usr/bin/env bash
# recall-cli.sh <query> [budget] [max_tokens] -> bare 0.3.3 <result> block on stdout.
set -euo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$LIB_DIR/recall.sh"

QUERY="${1:-}"
BUDGET="${2:-high}"
MAX_TOKENS="${3:-8000}"
[ -z "$QUERY" ] && exit 0

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

if [ "${RECALL_CLI_TEST:-0}" = "1" ] && [ -n "${RECALL_FIXTURE:-}" ]; then
  cat "$RECALL_FIXTURE" > "$TMPFILE"
else
  do_recall "$QUERY" "$BUDGET" "$MAX_TOKENS" > "$TMPFILE" 2>/dev/null || exit 0
fi

python3 "$LIB_DIR/format_results.py" "$TMPFILE" --bare 2>/dev/null || true
```

Make it executable and set the test to point the fixture: change the Step-1 test invocation to `RECALL_CLI_TEST=1 RECALL_FIXTURE="$SCRIPT_DIR/fixtures/recall-with-source-facts.json" bash "$LIB_DIR/recall-cli.sh" "connect Claude Desktop n8n MCP" high 8000`. (Update the test to this simpler form.)

Replace the Task-3 test block with:

```bash
cli=$(RECALL_CLI_TEST=1 RECALL_FIXTURE="$SCRIPT_DIR/fixtures/recall-with-source-facts.json" bash "$LIB_DIR/recall-cli.sh" "connect Claude Desktop n8n MCP" high 8000 2>/dev/null)
assert_contains "recall-cli emits <result>" "<result" "$cli"
assert_contains "recall-cli has no hook json" "n8n Knowledge Base" "$cli"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x hooks/lib/recall-cli.sh && bash tests/test-backstop.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/recall-cli.sh tests/test-backstop.sh
git commit -m "recall-cli.sh: shared bare-output recall entry"
```

---

## Task 4: Unify the manual skill on `recall-cli.sh`

**Files:** Modify `skills/n8n-knowledge/SKILL.md`. (Doc change; verification by running recall-cli, already covered by Task 3.)

- [ ] **Step 1: Replace the manual-recall section** — in `skills/n8n-knowledge/SKILL.md`, replace the `## Manual recall` body (the raw `curl … public/recall … "budget": "mid"` block) with:

```markdown
## Manual recall

Use when: (1) auto-recall didn't fire (a follow-up without n8n keywords), or (2) auto-recall results were thin and you want more depth.

Run the bundled recall CLI — it returns the same first-class `<result>` format as auto-recall (synthesis labels, source engagement, fetch nudge), with full source metadata:

​```bash
bash "${CLAUDE_PLUGIN_ROOT}/hooks/lib/recall-cli.sh" "<your specific question>" high 8000
​```

The output is `<result>…</result>` blocks. Prefer cited sources over machine-distilled synthesis on conflict; for solved/high-confidence items, fetch the source URL for the full thread.
```

- [ ] **Step 2: Verify the documented command works**

Run: `RECALL_CLI_TEST=1 RECALL_FIXTURE=tests/fixtures/recall-with-source-facts.json bash hooks/lib/recall-cli.sh "n8n webhook" high 8000`
Expected: prints `<result>` blocks (no hook-json wrapper).

- [ ] **Step 3: Commit**

```bash
git add skills/n8n-knowledge/SKILL.md
git commit -m "SKILL.md: manual recall uses recall-cli (0.3.3 format + source_facts)"
```

---

## Task 5: Configurable trigger keywords + `DEFAULTS` sentinel

**Files:** Modify `hooks/lib/detect-n8n.sh`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append:

```bash
# Task 5: trigger keywords configurable, DEFAULTS sentinel.
r1=$(CLAUDE_PLUGIN_OPTION_triggerKeywords="DEFAULTS, gizmo" bash -c 'source "'"$LIB_DIR"'/detect-n8n.sh"; resolve_trigger_keywords')
assert_contains "DEFAULTS expands to built-ins" "workflow" "$r1"
assert_contains "DEFAULTS keeps additions" "gizmo" "$r1"
r2=$(CLAUDE_PLUGIN_OPTION_triggerKeywords="alpha, beta" bash -c 'source "'"$LIB_DIR"'/detect-n8n.sh"; resolve_trigger_keywords')
assert_contains "replace mode keeps custom" "alpha" "$r2"
case "$r2" in *workflow*) echo "  FAIL: replace mode should drop defaults"; FAIL=$((FAIL+1));; *) echo "  PASS: replace drops defaults"; PASS=$((PASS+1));; esac
r3=$(bash -c 'source "'"$LIB_DIR"'/detect-n8n.sh"; resolve_trigger_keywords')
assert_contains "unset uses defaults" "webhook" "$r3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — `resolve_trigger_keywords` undefined; list is hardcoded.

- [ ] **Step 3: Write minimal implementation** — in `hooks/lib/detect-n8n.sh`, replace the `N8N_BROAD_KEYWORDS=...` line with:

```bash
N8N_DEFAULT_KEYWORDS="workflow node trigger webhook credential expression execution"

resolve_trigger_keywords() {
  # Output a space-separated keyword list. Honors CLAUDE_PLUGIN_OPTION_triggerKeywords
  # (comma list). The token DEFAULTS expands to the built-in list inline.
  local cfg="${CLAUDE_PLUGIN_OPTION_triggerKeywords:-}"
  if [ -z "$cfg" ]; then
    printf '%s' "$N8N_DEFAULT_KEYWORDS"; return
  fi
  local out=""
  local IFS=','
  for w in $cfg; do
    w="$(printf '%s' "$w" | tr -d '[:space:]')"
    [ -z "$w" ] && continue
    if [ "$w" = "DEFAULTS" ]; then
      out="$out $N8N_DEFAULT_KEYWORDS"
    else
      out="$out $w"
    fi
  done
  printf '%s' "$out" | tr -s ' ' | sed 's/^ //; s/ $//'
}
```

Then update `should_recall` to build its regex from `resolve_trigger_keywords` instead of `$N8N_BROAD_KEYWORDS`:

```bash
should_recall() {
  local message="$1"
  local cwd="$2"
  local lower_message
  lower_message=$(printf '%s' "$message" | tr '[:upper:]' '[:lower:]')

  if printf '%s' "$lower_message" | grep -qw "n8n"; then
    echo "yes"; return
  fi

  if [ "$(is_n8n_codebase "$cwd")" = "yes" ]; then
    local kw_regex
    kw_regex="$(resolve_trigger_keywords | tr ' ' '|')"
    if printf '%s' "$lower_message" | grep -qEi "\b($kw_regex)\b"; then
      echo "yes"; return
    fi
  fi

  echo "no"
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh` then `bash tests/run-all.sh`
Expected: Task 5 PASS; full suite green (default behavior identical to before).

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/detect-n8n.sh tests/test-backstop.sh
git commit -m "detect-n8n: configurable triggerKeywords with DEFAULTS sentinel"
```

---

## Task 6: `backstop_state.py` (session state + decide)

**Files:** Create `hooks/lib/backstop_state.py`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append:

```bash
# Task 6: state load/save + staleness + decide.
st=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import backstop_state as s
state = s.new_state()
assert state['total_calls']==0 and state['topics']=={}
# stale: recorded at total=0,trigger=0; now total=16 -> stale by total
assert s.is_stale({'at_total':0,'at_trigger':0}, 16, 0) is True
assert s.is_stale({'at_total':0,'at_trigger':0}, 10, 6) is True   # stale by trigger
assert s.is_stale({'at_total':0,'at_trigger':0}, 10, 4) is False  # fresh
# decide: new signature fires under cap
state['recalls_done']=0
assert s.decide(state, ['webhook'], cap=4) is True
# at cap -> no fire
state['recalls_done']=4
assert s.decide(state, ['webhook'], cap=4) is False
print('ok')
")
assert_eq "backstop_state logic" "ok" "$st"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation** — create `hooks/lib/backstop_state.py`:

```python
"""Per-session state for the backstop recall hook."""
import json
import os

STALE_TOTAL = 15
STALE_TRIGGER = 5


def _dir():
    base = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    d = os.path.join(base, "n8n-knowledge-backstop")
    os.makedirs(d, exist_ok=True)
    return d


def path_for(session_id):
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return os.path.join(_dir(), f"{safe or 'nosession'}.json")


def new_state():
    return {"total_calls": 0, "trigger_calls": 0, "recalls_done": 0, "topics": {}}


def load_state(session_id):
    try:
        with open(path_for(session_id)) as f:
            s = json.load(f)
        for k, v in new_state().items():
            s.setdefault(k, v)
        return s
    except Exception:
        return new_state()


def save_state(session_id, state):
    try:
        with open(path_for(session_id), "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def is_stale(entry, total_calls, trigger_calls):
    return (total_calls - entry.get("at_total", 0) > STALE_TOTAL) or \
           (trigger_calls - entry.get("at_trigger", 0) > STALE_TRIGGER)


def active_covered(state):
    """Keywords whose topic was recalled and is NOT yet stale."""
    total, trig = state["total_calls"], state["trigger_calls"]
    covered = set()
    for sig, entry in state["topics"].items():
        if not is_stale(entry, total, trig):
            covered.update(sig.split("|"))
    return covered


def decide(state, signature, cap):
    """Fire if there is at least one fresh keyword (non-empty signature) and under cap."""
    if not signature:
        return False
    return state.get("recalls_done", 0) < cap


def record(state, signature):
    sig = "|".join(sorted(signature))
    state["topics"][sig] = {"at_total": state["total_calls"], "at_trigger": state["trigger_calls"]}
    state["recalls_done"] = state.get("recalls_done", 0) + 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh`
Expected: PASS (`ok`).

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/backstop_state.py tests/test-backstop.sh
git commit -m "backstop_state: session state, staleness, decide"
```

---

## Task 7: `query_window.py` (fresh-keyword anchoring)

**Files:** Create `hooks/lib/query_window.py`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append:

```bash
# Task 7: query windowing anchors on first fresh keyword, sentence-aligned.
qw=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import query_window as q
kws='workflow node trigger webhook credential expression execution'.split()
# all-fresh: window starts at 0
content='First we set up a webhook. Then more text.'
query,sig,more = q.window_query(content, kws, covered=set(), char_budget=1600)
assert query.startswith('First we set up a webhook'), query
assert 'webhook' in sig and more is False
# stale-before-fresh: 'workflow' covered, 'webhook' fresh later -> anchor at sentence with webhook
content2='Configure the workflow first. Now add a webhook trigger here.'
query2,sig2,more2 = q.window_query(content2, kws, covered={'workflow'}, char_budget=1600)
assert query2.startswith('Now add a webhook'), query2
assert 'webhook' in sig2
# no fresh keyword -> empty
query3,sig3,more3 = q.window_query('just a workflow only', kws, covered={'workflow'}, char_budget=1600)
assert query3=='' and sig3==[]
# more_fresh_after: fresh keyword beyond the budget window
content4='webhook ' + ('x'*1700) + ' credential'
query4,sig4,more4 = q.window_query(content4, kws, covered=set(), char_budget=1600)
assert more4 is True
print('ok')
")
assert_eq "query_window logic" "ok" "$qw"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation** — create `hooks/lib/query_window.py`:

```python
"""Extract a <500-token recall query from tool content, anchored on the first
keyword not yet covered this session, so each tool call surfaces new topics."""
import re

BREAKS = "\n.?!"


def _occurrences(content, keywords):
    low = content.lower()
    hits = []
    for kw in keywords:
        for m in re.finditer(r"\b" + re.escape(kw.lower()) + r"\b", low):
            hits.append((m.start(), kw.lower()))
    hits.sort()
    return hits


def _last_break_before(content, offset):
    start = 0
    for i in range(offset - 1, -1, -1):
        if content[i] in BREAKS:
            start = i + 1
            break
    # trim leading whitespace
    while start < offset and content[start] in " \t\n":
        start += 1
    return start


def window_query(content, keywords, covered, char_budget=1600):
    """Returns (query, signature_list, more_fresh_after).
    covered: set of keywords whose topic is still active (not stale)."""
    content = content or ""
    hits = _occurrences(content, keywords)
    fresh_hits = [(off, kw) for off, kw in hits if kw not in covered]
    if not fresh_hits:
        return "", [], False

    first_off = fresh_hits[0][0]
    start = _last_break_before(content, first_off)
    end = start + char_budget
    query = content[start:end].strip()

    sig = sorted({kw for off, kw in fresh_hits if start <= off < end})
    more_fresh_after = any(off >= end for off, kw in fresh_hits)
    return query, sig, more_fresh_after
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh`
Expected: PASS (`ok`).

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/query_window.py tests/test-backstop.sh
git commit -m "query_window: fresh-keyword-anchored query extraction"
```

---

## Task 8: `backstop-recall.sh` (PostToolUse orchestrator) + content extraction

**Files:** Create `hooks/backstop-recall.sh`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append (drives the end-to-end decision via stdin payloads, mocking recall via the CLI fixture env):

```bash
# Task 8: PostToolUse orchestrator end-to-end (recall mocked via fixture).
export RECALL_CLI_TEST=1
export RECALL_FIXTURE="$SCRIPT_DIR/fixtures/recall-with-source-facts.json"
SID="backstop-test-$$"
rm -f "${TMPDIR:-/tmp}/n8n-knowledge-backstop/${SID}.json"
mk(){ python3 -c "import json,sys; print(json.dumps({'session_id':'$SID','cwd':'$SCRIPT_DIR','tool_name':sys.argv[1],'tool_input':json.loads(sys.argv[2])}))" "$1" "$2"; }

# First Edit touching n8n -> should inject (n8n keyword present so should_recall passes)
out1=$(echo "$(mk Edit '{"file_path":"x.js","new_string":"set up an n8n webhook node here"}')" | bash "$SCRIPT_DIR/../hooks/backstop-recall.sh")
assert_contains "first edit injects result" "<result" "$out1"
assert_contains "output is PostToolUse" '"hookEventName": "PostToolUse"' "$out1"
# Immediate repeat same topic -> skip (no new/stale topic)
out2=$(echo "$(mk Edit '{"file_path":"x.js","new_string":"another n8n webhook tweak"}')" | bash "$SCRIPT_DIR/../hooks/backstop-recall.sh")
assert_eq "repeat topic injects nothing" "" "$out2"
# Non-trigger tool -> nothing, but counts
out3=$(echo "$(mk Read '{"file_path":"x.js"}')" | bash "$SCRIPT_DIR/../hooks/backstop-recall.sh")
assert_eq "read tool injects nothing" "" "$out3"
unset RECALL_CLI_TEST RECALL_FIXTURE
rm -f "${TMPDIR:-/tmp}/n8n-knowledge-backstop/${SID}.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — `backstop-recall.sh` missing.

- [ ] **Step 3: Write minimal implementation** — create `hooks/backstop-recall.sh`:

```bash
#!/usr/bin/env bash
# PostToolUse backstop recall. Never blocks: any failure -> exit 0, no output.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/detect-n8n.sh" 2>/dev/null || exit 0

[ "${CLAUDE_PLUGIN_OPTION_enableBackstopRecall:-true}" = "false" ] && exit 0

INPUT=$(cat 2>/dev/null) || exit 0
read_field(){ printf '%s' "$INPUT" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$1',''))" 2>/dev/null; }
SID=$(read_field session_id); TOOL=$(read_field tool_name); CWD=$(read_field cwd)
[ -z "$TOOL" ] && exit 0

CAP="${CLAUDE_PLUGIN_OPTION_backstopRecallCap:-4}"
BUDGET="${CLAUDE_PLUGIN_OPTION_backstopRecallBudget:-high}"
MAXTOK="${CLAUDE_PLUGIN_OPTION_backstopRecallMaxTokens:-8000}"

# Always count the tool call; do full logic only for triggers.
case "$TOOL" in
  Edit|Write|Task) IS_TRIGGER=1 ;;
  *) IS_TRIGGER=0 ;;
esac

# Extract content + run decision in one python pass; emits TSV: DECISION\tQUERY\tMORE
RESULT=$(printf '%s' "$INPUT" | KW="$(resolve_trigger_keywords)" CAP="$CAP" IS_TRIGGER="$IS_TRIGGER" python3 -c '
import json,sys,os
sys.path.insert(0, "'"$LIB_DIR"'")
import backstop_state as st, query_window as qw
d=json.load(sys.stdin)
sid=d.get("session_id",""); tool=d.get("tool_name","")
ti=d.get("tool_input",{}) or {}
state=st.load_state(sid)
state["total_calls"]+=1
fire=False; query=""; more=False
if os.environ.get("IS_TRIGGER")=="1":
    state["trigger_calls"]+=1
    if tool in ("Edit","Write"):
        content=ti.get("new_string") or ti.get("content") or ""
    elif tool=="Task":
        content=(ti.get("description","")+"\n"+ti.get("prompt","")).strip()
    else:
        content=""
    kws=os.environ.get("KW","").split()
    covered=st.active_covered(state)
    query,sig,more=qw.window_query(content, kws, covered)
    if query and sig and st.decide(state, sig, int(os.environ.get("CAP","4"))):
        st.record(state, sig)
        fire=True
st.save_state(sid, state)
print(("FIRE" if fire else "SKIP")+"\t"+query.replace("\t"," ").replace("\n"," ")+"\t"+("1" if more else "0"))
' 2>/dev/null) || exit 0

DEC="${RESULT%%	*}"
REST="${RESULT#*	}"; QUERY="${REST%%	*}"; MORE="${REST##*	}"
[ "$DEC" != "FIRE" ] && exit 0

# Gate on should_recall (explicit n8n / codebase tier) using the windowed query.
[ "$(should_recall "$QUERY" "$CWD")" != "yes" ] && exit 0

BLOCK=$(bash "$LIB_DIR/recall-cli.sh" "$QUERY" "$BUDGET" "$MAXTOK" 2>/dev/null) || exit 0
[ -z "$BLOCK" ] && exit 0

if [ "$MORE" = "1" ]; then
  BLOCK="$BLOCK
note: this recall covered the first new n8n topic in your edit; the query was capped at 500 tokens, so other topics you just wrote may be uncovered — run a manual recall if you need them."
fi

HEADER="*** n8n Knowledge Base — context refresh (after $TOOL) ***"
CTX="$HEADER
$BLOCK"
python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':sys.argv[1]}}))" "$CTX" 2>/dev/null || exit 0
```

Note: `should_recall` requires the explicit `n8n` token (or an n8n codebase) — the Task-8 test deliberately includes "n8n" in the edit content so the gate passes in a non-n8n cwd.

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x hooks/backstop-recall.sh && bash tests/test-backstop.sh`
Expected: PASS — first edit injects a PostToolUse block; repeat + Read inject nothing.

- [ ] **Step 5: Commit**

```bash
git add hooks/backstop-recall.sh tests/test-backstop.sh
git commit -m "backstop-recall.sh: PostToolUse orchestrator (decide + inject)"
```

---

## Task 9: Register hooks + config

**Files:** Modify `hooks/hooks.json`, `.claude-plugin/plugin.json`. Test: `tests/test-backstop.sh`.

- [ ] **Step 1: Write the failing test** — append:

```bash
# Task 9: registration + config present.
hj=$(cat "$SCRIPT_DIR/../hooks/hooks.json")
assert_contains "PostToolUse registered" "PostToolUse" "$hj"
assert_contains "backstop-recall registered" "backstop-recall.sh" "$hj"
pj=$(cat "$SCRIPT_DIR/../.claude-plugin/plugin.json")
assert_contains "config enableBackstopRecall" "enableBackstopRecall" "$pj"
assert_contains "config backstopRecallMaxTokens" "backstopRecallMaxTokens" "$pj"
assert_contains "config triggerKeywords" "triggerKeywords" "$pj"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-backstop.sh`
Expected: FAIL — not registered yet.

- [ ] **Step 3: Implement** — set `hooks/hooks.json` to:

```json
{
  "version": 1,
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/auto-recall.sh\"", "timeout": 10 } ] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [ { "type": "command", "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/backstop-recall.sh\"", "timeout": 8 } ] }
    ],
    "PreToolUse": [
      { "matcher": "Task", "hooks": [ { "type": "command", "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/backstop-subagent.sh\"", "timeout": 8 } ] }
    ]
  }
}
```

Add these keys to the `userConfig` object in `.claude-plugin/plugin.json` (alongside the existing `enableAutoRecall`/`showRecallResults`):

```json
"enableBackstopRecall": { "type": "boolean", "title": "Enable Backstop Recall", "description": "Refresh n8n context during agent reasoning (after Edit/Write/Task). Disable to save tokens.", "default": true },
"backstopRecallCap": { "type": "number", "title": "Backstop Recall Cap", "description": "Max backstop recalls per session.", "default": 4 },
"backstopRecallMaxTokens": { "type": "number", "title": "Backstop Recall Max Tokens", "description": "Returned-context size cap per backstop recall.", "default": 8000 },
"backstopRecallBudget": { "type": "string", "title": "Backstop Recall Budget", "description": "Hindsight recall effort: low, mid, or high.", "default": "high" },
"enableSubagentInjection": { "type": "boolean", "title": "Inject Context Into Subagents", "description": "Prepend n8n context into Task subagent prompts (experimental).", "default": false },
"triggerKeywords": { "type": "string", "title": "Trigger Keywords", "description": "Comma list of broad n8n trigger keywords used in n8n codebases. Use the token DEFAULTS to include the built-ins (workflow, node, trigger, webhook, credential, expression, execution). Example: 'DEFAULTS, mynode'. Leave blank for defaults.", "default": "" }
```

(`backstop-subagent.sh` is created in Task 10; registering its hook now is fine — until that file exists, run Step 4 after Task 10, OR create an empty `exit 0` stub first: `printf '#!/usr/bin/env bash\nexit 0\n' > hooks/backstop-subagent.sh && chmod +x hooks/backstop-subagent.sh`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh` then `bash tests/run-all.sh`
Expected: Task 9 PASS; full suite green; both JSON files valid (`python3 -m json.tool hooks/hooks.json` and `.claude-plugin/plugin.json` succeed).

- [ ] **Step 5: Commit**

```bash
git add hooks/hooks.json .claude-plugin/plugin.json hooks/backstop-subagent.sh tests/test-backstop.sh
git commit -m "Register PostToolUse/PreToolUse backstop hooks + userConfig"
```

---

## Task 10: `backstop-subagent.sh` (PreToolUse-Task, gated) + verify `updatedInput`

**Files:** Create/replace `hooks/backstop-subagent.sh`; Test: `tests/test-backstop.sh`.

- [ ] **Step 1: VERIFY `updatedInput` first (manual probe — orchestrator only).** Before trusting the path, run a throwaway PreToolUse hook on a real Task that prepends a sentinel to `prompt` via `updatedInput`, and confirm the subagent sees it. If `updatedInput` for Task is NOT honored, leave `enableSubagentInjection` default false and ship the hook as a no-op-when-disabled; note the finding in the commit. (This is a runtime capability check, not a unit test.)

- [ ] **Step 2: Write the failing test** — append:

```bash
# Task 10: subagent injection gated; when enabled, prepends block to prompt.
export RECALL_CLI_TEST=1
export RECALL_FIXTURE="$SCRIPT_DIR/fixtures/recall-with-source-facts.json"
SID2="backstop-sub-$$"; rm -f "${TMPDIR:-/tmp}/n8n-knowledge-backstop/${SID2}.json"
payload=$(python3 -c "import json;print(json.dumps({'session_id':'$SID2','cwd':'$SCRIPT_DIR','tool_name':'Task','tool_input':{'description':'n8n','prompt':'build an n8n webhook workflow'}}))")
# disabled (default) -> no updatedInput
off=$(echo "$payload" | bash "$SCRIPT_DIR/../hooks/backstop-subagent.sh")
assert_eq "subagent injection off by default" "" "$off"
# enabled -> updatedInput.prompt prefixed with <result>
on=$(echo "$payload" | CLAUDE_PLUGIN_OPTION_enableSubagentInjection=true bash "$SCRIPT_DIR/../hooks/backstop-subagent.sh")
assert_contains "enabled returns updatedInput" "updatedInput" "$on"
assert_contains "updatedInput carries result block" "<result" "$on"
unset RECALL_CLI_TEST RECALL_FIXTURE
rm -f "${TMPDIR:-/tmp}/n8n-knowledge-backstop/${SID2}.json"
```

- [ ] **Step 3: Implement** — replace `hooks/backstop-subagent.sh` with:

```bash
#!/usr/bin/env bash
# PreToolUse on Task: optionally prepend n8n context into the subagent prompt.
# Gated by enableSubagentInjection (default false). Never blocks.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
[ "${CLAUDE_PLUGIN_OPTION_enableSubagentInjection:-false}" != "true" ] && exit 0
[ "${CLAUDE_PLUGIN_OPTION_enableBackstopRecall:-true}" = "false" ] && exit 0
source "$LIB_DIR/detect-n8n.sh" 2>/dev/null || exit 0

INPUT=$(cat 2>/dev/null) || exit 0
CWD=$(printf '%s' "$INPUT" | python3 -c "import json,sys;print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
CAP="${CLAUDE_PLUGIN_OPTION_backstopRecallCap:-4}"
BUDGET="${CLAUDE_PLUGIN_OPTION_backstopRecallBudget:-high}"
MAXTOK="${CLAUDE_PLUGIN_OPTION_backstopRecallMaxTokens:-8000}"

DEC=$(printf '%s' "$INPUT" | KW="$(resolve_trigger_keywords)" CAP="$CAP" python3 -c '
import json,sys,os
sys.path.insert(0,"'"$LIB_DIR"'")
import backstop_state as st, query_window as qw
d=json.load(sys.stdin); sid=d.get("session_id",""); ti=d.get("tool_input",{}) or {}
state=st.load_state(sid); state["total_calls"]+=1; state["trigger_calls"]+=1
content=(ti.get("description","")+"\n"+ti.get("prompt","")).strip()
query,sig,more=qw.window_query(content, os.environ.get("KW","").split(), st.active_covered(state))
fire=bool(query and sig and st.decide(state,sig,int(os.environ.get("CAP","4"))))
if fire: st.record(state,sig)
st.save_state(sid,state)
print(("FIRE" if fire else "SKIP")+"\t"+query.replace("\t"," ").replace("\n"," "))
' 2>/dev/null) || exit 0
[ "${DEC%%	*}" != "FIRE" ] && exit 0
QUERY="${DEC#*	}"
[ "$(should_recall "$QUERY" "$CWD")" != "yes" ] && exit 0

BLOCK=$(bash "$LIB_DIR/recall-cli.sh" "$QUERY" "$BUDGET" "$MAXTOK" 2>/dev/null) || exit 0
[ -z "$BLOCK" ] && exit 0

printf '%s' "$INPUT" | BLOCK="$BLOCK" python3 -c '
import json,sys,os
d=json.load(sys.stdin); ti=d.get("tool_input",{}) or {}
header="*** n8n Knowledge Base — context for this subagent ***\n"
ti["prompt"]=header+os.environ["BLOCK"]+"\n\n"+ti.get("prompt","")
print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":ti}}))
' 2>/dev/null || exit 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test-backstop.sh`
Expected: PASS — off by default; enabled returns `updatedInput` with the `<result>` block.

- [ ] **Step 5: Commit**

```bash
git add hooks/backstop-subagent.sh tests/test-backstop.sh
git commit -m "backstop-subagent.sh: gated PreToolUse-Task context injection"
```

---

## Task 11: README docs + version bump

**Files:** Modify `README.md`, `.claude-plugin/plugin.json`.

- [ ] **Step 1: Add a README section** — append to `README.md` a "Backstop recall (mid-turn context)" section that documents: what it does (refreshes context after Edit/Write/Task and into subagents), the config knobs and defaults, and the `triggerKeywords` `DEFAULTS` sentinel **with the current default list spelled out**: `workflow, node, trigger, webhook, credential, expression, execution`. Include the three `triggerKeywords` examples (extend with `DEFAULTS, x`, replace, reset).

- [ ] **Step 2: Bump version** — in `.claude-plugin/plugin.json` change `"version": "0.3.3"` to `"version": "0.4.0"` (new feature; if a patch is preferred use `0.3.4` — confirm with Dan’s incremental-bump preference at execution time).

- [ ] **Step 3: Run full suite**

Run: `bash tests/run-all.sh`
Expected: `ALL TESTS PASSED`.

- [ ] **Step 4: Commit**

```bash
git add README.md .claude-plugin/plugin.json
git commit -m "Docs + version bump for backstop recall"
```

---

## Self-Review (completed)

- **Spec coverage:** mechanism constraint → Tasks 8 (PostToolUse `additionalContext`) + 10 (PreToolUse `updatedInput`). Triggers/refresh/cap → Tasks 6–8. Budget/8000/source_facts/0.3.3 → Tasks 1–3, 8. Query windowing + truncation note → Tasks 7–8. Configurable keywords + DEFAULTS → Task 5 + README Task 11. Unify skill → Tasks 3–4. Config knobs → Task 9. Never-block → `exit 0` paths in Tasks 8/10 + 8s timeouts in Task 9. Tests → every task + `test-backstop.sh`.
- **Placeholder scan:** none — every step has concrete code/commands/expected output. Task 10 Step 1 is an explicit runtime capability probe (not a placeholder); the hook ships safe-by-default regardless of outcome.
- **Type consistency:** `do_recall(query, budget, max_tokens)`; `format_results.py --event/--bare`; `recall-cli.sh <query> [budget] [max_tokens]`; `resolve_trigger_keywords` (space-separated); `window_query(content, keywords, covered, char_budget)` → `(query, sig_list, more)`; `backstop_state`: `new_state/load_state/save_state/is_stale/active_covered/decide/record`. Signatures match across all call sites (Tasks 8, 10).

## Notes
- Subagents can't run Bash here — orchestrator runs all test/commit steps (header reminder).
- `recall-cli.sh` test hook (`RECALL_CLI_TEST`/`RECALL_FIXTURE`) keeps tests offline/deterministic; production path calls the live endpoint.
- After merge, run `/plugin marketplace update n8n-local` + `/plugin update` to install the new version (symlinked marketplace; version bump required).
