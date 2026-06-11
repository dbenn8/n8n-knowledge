# Local Hygiene + Version Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **ENVIRONMENT CONSTRAINT (Dan's machine):** dispatched subagents CANNOT run Bash.
> Subagents author files via Write/Edit and report the exact commands; the
> ORCHESTRATOR runs every test (red→green) and makes every commit. Commits only
> with Dan's explicit go-ahead; never include AI attribution in messages.
>
> **Conflict boundary with Team 1:** Team 1 owns `hooks/lib/format_results.py`,
> the README *eval section*, and new test files under `tests/`. This plan owns
> shell hooks' path handling, `scripts/eval/run-eval-v2.sh` traps, the README
> *version line*, `PRIVACY.md`, and `CHANGELOG.md`. Do not touch Team 1's files.

**Goal:** Move plugin state and the debug log out of world-readable `/tmp` into a per-user 0700 directory, make eval credential cleanup survive hard kills, and pin README/plugin.json version parity with a test.

**Architecture:** One new sourced helper (`hooks/lib/runtime_dirs.sh`) defines the per-user runtime dir and debug-log path; every hook and Python lib that hardcodes `/tmp/n8n-knowledge-debug.log` or `${TMPDIR}/n8n-knowledge-workflow-validation` switches to it. An env override (`N8N_KNOWLEDGE_RUNTIME_DIR`) keeps tests hermetic. Signal traps in the eval harness extend the existing EXIT cleanup.

**Tech Stack:** bash, Python 3 stdlib, existing bash test harness style (see `tests/test-validator-budget-line.sh` for the assert pattern).

**Why (context for the implementer):**
- The debug log (`/tmp/n8n-knowledge-debug.log`) contains user PROMPT TEXT and is world-readable on shared machines — contradicting PRIVACY.md's "your machine only" framing. Session state files in `/tmp` are similarly exposed and collide across users.
- `run-eval-v2.sh` copies `~/.claude.json` + `~/.claude/.credentials.json` into a scratch dir removed by an EXIT trap; a hard kill (SIGKILL, TaskStop) skips the trap and strands credentials in `$TMPDIR` (observed 2026-06-11).
- `README.md` says **v0.3.7**; `.claude-plugin/plugin.json` says `0.3.8`. Nothing pins them together.

**Current hardcoded-path inventory (verify with grep before editing — the cleanup
sessions move fast; re-run `grep -rn "n8n-knowledge-debug.log" hooks/ tests/ README.md PRIVACY.md`
and `grep -rn "n8n-knowledge-workflow-validation" hooks/ tests/` and update this list):**
- `hooks/auto-recall.sh` — debug log writes (~3 sites: skip-log, summary writer, comment)
- `hooks/lib/recall_common.sh` — `recall_post FAIL` log line
- `hooks/lib/debug_formatter.py` — consumed by the summary writer (path passed in or hardcoded — check)
- `hooks/backstop-recall.sh` — check for log/state references
- `hooks/validate-workflow.sh` — `STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"`
- `hooks/lib/backstop_state.py` — `base = os.environ.get("TMPDIR", "/tmp")`
- `tests/test-auto-recall.sh`, `tests/test-validator-budget-line.sh`, others — assert against the old paths
- `README.md` (`tail -f /tmp/n8n-knowledge-debug.log`), `PRIVACY.md` (log description)

---

### Task 1: `runtime_dirs.sh` helper + Python twin

**Files:**
- Create: `hooks/lib/runtime_dirs.sh`
- Create: `hooks/lib/runtime_dirs.py`
- Test: `tests/test-runtime-dirs.sh`

- [ ] **Step 1 (subagent): Write the failing test**

