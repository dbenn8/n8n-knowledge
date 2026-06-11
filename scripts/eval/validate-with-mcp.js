#!/usr/bin/env node
/**
 * Post-hoc workflow validator entry point for the eval harness.
 *
 * Thin wrapper around the single shared bridge at hooks/lib/validator_bridge.js.
 * The hardcoded machine-specific npx cache path that used to live here is gone;
 * install-root resolution now mirrors the cloud bridge (env override
 * N8N_MCP_INSTALL_ROOT, else a 5-level probe for dist/ + data/nodes.db).
 *
 * CLI contract (unchanged, relied on by scripts/eval/workflow_validation.py):
 *   echo '{"nodes":[...],"connections":{}}' | node scripts/eval/validate-with-mcp.js
 *   node scripts/eval/validate-with-mcp.js < workflow.json
 *   node scripts/eval/validate-with-mcp.js --file path/to/response.json
 *
 * Uses n8n-mcp (MIT licensed) - https://github.com/czlonkowski/n8n-mcp
 */

const path = require("path");
const bridge = require(path.join(__dirname, "..", "..", "hooks", "lib", "validator_bridge.js"));

bridge.run();
