#!/usr/bin/env bash
# Refresh node_lookup_data.json from the latest n8n-mcp npm package,
# then run the test suite to validate the new dictionary.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR/.."
WORKDIR=$(mktemp -d)
trap "rm -rf $WORKDIR" EXIT

echo "Fetching latest n8n-mcp package..."
cd "$WORKDIR" && npm pack n8n-mcp@latest 2>/dev/null && tar xzf n8n-mcp-*.tgz package/data/nodes.db

echo "Generating lookup dictionary..."
python3 "$REPO_DIR/hooks/lib/generate_lookup.py" \
  "$WORKDIR/package/data/nodes.db" \
  "$REPO_DIR/hooks/lib/node_lookup_data.json"

echo ""
echo "Running validation tests..."
bash "$REPO_DIR/tests/test-node-lookup.sh"
bash "$REPO_DIR/tests/test-lookup-integrity.sh"

echo ""
echo "All tests passed. Dictionary updated successfully."