```bash
#!/usr/bin/env bash
set -uo pipefail
# Pins the per-user runtime dir contract:
#  R1 default dir is under the user's cache home, mode 0700
#  R2 N8N_KNOWLEDGE_RUNTIME_DIR overrides it (hermetic tests)
#  R3 debug log lives inside it and is created 0600
#  R4 bash and python helpers agree on the same paths

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$SCRIPT_DIR/../hooks/lib"
PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== runtime dirs tests ==="

# R2 + R1: override respected, dir created 0700
T=$(mktemp -d)/rt
OUT=$(N8N_KNOWLEDGE_RUNTIME_DIR="$T" bash -c '
  source "'"$LIB"'/runtime_dirs.sh"
  nk_runtime_init
  echo "$NK_RUNTIME_DIR|$NK_DEBUG_LOG|$NK_STATE_DIR"')
DIR=${OUT%%|*}
[ "$DIR" = "$T" ] && ok "R2 override respected" || bad "R2 override ignored (got $DIR)"
PERM=$(stat -f '%Lp' "$T" 2>/dev/null || stat -c '%a' "$T" 2>/dev/null)
[ "$PERM" = "700" ] && ok "R1 dir mode 0700" || bad "R1 dir mode is $PERM"

# R3: nk_debug_log_write creates the log 0600 and appends
N8N_KNOWLEDGE_RUNTIME_DIR="$T" bash -c '
  source "'"$LIB"'/runtime_dirs.sh"; nk_runtime_init
  nk_debug_log_write "hello from test"'
LOG="$T/debug.log"
grep -q "hello from test" "$LOG" && ok "R3a log written" || bad "R3a log missing"
LPERM=$(stat -f '%Lp' "$LOG" 2>/dev/null || stat -c '%a' "$LOG" 2>/dev/null)
[ "$LPERM" = "600" ] && ok "R3b log mode 0600" || bad "R3b log mode is $LPERM"

# R4: python twin agrees
PYOUT=$(N8N_KNOWLEDGE_RUNTIME_DIR="$T" python3 -c "
import sys; sys.path.insert(0, '$LIB')
from runtime_dirs import runtime_dir, debug_log_path, state_dir
print(f'{runtime_dir()}|{debug_log_path()}|{state_dir()}')")
[ "$PYOUT" = "$OUT" ] && ok "R4 bash/python parity" || bad "R4 mismatch: bash=$OUT py=$PYOUT"

rm -rf "$(dirname "$T")"
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2 (orchestrator): Run to verify red** — `bash tests/test-runtime-dirs.sh` → FAIL ("no such file: runtime_dirs.sh").

- [ ] **Step 3 (subagent): Implement the bash helper**

```bash
#!/usr/bin/env bash
# runtime_dirs.sh — per-user runtime paths for n8n-knowledge state + debug log.
#
# Everything used to live in world-readable /tmp; the debug log contains user
# PROMPT TEXT, so on shared machines that leaked prompts to other accounts and
# contradicted PRIVACY.md. One dir, mode 0700, log files 0600.
#
# Override with N8N_KNOWLEDGE_RUNTIME_DIR (tests use this to stay hermetic).

nk_runtime_init() {
  NK_RUNTIME_DIR="${N8N_KNOWLEDGE_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/n8n-knowledge}"
  NK_DEBUG_LOG="$NK_RUNTIME_DIR/debug.log"
  NK_STATE_DIR="$NK_RUNTIME_DIR/state"
  mkdir -p "$NK_STATE_DIR" 2>/dev/null || true
  chmod 700 "$NK_RUNTIME_DIR" 2>/dev/null || true
  export NK_RUNTIME_DIR NK_DEBUG_LOG NK_STATE_DIR
}

nk_debug_log_write() {
  # $1 = line. Never fails the caller.
  [ -n "${NK_DEBUG_LOG:-}" ] || nk_runtime_init
  { umask 077; printf '%s\n' "$1" >> "$NK_DEBUG_LOG"; } 2>/dev/null || true
}
```

- [ ] **Step 4 (subagent): Implement the Python twin**

```python
"""Per-user runtime paths — Python twin of runtime_dirs.sh.

Keep the two in lockstep; tests/test-runtime-dirs.sh asserts parity.
"""
import os


def runtime_dir():
    return os.environ.get(
        "N8N_KNOWLEDGE_RUNTIME_DIR",
        os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "n8n-knowledge"),
    )


def debug_log_path():
    return os.path.join(runtime_dir(), "debug.log")


