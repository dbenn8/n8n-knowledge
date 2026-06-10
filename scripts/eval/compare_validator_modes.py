#!/usr/bin/env python3
"""Compare local and cloud validator responses for the same workflow.

Usage:
  python3 scripts/eval/compare_validator_modes.py --workflow-file path/to/workflow.json
  python3 scripts/eval/compare_validator_modes.py --fail-on-diff
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
HOOKS_LIB = os.path.join(REPO_DIR, "hooks", "lib")
if HOOKS_LIB not in sys.path:
    sys.path.insert(0, HOOKS_LIB)

from plugin_config import find_n8n_mcp_install_root, load_config

BRIDGE_JS = os.path.join(REPO_DIR, "hooks", "lib", "validator_bridge.js")


def build_sample_workflow() -> dict[str, Any]:
    return {
        "name": "Validator Parity Sample",
        "nodes": [
            {
                "parameters": {
                    "httpMethod": "POST",
                    "path": "lead-capture",
                    "responseMode": "onReceived",
                    "responseData": {"success": True},
                    "options": {},
                },
                "id": "webhook-1",
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 300],
            },
            {
                "parameters": {
                    "conditions": {
                        "conditions": [
                            {
                                "id": "cond-1",
                                "leftValue": "={{ $json.metrics?.employees || 0 }}",
                                "rightValue": 50,
                                "operator": {
                                    "type": "number",
                                    "operation": "larger",
                                },
                            }
                        ],
                        "combinator": "and",
                    }
                },
                "id": "if-1",
                "name": "Enterprise Check",
                "type": "n8n-nodes-base.if",
                "typeVersion": 2,
                "position": [280, 300],
            },
            {
                "parameters": {
                    "operation": "postMessage",
                    "channel": "#enterprise-leads",
                    "text": "={{ 'Lead: ' + ($json.metrics?.employees || 'unknown') }}",
                },
                "id": "slack-1",
                "name": "Slack - Enterprise Channel",
                "type": "n8n-nodes-base.slack",
                "typeVersion": 2,
                "position": [560, 300],
            },
            {
                "parameters": {
                    "operation": "append",
                    "documentId": {
                        "__rl": True,
                        "value": "YOUR_GOOGLE_SHEET_ID",
                        "mode": "id",
                    },
                    "sheetName": {
                        "__rl": True,
                        "value": "Sheet1",
                    },
                    "range": "A:I",
                    "columns": {
                        "mappingMode": "defineBelow",
                        "value": {"A": "={{ $json.metrics?.employees || 'N/A' }}"},
                    },
                    "options": {},
                },
                "id": "sheets-1",
                "name": "Google Sheets - Log Lead",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4,
                "position": [840, 300],
            },
        ],
        "connections": {
            "Webhook": {"main": [[{"node": "Enterprise Check", "type": "main", "index": 0}]]},
            "Enterprise Check": {
                "main": [
                    [
                        {"node": "Slack - Enterprise Channel", "type": "main", "index": 0},
                    ],
                    [
                        {"node": "Google Sheets - Log Lead", "type": "main", "index": 0},
                    ],
                ]
            },
        },
    }


def load_workflow(path: str | None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text())
    return build_sample_workflow()


def validate_local_raw(workflow: dict[str, Any], project_dir: str) -> dict[str, Any]:
    config = load_config(project_dir)
    local_root = find_n8n_mcp_install_root(config)
    if not local_root:
        raise RuntimeError("No local n8n-mcp install found.")
    env = dict(os.environ)
    env["N8N_MCP_INSTALL_ROOT"] = local_root
    proc = subprocess.run(
        ["node", BRIDGE_JS],
        input=json.dumps(workflow),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[:1000] or "local validator bridge failed")
    return json.loads(proc.stdout)


def validate_cloud_raw(workflow: dict[str, Any], url: str) -> dict[str, Any]:
    body = json.dumps({"workflow": workflow}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "valid": result.get("valid"),
        "error_count": result.get("error_count"),
        "warning_count": result.get("warning_count"),
        "errors": [
            {
                "type": err.get("type"),
                "message": err.get("message"),
                "node": err.get("node"),
            }
            for err in result.get("errors", [])
        ],
        "warnings": [
            {
                "type": warn.get("type"),
                "message": warn.get("message"),
                "node": warn.get("node"),
            }
            for warn in result.get("warnings", [])
        ],
        "statistics": result.get("statistics", {}),
        "suggestions": result.get("suggestions", []),
    }


def compare(local_result: dict[str, Any], cloud_result: dict[str, Any]) -> list[str]:
    diffs: list[str] = []
    local_summary = summarize(local_result)
    cloud_summary = summarize(cloud_result)

    for key in ["valid", "error_count", "warning_count"]:
        if local_summary.get(key) != cloud_summary.get(key):
            diffs.append(
                f"{key} differs: local={local_summary.get(key)!r} cloud={cloud_summary.get(key)!r}"
            )

    if local_summary["errors"] != cloud_summary["errors"]:
        diffs.append("errors differ")
    if local_summary["warnings"] != cloud_summary["warnings"]:
        diffs.append("warnings differ")
    if local_summary["statistics"] != cloud_summary["statistics"]:
        diffs.append("statistics differ")
    if local_summary["suggestions"] != cloud_summary["suggestions"]:
        diffs.append("suggestions differ")
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-file")
    parser.add_argument("--project-dir", default=REPO_DIR)
    parser.add_argument(
        "--cloud-url",
        default="https://n8nhindsight.applikuapp.com/public/validate-workflow",
    )
    parser.add_argument("--fail-on-diff", action="store_true")
    args = parser.parse_args()

    workflow = load_workflow(args.workflow_file)
    local_result = validate_local_raw(workflow, args.project_dir)
    cloud_result = validate_cloud_raw(workflow, args.cloud_url)
    diffs = compare(local_result, cloud_result)

    payload = {
        "workflow_source": args.workflow_file or "built_in_sample",
        "cloud_url": args.cloud_url,
        "diffs": diffs,
        "local": summarize(local_result),
        "cloud": summarize(cloud_result),
    }
    print(json.dumps(payload, indent=2))

    if diffs and args.fail_on_diff:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
