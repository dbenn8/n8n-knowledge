#!/usr/bin/env bash
# Tests for the single shared validator bridge (hooks/lib/validator_bridge.js)
# and its thin eval wrapper (scripts/eval/validate-with-mcp.js).
#
# These exercise the REAL bridge file (not the mock-response path that
# test-workflow-validation.sh uses). The bridge's error contract is testable
# without n8n-mcp installed: we force install-root resolution to land on an
# empty directory so the dist/ require fails deterministically, and assert the
# structured `validator_bridge_error` JSON is emitted on stdout with exit 0.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BRIDGE="$REPO_DIR/hooks/lib/validator_bridge.js"
EVAL_WRAPPER="$REPO_DIR/scripts/eval/validate-with-mcp.js"
PASS=0; FAIL=0
assert_contains(){ local d="$1" n="$2" h="$3"; if echo "$h"|grep -Fq "$n"; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$n')"; FAIL=$((FAIL+1)); fi; }
assert_eq(){ local d="$1" e="$2" a="$3"; if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$e' got '$a')"; FAIL=$((FAIL+1)); fi; }

echo "=== validator bridge resolution / error-contract tests ==="

# Force a guaranteed n8n-mcp-absent environment: an empty install root makes the
# require(<root>/dist/...) fail deterministically regardless of what is or isn't
# installed on the host. This isolates the structured-error contract.
EMPTY_ROOT="$(mktemp -d)"
trap 'rm -rf "$EMPTY_ROOT"' EXIT

WF='{"nodes":[],"connections":{}}'

# 1. The bridge module loads and is requireable (exports its public API).
exports_out=$(node -e "const b=require('$BRIDGE'); console.log(['run','main','validate','resolveInstallRoot','bridgeErrorResult','serializeIssue'].every(k=>typeof b[k]!=='undefined') ? 'OK' : 'MISSING')")
assert_eq "bridge module loads and exports public API" "OK" "$exports_out"

# 2. Bridge emits structured validator_bridge_error on stdin path when mcp absent.
out_stdin=$(N8N_MCP_INSTALL_ROOT="$EMPTY_ROOT" bash -c "echo '$WF' | node '$BRIDGE'") ; rc_stdin=$?
assert_eq "bridge exits 0 on missing n8n-mcp (graceful)" "0" "$rc_stdin"
assert_contains "bridge stdin reports structured error type" '"type":"validator_bridge_error"' "$out_stdin"
assert_contains "bridge stdin reports valid=false" '"valid":false' "$out_stdin"
assert_contains "bridge stdin reports error_count=1" '"error_count":1' "$out_stdin"
assert_contains "bridge stdin output is null-node shaped" '"node":null' "$out_stdin"

# 3. Output is parseable JSON matching the cloud bridge contract shape.
parsed=$(echo "$out_stdin" | python3 -c "import json,sys; d=json.load(sys.stdin); print(','.join(sorted(d.keys())))")
assert_eq "bridge output has cloud-contract keys" "error_count,errors,statistics,suggestions,valid,warning_count,warnings" "$parsed"

# 4. The eval wrapper (thin require of the shared bridge) emits the same contract.
out_eval=$(N8N_MCP_INSTALL_ROOT="$EMPTY_ROOT" bash -c "echo '$WF' | node '$EVAL_WRAPPER'")
assert_contains "eval wrapper reports structured error type" '"type":"validator_bridge_error"' "$out_eval"
assert_contains "eval wrapper reports valid=false" '"valid":false' "$out_eval"

# 5. The eval wrapper preserves the --file CLI contract.
WF_FILE="$EMPTY_ROOT/wf.json"
printf '%s' "$WF" > "$WF_FILE"
out_file=$(N8N_MCP_INSTALL_ROOT="$EMPTY_ROOT" node "$EVAL_WRAPPER" --file "$WF_FILE")
assert_contains "eval wrapper --file reports structured error type" '"type":"validator_bridge_error"' "$out_file"

# 6. The legacy hardcoded npx cache path must be gone from the eval entry point.
if grep -Fq "_npx" "$EVAL_WRAPPER"; then
  echo "  FAIL: eval wrapper still references a hardcoded npx cache path"; FAIL=$((FAIL+1))
else
  echo "  PASS: eval wrapper has no hardcoded npx cache path"; PASS=$((PASS+1))
fi

echo ""; echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