def state_dir():
    return os.path.join(runtime_dir(), "state")
```

- [ ] **Step 5 (orchestrator): Green** — `bash tests/test-runtime-dirs.sh` → `Results: 5 passed, 0 failed` (R2, R1, R3a, R3b, R4).

- [ ] **Step 6 (orchestrator): Commit** (with go-ahead)

```bash
git add hooks/lib/runtime_dirs.sh hooks/lib/runtime_dirs.py tests/test-runtime-dirs.sh
git commit -m "feat: per-user 0700 runtime dir for state and debug log"
```

### Task 2: Migrate every hardcoded path to the helper

**Files (re-grep first — see inventory above):**
- Modify: `hooks/auto-recall.sh`, `hooks/backstop-recall.sh`, `hooks/validate-workflow.sh`, `hooks/lib/recall_common.sh`, `hooks/lib/backstop_state.py`
- Modify: every test that references the old paths (run `grep -rln "n8n-knowledge-debug.log\|n8n-knowledge-workflow-validation" tests/`)
- Modify: `tests/run-all.sh` if the new test isn't auto-discovered

- [ ] **Step 1 (subagent): Bash hooks** — at the top of each hook (after existing `source` lines): `source "$SCRIPT_DIR/lib/runtime_dirs.sh"; nk_runtime_init`. Replace every literal `/tmp/n8n-knowledge-debug.log` with `$NK_DEBUG_LOG` (inside inline Python heredocs, pass it via env or argv — do NOT interpolate shell vars into Python string literals). Replace `STATE_DIR="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation"` in `validate-workflow.sh` with `STATE_DIR="$NK_STATE_DIR/workflow-validation"`.
- [ ] **Step 2 (subagent): Python libs** — `backstop_state.py`: replace the `TMPDIR` base with `from runtime_dirs import state_dir` and base session files under `state_dir()`. Preserve the existing `path_for(session_id)` sanitization unchanged.
- [ ] **Step 3 (subagent): Tests** — update old-path assertions to set `N8N_KNOWLEDGE_RUNTIME_DIR` to a mktemp dir and assert against `$DIR/debug.log` (this also makes those tests hermetic for the first time — they currently share the real log).
- [ ] **Step 4 (orchestrator): Run the full suite** — `bash tests/run-all.sh` → all passing. Then a live check: trigger one auto-recall (pipe a prompt JSON into `hooks/auto-recall.sh` with repo cwd) and confirm the log lands in `~/.cache/n8n-knowledge/debug.log` and NOT in `/tmp`.
- [ ] **Step 5 (orchestrator): Commit** (with go-ahead)

```bash
git add hooks/ tests/
git commit -m "refactor: route all state and debug logging through the per-user runtime dir"
```

### Task 3: Docs follow the move

**Files:**
- Modify: `README.md` — ONLY the debug-log path references (the `tail -f /tmp/n8n-knowledge-debug.log` snippet and the `debugRecall` rows) → `tail -f ~/.cache/n8n-knowledge/debug.log`; and the version line `**v0.3.7**` → `**v0.3.8**` (Task 5 pins this)
- Modify: `PRIVACY.md` — update the Local Transparency Log paragraph: new path, plus one honest sentence: "The log is created with owner-only permissions (0600) in your user cache directory; on machines you share, other accounts cannot read it."
- Modify: `CHANGELOG.md` — add an entry under 0.3.8 (or 0.3.9 if 0.3.8 already shipped — check `git log --oneline -5` for the release commit): "State and debug log moved from /tmp to ~/.cache/n8n-knowledge (0700/0600). Set N8N_KNOWLEDGE_RUNTIME_DIR to override. Old /tmp files are no longer read; delete them manually if present."

- [ ] **Step 1 (subagent): Make the three edits above** (exact text included — no improvisation on PRIVACY.md beyond the quoted sentence).
- [ ] **Step 2 (orchestrator): Verify** — `grep -rn "n8n-knowledge-debug" README.md PRIVACY.md` → zero `/tmp` hits.
- [ ] **Step 3 (orchestrator): Commit** (with go-ahead)

```bash
git add README.md PRIVACY.md CHANGELOG.md
git commit -m "docs: per-user debug log path and privacy wording"
```

### Task 4: Eval harness — credential cleanup survives hard kills

**Files:**
- Modify: `scripts/eval/run-eval-v2.sh` (the scratch-config block added 2026-06-11; search for `EVAL_SCRATCH_CONFIG_DIR`)
- Test: `tests/test-eval-scratch-cleanup.sh`

- [ ] **Step 1 (subagent): Write the failing test**

```bash
#!/usr/bin/env bash
set -uo pipefail
# The eval harness copies ~/.claude credentials into a scratch dir. EXIT traps
# don't fire on SIGTERM-by-default-handler or SIGKILL paths used by TaskStop.
# Contract: the cleanup function is registered for EXIT *and* TERM/INT, and a
# TERM'd harness leaves no scratch dir behind. SIGKILL can never be trapped —
# the residual defense for that is the startup sweep (asserted here too).
PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== eval scratch cleanup tests ==="

