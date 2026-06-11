#!/usr/bin/env bash
# Shim so the bash test runner (run-all.sh globs test-*.sh) executes the
# hooks/lib Python unit suite under tests/python/.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== hooks/lib python unit tests ==="
python3 -m pytest "$SCRIPT_DIR/python/" -q
