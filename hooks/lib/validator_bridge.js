#!/usr/bin/env node
/**
 * Validate workflow JSON against a locally installed n8n-mcp package.
 *
 * This is the single shared bridge used by BOTH the plugin local-mode validator
 * (hooks/validate-workflow.sh -> workflow_validator.py NodeValidatorBridge) and
 * the eval harness (scripts/eval/validate-with-mcp.js, which is a thin wrapper
 * around this module).
 *
 * Install-root resolution mirrors the cloud bridge
 * (n8n-hindsight ops-proxy/validator_bridge.js):
 *   1. honor N8N_MCP_INSTALL_ROOT if set,
 *   2. otherwise resolve the n8n-mcp entry point and probe up to 5 parent
 *      directories for one containing both dist/ and data/nodes.db.
 *
 * Error/issue serialization matches the cloud bridge exactly so plugin
 * local-mode output is byte-shape-identical to cloud output:
 *   - issues -> { type, message, node }
 *   - top-level failures -> errors[0].type === "validator_bridge_error".
 *
 * CLI contract (preserved for the eval harness):
 *   echo '{...}' | node validator_bridge.js     # read workflow from stdin
 *   node validator_bridge.js < workflow.json     # read workflow from stdin
 *   node validator_bridge.js --file path.json    # read workflow from a file
 * Result JSON is written to stdout.
 *
 * Uses n8n-mcp (MIT licensed) - https://github.com/czlonkowski/n8n-mcp
 */

const fs = require("fs");
const path = require("path");

function resolveInstallRoot() {
  if (process.env.N8N_MCP_INSTALL_ROOT) {
    return process.env.N8N_MCP_INSTALL_ROOT;
  }

  const entryPath = require.resolve("n8n-mcp");
  let current = path.dirname(entryPath);

  for (let index = 0; index < 5; index += 1) {
    if (
      fs.existsSync(path.join(current, "dist")) &&
      fs.existsSync(path.join(current, "data", "nodes.db"))
    ) {
      return current;
    }

    const parent = path.dirname(current);
    if (parent === current) {
      break;
    }
    current = parent;
  }

  throw new Error(`Unable to locate n8n-mcp install root from ${entryPath}`);
}

function serializeIssue(issue) {
  return {
    type: issue.type,
    message: issue.message,
    node: issue.nodeName || null,
  };
}

async function validate(workflow) {
  const installRoot = resolveInstallRoot();
  const distRoot = path.join(installRoot, "dist");
  const dbPath = path.join(installRoot, "data", "nodes.db");

  const { SQLiteStorageService } = require(path.join(
    distRoot,
    "services/sqlite-storage-service"
  ));
  const { NodeRepository } = require(path.join(distRoot, "database/node-repository"));
  const { WorkflowValidator } = require(path.join(
    distRoot,
    "services/workflow-validator"
  ));
  const { EnhancedConfigValidator } = require(path.join(
    distRoot,
    "services/enhanced-config-validator"
  ));

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
    errors: result.errors.map(serializeIssue),
    warnings: result.warnings.map(serializeIssue),
    statistics: result.statistics || {},
    suggestions: (result.suggestions || []).slice(0, 5),
  };
}

function bridgeErrorResult(error) {
  return {
    valid: false,
    error_count: 1,
    warning_count: 0,
    errors: [
      {
        type: "validator_bridge_error",
        message: error instanceof Error ? error.message : String(error),
        node: null,
      },
    ],
    warnings: [],
    statistics: {},
    suggestions: [],
  };
}

async function readInput() {
  const fileFlagIndex = process.argv.indexOf("--file");
  if (fileFlagIndex !== -1) {
    const filePath = process.argv[fileFlagIndex + 1];
    return fs.readFileSync(filePath, "utf8");
  }

  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function main() {
  try {
    const input = await readInput();
    const workflow = JSON.parse(input);
    const result = await validate(workflow);
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stdout.write(JSON.stringify(bridgeErrorResult(error)));
  }
}

function run() {
  return main().catch((error) => {
    process.stderr.write(
      `${error instanceof Error ? error.stack || error.message : String(error)}\n`
    );
    process.exit(1);
  });
}

module.exports = {
  resolveInstallRoot,
  serializeIssue,
  validate,
  bridgeErrorResult,
  main,
  run,
};

if (require.main === module) {
  run();
}