# T1: TERM'd process removes its scratch dir (simulate the harness trap block)
SCRATCH_PARENT=$(mktemp -d)
bash -c '
  EVAL_SCRATCH_CONFIG_DIR=$(mktemp -d "'"$SCRATCH_PARENT"'/n8n-eval-claude-config.XXXXXX")
  cleanup() { rm -rf "$EVAL_SCRATCH_CONFIG_DIR"; }
  trap cleanup EXIT
  trap "cleanup; exit 143" TERM INT
  echo "$EVAL_SCRATCH_CONFIG_DIR" > "'"$SCRATCH_PARENT"'/path.txt"
  sleep 30' &
PID=$!
sleep 1
kill -TERM $PID
wait $PID 2>/dev/null
DIR=$(cat "$SCRATCH_PARENT/path.txt")
[ ! -d "$DIR" ] && ok "T1 TERM removes scratch dir" || bad "T1 scratch dir survived TERM"

# T2: the real harness contains the TERM/INT trap line (source-level pin)
grep -q 'trap .*TERM INT' "$(dirname "$0")/../scripts/eval/run-eval-v2.sh" \
  && ok "T2 harness registers TERM/INT cleanup" || bad "T2 harness missing TERM/INT trap"

# T3: startup sweep removes stale scratch dirs from prior killed runs
grep -q 'n8n-eval-claude-config' "$(dirname "$0")/../scripts/eval/run-eval-v2.sh" \
  && grep -qE 'rm -rf .*n8n-eval-claude-config\.\*|stale' "$(dirname "$0")/../scripts/eval/run-eval-v2.sh" \
  && ok "T3 startup sweep present" || bad "T3 no startup sweep for stale scratch dirs"

rm -rf "$SCRATCH_PARENT"
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2 (orchestrator): Red** — T2 and T3 FAIL against current harness.

- [ ] **Step 3 (subagent): Implement in `run-eval-v2.sh`** — replace the single-line EXIT trap in the scratch-config block with:

```bash
# Clean up credential copies on EVERY exit path we can trap. SIGKILL cannot be
# trapped — the startup sweep below handles dirs stranded by a hard kill.
cleanup_scratch_config() { rm -rf "$EVAL_SCRATCH_CONFIG_DIR"; }
trap cleanup_scratch_config EXIT
trap 'cleanup_scratch_config; exit 130' INT
trap 'cleanup_scratch_config; exit 143' TERM

# Startup sweep: remove stale scratch dirs from previously hard-killed runs.
rm -rf "${TMPDIR:-/tmp}"/n8n-eval-claude-config.* 2>/dev/null || true
```

Place the startup sweep BEFORE creating the new scratch dir (mktemp will then create the only one present).

- [ ] **Step 4 (orchestrator): Green** — `bash tests/test-eval-scratch-cleanup.sh` → `3 passed`. Then `bash -n scripts/eval/run-eval-v2.sh`.

- [ ] **Step 5 (orchestrator): Commit** (with go-ahead)

```bash
git add scripts/eval/run-eval-v2.sh tests/test-eval-scratch-cleanup.sh
git commit -m "fix: eval credential scratch dir survives TERM/INT and stale dirs swept at startup"
```

