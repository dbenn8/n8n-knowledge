#!/usr/bin/env bash
# Canonical-source parity guard for hooks/lib/node_lookup.py.
#
# node_lookup.py is THE single node-detection logic (see its module header). The
# plugin vendors it (it must — it ships to end users with no guaranteed `pip`),
# and n8n-hindsight keeps a byte-identical VENDORED COPY (scripts/lib/node_lookup.py)
# for the ingest side, so the node:X tags ingest WRITES match what the plugin's
# do_gotcha_recall QUERIES. This pins the canonical sha256; the IDENTICAL literal
# is pinned in n8n-hindsight:
#   scripts/tests/test_github_node_tagging.py::test_parity_node_lookup_hash
# If either copy drifts, that repo's suite fails — same pattern as
# test-hash-parity.sh. To change the detector: edit THIS file, re-vendor the
# identical file to n8n-hindsight, recompute (`shasum -a 256 hooks/lib/node_lookup.py`),
# and update BOTH pinned literals. A red test means a copy drifted, NOT that the
# test is wrong — never weaken it.
#
# The data file (node_lookup_data.json) is intentionally NOT pinned: it is
# regenerated from nodes.db (the single source for the data) and changes whenever
# the node catalog updates.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PASS=0; FAIL=0

echo "=== node_lookup.py canonical-source parity ==="

PINNED="7cc17940eb0bc0ac4f4653a7205656a8ac8a14f34586df254fcfaf5ef225c32b"
ACTUAL="$(shasum -a 256 "$REPO_DIR/hooks/lib/node_lookup.py" | cut -d' ' -f1)"
if [ "$ACTUAL" = "$PINNED" ]; then
  echo "  PASS: node_lookup.py matches the pinned canonical hash"; PASS=$((PASS + 1))
else
  echo "  FAIL: node_lookup.py drifted (pinned $PINNED, got $ACTUAL)"
  echo "        If this change is intentional: re-vendor to n8n-hindsight and"
  echo "        update the pinned literal in BOTH repos. Do not weaken this test."
  FAIL=$((FAIL + 1))
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
