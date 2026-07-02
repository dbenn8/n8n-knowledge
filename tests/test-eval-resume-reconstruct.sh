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

echo "=== eval transcript reconstruction resume tests ==="

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

GT_FILE="$WORK_DIR/ground_truth.jsonl"
cat > "$GT_FILE" <<'EOF'
{"id":"prompt-0","group":"a","prompt":"Build workflow zero"}
EOF

RESUME_SOURCE="$WORK_DIR/resume-source"
mkdir -p "$RESUME_SOURCE/bare"

cat > "$RESUME_SOURCE/bare/prompt-000-run01.json" <<'EOF'
{"session_id":"11111111-2222-4333-8444-555555555555","error":"model_invocation_timeout","is_error":true}
EOF

cat > "$RESUME_SOURCE/bare/prompt-000-run01.transcript.jsonl" <<'EOF'
{"type":"user","sessionId":"11111111-2222-4333-8444-555555555555","message":{"role":"user","content":"Build workflow zero"}}
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
  bash "$TARGET" --conditions bare --runs 1 --groups a --prompt-file-idxs 0 --resume-failed-from "$RESUME_SOURCE" > "$RUN_LOG" 2>&1
)

NEW_RESULTS_DIR="$(sed -n 's/^  Output: //p' "$RUN_LOG" | tail -1)"

if [ -n "$NEW_RESULTS_DIR" ] && [ -d "$NEW_RESULTS_DIR" ]; then
  assert_pass "runner created a new results dir for reconstructed resume" 0
else
  assert_pass "runner created a new results dir for reconstructed resume" 1
fi

if grep -q -- '--resume 11111111-2222-4333-8444-555555555555' "$FAKE_CLAUDE_LOG"; then
  assert_pass "reconstructed transcript allows resume via saved session id" 0
else
  assert_pass "reconstructed transcript allows resume via saved session id" 1
fi

if python3 - "$NEW_RESULTS_DIR/run-manifest.json" <<'PYEOF'
import json
import os
import sys

manifest = json.load(open(sys.argv[1]))
cfg = manifest.get("claude_config_dir")
slug = "-Users-danielbennett-codeNew-n8n-knowledge"
path = os.path.join(cfg, "projects", slug, "11111111-2222-4333-8444-555555555555.jsonl")
raise SystemExit(0 if cfg and os.path.exists(path) else 1)
PYEOF
then
  assert_pass "runner reconstructs the Claude project transcript under the new config dir" 0
else
  assert_pass "runner reconstructs the Claude project transcript under the new config dir" 1
fi

if python3 - "$NEW_RESULTS_DIR/bare/prompt-000-run01.session.json" "$NEW_RESULTS_DIR/bare/prompt-000-run01.meta.json" <<'PYEOF'
import json
import sys

session_payload = json.load(open(sys.argv[1]))
meta_payload = json.load(open(sys.argv[2]))
ok = session_payload.get("resume_session_used") is True and meta_payload.get("resume_session_used") is True
raise SystemExit(0 if ok else 1)
PYEOF
then
  assert_pass "reconstructed resume is recorded in session and meta sidecars" 0
else
  assert_pass "reconstructed resume is recorded in session and meta sidecars" 1
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
