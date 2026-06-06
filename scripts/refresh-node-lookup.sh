#!/usr/bin/env bash
# Refresh node_lookup_data.json from the latest n8n-mcp npm package
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR=$(mktemp -d)
trap "rm -rf $WORKDIR" EXIT

echo "Fetching latest n8n-mcp package..."
cd "$WORKDIR" && npm pack n8n-mcp@latest 2>/dev/null && tar xzf n8n-mcp-*.tgz package/data/nodes.db

echo "Generating lookup dictionary..."
python3 "$SCRIPT_DIR/../hooks/lib/generate_lookup.py" \
  "$WORKDIR/package/data/nodes.db" \
  "$SCRIPT_DIR/../hooks/lib/node_lookup_data.json"

echo "Done. Updated hooks/lib/node_lookup_data.json"
