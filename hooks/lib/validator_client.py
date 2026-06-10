#!/usr/bin/env python3
"""Plugin-side workflow validator client.

Routes to either a local n8n-mcp install or the cloud validator endpoint,
based on plugin config. This is for plugin-side validation only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

from plugin_config import resolve_validator_target
from validator_enrichment import (
    build_issue_block,
    build_structured_issues,
    summarize_validation,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE_JS = os.path.join(SCRIPT_DIR, "validator_bridge.js")


def shape_result(
    validation: dict[str, Any],
    workflow: dict[str, Any],
    *,
    mode: str,
    max_errors: int = 8,
) -> dict[str, Any]:
    repair_messages = summarize_validation(validation, max_errors=max_errors)
    issues = build_structured_issues(validation, workflow, max_errors=max_errors)
    return {
        "valid": bool(validation.get("valid")),
        "has_json": True,
        "extract_error": None,
        "error_count": validation.get("error_count", len(validation.get("errors", []))),
        "warning_count": validation.get("warning_count", len(validation.get("warnings", []))),
        "node_count": validation.get("statistics", {}).get("totalNodes", 0),
        "trigger_count": validation.get("statistics", {}).get("triggerNodes", 0),
        "issues": issues,
        "issues_block": build_issue_block(issues),
        "repair_messages": repair_messages,
        "feedback_block": "\n".join(f"- {msg}" for msg in repair_messages),
        "errors": validation.get("errors", []),
        "warnings": validation.get("warnings", []),
        "statistics": validation.get("statistics", {}),
        "suggestions": validation.get("suggestions", []),
        "validator_mode": mode,
    }


def validate_local(workflow: dict[str, Any], local_root: str) -> dict[str, Any]:
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
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [
                {
                    "type": "validator_bridge_error",
                    "message": proc.stderr[:400] or "local validator bridge failed",
                    "node": None,
                }
            ],
            "warnings": [],
            "statistics": {},
            "suggestions": [],
        }
    return json.loads(proc.stdout)


def validate_cloud(workflow: dict[str, Any], url: str) -> dict[str, Any]:
    body = json.dumps({"workflow": workflow}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [
                {
                    "type": "validator_http_error",
                    "message": f"cloud validator returned HTTP {exc.code}: {text[:300]}",
                    "node": None,
                }
            ],
            "warnings": [],
            "statistics": {},
            "suggestions": [],
        }
    except Exception as exc:
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [
                {
                    "type": "validator_request_error",
                    "message": str(exc),
                    "node": None,
                }
            ],
            "warnings": [],
            "statistics": {},
            "suggestions": [],
        }


def validate_workflow(workflow: dict[str, Any], project_dir: str) -> dict[str, Any]:
    mock_response = os.environ.get("N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE")
    if mock_response:
        validation = json.loads(mock_response)
        return shape_result(validation, workflow, mode="mock")

    resolved = resolve_validator_target(project_dir)
    mode = resolved.get("effective_mode")
    if mode == "local":
        raw = validate_local(workflow, resolved["local_root"])
        return shape_result(raw, workflow, mode="local")
    if mode == "cloud":
        raw = validate_cloud(workflow, resolved["cloud_url"])
        return shape_result(raw, workflow, mode="cloud")

    return {
        "valid": False,
        "has_json": True,
        "extract_error": "validator_not_configured",
        "error_count": 1,
        "warning_count": 0,
        "node_count": 0,
        "trigger_count": 0,
        "issues": [],
        "issues_block": "",
        "repair_messages": [resolved["reason"]],
        "feedback_block": f"- {resolved['reason']}",
        "errors": [{"type": "validator_not_configured", "message": resolved["reason"], "node": None}],
        "warnings": [],
        "statistics": {},
        "suggestions": [],
        "validator_mode": None,
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"Usage: {sys.argv[0]} <workflow-json-file> <project-dir>")
    workflow = json.load(open(sys.argv[1]))
    project_dir = sys.argv[2]
    print(json.dumps(validate_workflow(workflow, project_dir)))


if __name__ == "__main__":
    main()
