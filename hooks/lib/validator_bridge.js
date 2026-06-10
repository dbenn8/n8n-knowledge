#!/usr/bin/env node
/**
 * Validate workflow JSON against a locally installed n8n-mcp package.
 *
 * Requires N8N_MCP_INSTALL_ROOT to point at the package root:
 *   .../node_modules/n8n-mcp
 */

const fs = require("fs");
const path = require("path");

const installRoot = process.env.N8N_MCP_INSTALL_ROOT;
if (!installRoot) {
  console.error("N8N_MCP_INSTALL_ROOT is required");
  process.exit(2);
}

const distRoot = path.join(installRoot, "dist");
const dbPath = path.join(installRoot, "data", "nodes.db");

const { SQLiteStorageService } = require(path.join(distRoot, "services/sqlite-storage-service"));
const { NodeRepository } = require(path.join(distRoot, "database/node-repository"));
const { WorkflowValidator } = require(path.join(distRoot, "services/workflow-validator"));
const { EnhancedConfigValidator } = require(path.join(distRoot, "services/enhanced-config-validator"));

async function validate(workflow) {
  const storage = new SQLiteStorageService(dbPath);
  const repo = new NodeRepository(storage);
  EnhancedConfigValidator.initializeSimilarityServices(repo);
  const validator = new WorkflowValidator(repo, EnhancedConfigValidator);

  const result = await validator.validateWorkflow(workflow, {
    validateNodes: true,
    validateConnections: true,
    validateExpressions: true,
    profile: "runtime",
  });

  return {
    valid: result.valid,
    error_count: result.errors.length,
    warning_count: result.warnings.length,
    errors: result.errors.map((e) => ({
      type: e.type,
      message: e.message,
      node: e.nodeName || null,
    })),
    warnings: result.warnings.map((w) => ({
      type: w.type,
      message: w.message,
      node: w.nodeName || null,
    })),
    statistics: result.statistics,
    suggestions: (result.suggestions || []).slice(0, 5),
  };
}

async function main() {
  const input = fs.readFileSync(0, "utf8");
  try {
    const workflow = JSON.parse(input);
    const result = await validate(workflow);
    process.stdout.write(JSON.stringify(result));
  } catch (err) {
    process.stdout.write(
      JSON.stringify({
        valid: false,
        error_count: 1,
        warning_count: 0,
        errors: [{ type: "parse_error", message: err.message, node: null }],
        warnings: [],
        statistics: {},
        suggestions: [],
      })
    );
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
