#!/usr/bin/env bash
set -euo pipefail

# Pins the scratch-config credential-cleanup contract in run-eval-v2.sh. The
# harness copies ~/.claude.json and ~/.claude/.credentials.json into a scratch
# dir under $TMPDIR and must remove it on EVERY trappable exit path. A hard kill
# (TaskStop) was observed skipping a bare `trap ... EXIT` and stranding a
# credentials copy. Fix: also trap TERM/INT, and sweep stale dirs at startup.
#
# NOTE: SIGKILL (kill -9) CANNOT be trapped by any process. The startup sweep
# (an age-gated `find ... -mmin +180 -exec rm -rf`) is the compensating control
# for dirs stranded by a hard kill — T3 pins that line. The age gate ensures a
# CONCURRENTLY RUNNING harness's fresh scratch dir is never swept; T4 proves a
# fresh dir survives the sweep while an old (>3h) one is removed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$SCRIPT_DIR/../scripts/eval/run-eval-v2.sh"

PASS=0
FAIL=0

assert_pass() {
  # $1 = description, $2 = condition exit status (0 = pass)
  local desc="$1"
  if [ "$2" -eq 0 ]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc"
    FAIL=$((FAIL + 1))
  fi
}

assert_source_matches() {
  # $1 = description, $2 = regex
  local desc="$1" pattern="$2"
  if grep -Eq "$pattern" "$TARGET"; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (no source line matching /$pattern/)"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== eval scratch-config cleanup tests ==="

# --- T1 (behavioral): the exact trap pattern survives a SIGTERM ---
# Spawn a background process that mirrors the harness trap setup, kill it with
# -TERM, and assert the scratch dir it created is gone (i.e. the TERM trap fired
# its cleanup before exiting). This validates the pattern the harness uses.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

DIR_RECORD="$WORK_DIR/dir-path.txt"

bash -c '
  PARENT="$1"
  RECORD="$2"
  EVAL_SCRATCH_CONFIG_DIR=$(mktemp -d "$PARENT/n8n-eval-claude-config.XXXXXX")
  cleanup() { rm -rf "$EVAL_SCRATCH_CONFIG_DIR"; }
  trap cleanup EXIT
  trap "cleanup; exit 143" TERM
  trap "cleanup; exit 130" INT
  printf "%s" "$EVAL_SCRATCH_CONFIG_DIR" > "$RECORD"
  sleep 30
' _ "$WORK_DIR" "$DIR_RECORD" &
BG_PID=$!

# Wait for the child to register its traps and record the dir path.
for _ in $(seq 1 50); do
  [ -s "$DIR_RECORD" ] && break
  sleep 0.1
done

SCRATCH_DIR="$(cat "$DIR_RECORD" 2>/dev/null || true)"

kill -TERM "$BG_PID" 2>/dev/null || true
wait "$BG_PID" 2>/dev/null || true

if [ -n "$SCRATCH_DIR" ] && [ ! -e "$SCRATCH_DIR" ]; then
  assert_pass "TERM trap removes the scratch dir on a graceful kill" 0
else
  assert_pass "TERM trap removes the scratch dir on a graceful kill" 1
fi

# --- T2 (source pin): the TERM and INT trap registrations exist ---
assert_source_matches "source registers a TERM trap (exit 143) on cleanup_scratch_config" \
  "trap '.*cleanup_scratch_config; *exit 143.*' *TERM"
assert_source_matches "source registers an INT trap (exit 130) on cleanup_scratch_config" \
  "trap '.*cleanup_scratch_config; *exit 130.*' *INT"

# --- T3 (source pin): the age-gated startup sweep of stale scratch dirs exists ---
# Must be the age-gated find form (-mmin +180) over the scratch glob, NOT an
# unconditional rm that would clobber a concurrent run's fresh dir.
assert_source_matches "source sweeps stale n8n-eval-claude-config.* dirs via age-gated find (-mmin +180)" \
  "find .*-name 'n8n-eval-claude-config\.\*'.*-mmin \+180"

# --- T4 (behavioral): the age gate spares a FRESH dir, removes an OLD one ---
# Reproduce the exact sweep command line against an overridden TMPDIR holding one
# just-created dir and one back-dated (>3h) dir. Assert fresh survives, old gone.
SWEEP_ROOT="$(mktemp -d)"
FRESH_DIR="$(mktemp -d "$SWEEP_ROOT/n8n-eval-claude-config.XXXXXX")"
OLD_DIR="$(mktemp -d "$SWEEP_ROOT/n8n-eval-claude-config.XXXXXX")"
# Back-date the old dir well past the 3h gate (2026-01-01 00:00).
touch -mt 202601010000 "$OLD_DIR"

TMPDIR="$SWEEP_ROOT" find "${SWEEP_ROOT}" -maxdepth 1 -name 'n8n-eval-claude-config.*' -type d -mmin +180 -exec rm -rf {} + 2>/dev/null || true

if [ -d "$FRESH_DIR" ] && [ ! -e "$OLD_DIR" ]; then
  assert_pass "age-gated sweep spares the fresh scratch dir and removes the old (>3h) one" 0
else
  assert_pass "age-gated sweep spares the fresh scratch dir and removes the old (>3h) one" 1
fi
rm -rf "$SWEEP_ROOT"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
