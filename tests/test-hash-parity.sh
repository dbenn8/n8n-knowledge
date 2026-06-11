#!/usr/bin/env bash
# Cross-repo hash-parity guard for _nodes_content_sha256.
#
# hooks/lib/validator_metadata.py:_nodes_content_sha256 (n8n-knowledge) and
# n8n-hindsight ops-proxy/workflow_validator.py:_nodes_content_sha256 MUST stay
# byte-identical. This test pins the expected hash of a shared edge-case fixture
# (unicode + emoji, NULLs, integers, floats, blobs, mixed types). The identical
# literal is pinned in n8n-hindsight ops-proxy/tests/test_hash_parity.py. If
# either implementation drifts, that repo's suite fails here.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE="$SCRIPT_DIR/fixtures/nodes-parity-fixture.db"
PASS=0; FAIL=0
assert_eq(){ local d="$1" e="$2" a="$3"; if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS+1)); else echo "  FAIL: $d (want '$e' got '$a')"; FAIL=$((FAIL+1)); fi; }

echo "=== cross-repo nodes_content_sha256 parity tests ==="

# Pinned parity hash — identical literal pinned in the n8n-hindsight suite.
EXPECTED_PARITY_HASH="a9a698eb493b3f3b6dc1c1818ae14540c303f9ce769dca7c57a099dffcec5fb7"

if [ -f "$FIXTURE" ]; then
  echo "  PASS: parity fixture exists"; PASS=$((PASS+1))
else
  echo "  FAIL: parity fixture missing ($FIXTURE)"; FAIL=$((FAIL+1))
fi

ACTUAL_HASH=$(REPO_DIR="$REPO_DIR" FIXTURE="$FIXTURE" python3 - <<'PYEOF'
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["REPO_DIR"], "hooks", "lib"))
from validator_metadata import _nodes_content_sha256
print(_nodes_content_sha256(Path(os.environ["FIXTURE"])))
PYEOF
)

assert_eq "nodes_content_sha256 matches pinned cross-repo parity hash" \
  "$EXPECTED_PARITY_HASH" "$ACTUAL_HASH"

echo ""; echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
