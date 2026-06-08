#!/usr/bin/env node
/**
 * Post-hoc workflow validator using n8n-mcp's full validation engine.
 *
 * Reads workflow JSON from stdin, validates using the same engine
 * that powers n8n-mcp's validate_workflow tool, outputs results as JSON.
 *
 * Usage:
 *   echo '{"nodes":[...],"connections":{}}' | node scripts/eval/validate-with-mcp.js
 *   node scripts/eval/validate-with-mcp.js < workflow.json
 *   node scripts/eval/validate-with-mcp.js --file path/to/response.json
 *
 * Uses n8n-mcp (MIT licensed) - https://github.com/czlonkowski/n8n-mcp
 */

const path = require('path');

// Resolve n8n-mcp from npx cache
const MCP_BASE = path.join(
  process.env.HOME, '.npm/_npx/b6a381d62ce0fe56/node_modules/n8n-mcp/dist'
);

const { SQLiteStorageService } = require(path.join(MCP_BASE, 'services/sqlite-storage-service'));
const { NodeRepository } = require(path.join(MCP_BASE, 'database/node-repository'));
const { WorkflowValidator } = require(path.join(MCP_BASE, 'services/workflow-validator'));
const { EnhancedConfigValidator } = require(path.join(MCP_BASE, 'services/enhanced-config-validator'));

const DB_PATH = path.join(MCP_BASE, '..', 'data', 'nodes.db');

async function validate(workflow) {
  const storage = new SQLiteStorageService(DB_PATH);
  const repo = new NodeRepository(storage);

  EnhancedConfigValidator.initializeSimilarityServices(repo);

  const validator = new WorkflowValidator(repo, EnhancedConfigValidator);

  const result = await validator.validateWorkflow(workflow, {
    validateNodes: true,
    validateConnections: true,
    validateExpressions: true,
    profile: 'runtime',
  });

  return {
    valid: result.valid,
    error_count: result.errors.length,
    warning_count: result.warnings.length,
    errors: result.errors.map(e => ({
      type: e.type,
      message: e.message,
      node: e.nodeName || null,
    })),
    warnings: result.warnings.map(w => ({
      type: w.type,
      message: w.message,
      node: w.nodeName || null,
    })),
    statistics: result.statistics,
    suggestions: (result.suggestions || []).slice(0, 5),
  };
}

async function main() {
  let input = '';

  if (process.argv.includes('--file')) {
    const idx = process.argv.indexOf('--file');
    const filePath = process.argv[idx + 1];
    const fs = require('fs');
    input = fs.readFileSync(filePath, 'utf-8');
  } else {
    // Read from stdin
    input = await new Promise((resolve) => {
      let data = '';
      process.stdin.setEncoding('utf-8');
      process.stdin.on('data', chunk => data += chunk);
      process.stdin.on('end', () => resolve(data));
    });
  }

  try {
    const workflow = JSON.parse(input);
    const result = await validate(workflow);
    console.log(JSON.stringify(result));
  } catch (err) {
    console.log(JSON.stringify({
      valid: false,
      error_count: 1,
      warning_count: 0,
      errors: [{ type: 'parse_error', message: err.message, node: null }],
      warnings: [],
      statistics: {},
      suggestions: [],
    }));
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
