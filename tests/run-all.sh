#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOTAL_FAIL=0

echo "==============================="
echo "  n8n-knowledge plugin tests"
echo "==============================="
echo ""

for test_file in "$SCRIPT_DIR"/test-*.sh; do
  echo "--- Running $(basename "$test_file") ---"
  if bash "$test_file"; then
    echo ""
  else
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
    echo ""
  fi
done

echo "==============================="
if [ "$TOTAL_FAIL" -gt 0 ]; then
  echo "  SOME TESTS FAILED"
  exit 1
else
  echo "  ALL TESTS PASSED"
fi
echo "==============================="