### Task 5: Docs parity test (version + debug-log path)

**Files:**
- Create: `tests/test-docs-parity.sh`
- Modify: `README.md` version line (coordinated in Task 3), `CHANGELOG.md` if the top entry doesn't match

**Why the path pin (Dan, 2026-06-11):** the README's `tail -f` instruction is how
users watch what the plugin injects live. It already went stale once (/tmp -> cache
move). The test derives the canonical path from the SAME helper the code uses
(`runtime_dirs.py`), so if the location ever moves again, the suite fails until
every README mention is updated.

- [ ] **Step 1 (subagent): Write the failing test**

```bash
#!/usr/bin/env bash
set -uo pipefail
# README, plugin.json, and the top CHANGELOG entry must agree on the version.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

PLUGIN_V=$(python3 -c "import json;print(json.load(open('$ROOT/.claude-plugin/plugin.json'))['version'])")
README_V=$(grep -m1 -oE '\*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*' "$ROOT/README.md" | tr -d '*v')
CHANGELOG_V=$(grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+' "$ROOT/CHANGELOG.md")

[ "$README_V" = "$PLUGIN_V" ] \
  && ok "README ($README_V) matches plugin.json ($PLUGIN_V)" \
  || bad "README says $README_V but plugin.json says $PLUGIN_V"
[ "$CHANGELOG_V" = "$PLUGIN_V" ] \
  && ok "CHANGELOG top entry ($CHANGELOG_V) matches plugin.json" \
  || bad "CHANGELOG top entry is $CHANGELOG_V, plugin.json is $PLUGIN_V"

# --- Debug-log path: README tail instruction must match the code's canonical path ---
# Derive the user-facing form (~/.cache/...) from runtime_dirs.py with a clean env,
# so this test fails whenever the log location moves but the README doesn't.
CANON=$(env -u N8N_KNOWLEDGE_RUNTIME_DIR -u XDG_CACHE_HOME python3 -c "
import sys; sys.path.insert(0, '$ROOT/hooks/lib')
import os
from runtime_dirs import debug_log_path
print(debug_log_path().replace(os.path.expanduser('~'), '~'))")
MENTIONS=$(grep -cF "$CANON" "$ROOT/README.md")
[ "$MENTIONS" -ge 1 ] \
  && ok "README mentions canonical debug log path ($CANON x$MENTIONS)" \
  || bad "README does not mention canonical debug log path $CANON"
STALE=$(grep -c "/tmp/n8n-knowledge-debug.log" "$ROOT/README.md" "$ROOT/PRIVACY.md" | awk -F: '{s+=$2} END{print s}')
[ "$STALE" -eq 0 ] \
  && ok "no stale /tmp debug-log references in README/PRIVACY" \
  || bad "$STALE stale /tmp debug-log reference(s) remain in README/PRIVACY"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2 (orchestrator): Red** — version check FAILS (0.3.7 vs 0.3.8); path checks FAIL until Task 3 lands (README still says /tmp). Note: this test depends on Task 1 (runtime_dirs.py) existing — run Task 5 after Tasks 1 and 3.
- [ ] **Step 3 (subagent): Fix** — README version line → `**v0.3.8**` (if not already done in Task 3).
- [ ] **Step 4 (orchestrator): Green**, add to `tests/run-all.sh`, then full suite: `bash tests/run-all.sh`.
- [ ] **Step 5 (orchestrator): Commit** (with go-ahead)

```bash
git add tests/test-docs-parity.sh tests/run-all.sh README.md
git commit -m "test: pin docs parity — version strings and debug-log path"
```

## Self-review notes
- Task 2's grep-first instruction is deliberate: the path inventory can drift while other sessions commit.
- The surgical-edits branch's `hooks/check-draft-stop.sh` also uses the old STATE_DIR path — it is NOT in this plan's scope (different branch); flag it in the PR/handoff notes so it migrates when that branch merges.
- SIGKILL is acknowledged as untrappable; the startup sweep is the compensating control and T3 pins it.
