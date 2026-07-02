#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/.."
TARGET="$REPO_DIR/scripts/eval/run-eval-v2.sh"

PASS=0
FAIL=0

assert_pass() {
  local desc="$1" status="$2"
  if [ "$status" -eq 0 ]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== eval failed-run resume tests ==="

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

GT_FILE="$WORK_DIR/ground_truth.jsonl"
cat > "$GT_FILE" <<'EOF'
{"id":"prompt-0","group":"a","prompt":"Build workflow zero"}
{"id":"prompt-1","group":"a","prompt":"Build workflow one"}
EOF

RESUME_SOURCE="$WORK_DIR/resume-source"
RESUME_CONFIG_DIR="$WORK_DIR/resume-claude-config"
mkdir -p "$RESUME_SOURCE/bare" "$RESUME_CONFIG_DIR"
PROJECT_SLUG="-Users-danielbennett-codeNew-n8n-knowledge"
mkdir -p "$RESUME_CONFIG_DIR/projects/$PROJECT_SLUG"

cat > "$RESUME_SOURCE/run-manifest.json" <<EOF
{
  "claude_config_dir": "$RESUME_CONFIG_DIR",
  "prompt_file_idxs": [0, 1]
}
EOF

cat > "$RESUME_SOURCE/bare/prompt-000-run01.json" <<'EOF'
{"session_id":"skip-sid","result":"completed","usage":{},"num_turns":1,"total_cost_usd":0}
EOF

cat > "$RESUME_SOURCE/bare/prompt-001-run01.json" <<'EOF'
{"session_id":"resume-sid-1","error":"model_invocation_timeout","is_error":true}
EOF

cat > "$RESUME_SOURCE/bare/prompt-999-run01.json" <<'EOF'
{"session_id":"should-not-seed","result":"completed","usage":{},"num_turns":1,"total_cost_usd":0}
EOF

cat > "$RESUME_SOURCE/bare/prompt-001-run01.session.json" <<'EOF'
{"condition":"bare","prompt_array_idx":1,"prompt_idx":1,"prompt_id":"prompt-1","run":1,"session_id":"resume-sid-1"}
EOF

cat > "$RESUME_CONFIG_DIR/projects/$PROJECT_SLUG/resume-sid-1.jsonl" <<'EOF'
{"type":"user","sessionId":"resume-sid-1","message":{"role":"user","content":"Build workflow one"}}
EOF

FAKE_CLAUDE_LOG="$WORK_DIR/fake-claude.log"
FAKE_CLAUDE="$WORK_DIR/fake-claude.sh"
cat > "$FAKE_CLAUDE" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
log_file="${FAKE_CLAUDE_LOG:?}"
printf '%s\n' "$*" >> "$log_file"
session_id=""
resume_id=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-id) session_id="$2"; shift 2 ;;
    --resume) resume_id="$2"; shift 2 ;;
    *) shift ;;
  esac
done
python3 - "$session_id" "$resume_id" <<'PYEOF'
import json
import sys

session_id, resume_id = sys.argv[1:3]
payload = {
    "session_id": resume_id or session_id,
    "result": "ok",
    "usage": {},
    "num_turns": 1,
    "total_cost_usd": 0,
}
print(json.dumps(payload))
PYEOF
EOF
chmod +x "$FAKE_CLAUDE"

RUN_LOG="$WORK_DIR/run.log"
(
  cd "$REPO_DIR"
  FAKE_CLAUDE_LOG="$FAKE_CLAUDE_LOG" \
  EVAL_CLAUDE_BIN="$FAKE_CLAUDE" \
  EVAL_GROUND_TRUTH_FILE="$GT_FILE" \
  EVAL_SKIP_VALIDATOR_PREFLIGHT=1 \
  bash "$TARGET" --conditions bare --runs 1 --groups a --resume-failed-from "$RESUME_SOURCE" > "$RUN_LOG" 2>&1
)

NEW_RESULTS_DIR="$(sed -n 's/^  Output: //p' "$RUN_LOG" | tail -1)"

if [ -n "$NEW_RESULTS_DIR" ] && [ -d "$NEW_RESULTS_DIR" ]; then
  assert_pass "runner created a new results dir for the resumed run" 0
else
  assert_pass "runner created a new results dir for the resumed run" 1
fi

CALL_COUNT="$(wc -l < "$FAKE_CLAUDE_LOG" | tr -d ' ')"
if [ "$CALL_COUNT" = "1" ]; then
  assert_pass "only the unfinished prompt invoked Claude" 0
else
  assert_pass "only the unfinished prompt invoked Claude" 1
fi

if grep -q -- '--resume resume-sid-1' "$FAKE_CLAUDE_LOG"; then
  assert_pass "unfinished prompt resumes via --resume using the saved session id" 0
else
  assert_pass "unfinished prompt resumes via --resume using the saved session id" 1
fi

if grep -q -- '\[bare\] p0 r1 — skipped (resume)' "$RUN_LOG"; then
  assert_pass "completed prompt is skipped from the seeded results" 0
else
  assert_pass "completed prompt is skipped from the seeded results" 1
fi

if python3 - "$NEW_RESULTS_DIR/run-manifest.json" "$RESUME_CONFIG_DIR" <<'PYEOF'
import json
import sys

manifest = json.load(open(sys.argv[1]))
ok = (
    manifest.get("claude_config_dir") == sys.argv[2]
    and manifest.get("prompt_file_idxs") == [0, 1]
    and manifest.get("resume_failed_from")
)
raise SystemExit(0 if ok else 1)
PYEOF
then
  assert_pass "run manifest captures the reused Claude config dir and prompt subset" 0
else
  assert_pass "run manifest captures the reused Claude config dir and prompt subset" 1
fi

if python3 - "$NEW_RESULTS_DIR/bare/prompt-001-run01.session.json" "$NEW_RESULTS_DIR/bare/prompt-001-run01.meta.json" <<'PYEOF'
import json
import sys

session_payload = json.load(open(sys.argv[1]))
meta_payload = json.load(open(sys.argv[2]))
ok = session_payload.get("resume_session_used") is True and meta_payload.get("resume_session_used") is True
raise SystemExit(0 if ok else 1)
PYEOF
then
  assert_pass "successful failed-run resume is recorded in session and meta sidecars" 0
else
  assert_pass "successful failed-run resume is recorded in session and meta sidecars" 1
fi

if [ ! -e "$NEW_RESULTS_DIR/bare/prompt-999-run01.json" ]; then
  assert_pass "seeded resume artifacts are limited to the selected prompt subset" 0
else
  assert_pass "seeded resume artifacts are limited to the selected prompt subset" 1
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
