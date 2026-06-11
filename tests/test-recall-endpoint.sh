#!/usr/bin/env bash
set -euo pipefail

# Tests for unified recall endpoint resolution across recall.sh and structured_recall.sh.
# Both must resolve to the SAME default (the applikuapp public recall URL) and both must
# honor a RECALL_URL env override. Before the refactor, recall.sh hardcoded the URL
# (NOT overridable) while structured_recall.sh used ${RECALL_URL:-...}; the shared helper
# makes resolution consistent.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected '$expected', got '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

DEFAULT_URL="https://n8nhindsight.applikuapp.com/public/recall"

echo "=== recall endpoint resolution tests ==="

# Resolve the URL that each script would use by sourcing it and reading RECALL_URL.
# (Sourcing does NOT make a network call — it only defines functions + the URL var.)
resolve_recall() {
  env -u RECALL_URL bash -c "source '$LIB_DIR/recall.sh' >/dev/null 2>&1; printf '%s' \"\$RECALL_URL\""
}
resolve_structured() {
  env -u RECALL_URL bash -c "source '$LIB_DIR/structured_recall.sh' >/dev/null 2>&1; printf '%s' \"\$RECALL_URL\""
}
resolve_recall_override() {
  RECALL_URL="http://localhost:9/override" bash -c "source '$LIB_DIR/recall.sh' >/dev/null 2>&1; printf '%s' \"\$RECALL_URL\""
}
resolve_structured_override() {
  RECALL_URL="http://localhost:9/override" bash -c "source '$LIB_DIR/structured_recall.sh' >/dev/null 2>&1; printf '%s' \"\$RECALL_URL\""
}

echo ""
echo "--- default endpoint ---"
assert_eq "recall.sh default endpoint" "$DEFAULT_URL" "$(resolve_recall)"
assert_eq "structured_recall.sh default endpoint" "$DEFAULT_URL" "$(resolve_structured)"
assert_eq "both default to the same endpoint" "$(resolve_recall)" "$(resolve_structured)"

echo ""
echo "--- RECALL_URL override applies to BOTH ---"
assert_eq "recall.sh honors RECALL_URL override" "http://localhost:9/override" "$(resolve_recall_override)"
assert_eq "structured_recall.sh honors RECALL_URL override" "http://localhost:9/override" "$(resolve_structured_override)"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
