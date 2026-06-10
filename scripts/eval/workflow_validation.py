#!/usr/bin/env python3
"""Reusable workflow extraction, validation, and repair feedback helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
HOOKS_LIB = os.path.join(REPO_DIR, "hooks", "lib")
if HOOKS_LIB not in sys.path:
    sys.path.insert(0, HOOKS_LIB)

from plugin_config import resolve_validator_target_config
from validator_metadata import (
    build_local_validator_info,
    build_validator_descriptor,
    compare_validator_descriptors,
    copy_descriptor,
    fetch_cloud_health,
)
from validator_enrichment import (
    build_issue_block,
    build_structured_issues,
    normalize_message,
    summarize_validation,
)

VALIDATOR_JS = os.path.join(SCRIPT_DIR, "validate-with-mcp.js")


def _build_plugin_validator_config_from_env() -> dict[str, Any]:
    return {
        "validator_mode": os.environ.get("EVAL_PLUGIN_VALIDATOR_MODE", "").strip() or "default",
        "validator_cloud_url": os.environ.get("EVAL_PLUGIN_VALIDATOR_CLOUD_URL", "").strip(),
        "validator_local_path": os.environ.get("EVAL_PLUGIN_VALIDATOR_LOCAL_PATH", "").strip(),
    }


def resolve_eval_plugin_validator_target() -> dict[str, Any]:
    config = _build_plugin_validator_config_from_env()
    return resolve_validator_target_config(config, mode_override=config.get("validator_mode"))


def resolve_scoring_validator_target() -> dict[str, Any]:
    plugin_target = resolve_eval_plugin_validator_target()
    raw_scoring_mode = os.environ.get("EVAL_SCORING_VALIDATOR_MODE", "").strip().lower()
    if not raw_scoring_mode:
        raw_scoring_mode = (
            "same-as-plugin"
            if os.environ.get("EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION", "0") == "1"
            else "local"
        )

    if raw_scoring_mode == "same-as-plugin":
        scoring_target = copy_descriptor(plugin_target)
        scoring_target["requested_mode"] = "same-as-plugin"
        scoring_target["reason"] = (
            "scoring validator uses the same effective target as plugin validation"
        )
        return scoring_target

    scoring_mode = raw_scoring_mode if raw_scoring_mode in {"default", "local", "cloud"} else "local"
    scoring_config = {
        "validator_mode": scoring_mode,
        "validator_cloud_url": (
            os.environ.get("EVAL_SCORING_VALIDATOR_CLOUD_URL", "").strip()
            or plugin_target.get("cloud_url")
            or ""
        ),
        "validator_local_path": (
            os.environ.get("EVAL_SCORING_VALIDATOR_LOCAL_PATH", "").strip()
            or os.environ.get("EVAL_PLUGIN_VALIDATOR_LOCAL_PATH", "").strip()
        ),
    }
    return resolve_validator_target_config(scoring_config, mode_override=scoring_mode)


def describe_validator_target(target: dict[str, Any]) -> dict[str, Any]:
    if target.get("effective_mode") == "cloud" and target.get("cloud_url"):
        try:
            health_payload = fetch_cloud_health(target["cloud_url"], timeout_seconds=10)
        except Exception as exc:
            descriptor = build_validator_descriptor(target)
            descriptor["status"] = "unavailable"
            descriptor["detail"] = str(exc)
            return descriptor
        return build_validator_descriptor(target, health_payload=health_payload)

    return build_validator_descriptor(target)


def extract_workflow_json(response_text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract n8n workflow JSON from a model response."""
    json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", response_text, re.DOTALL)

    for block in json_blocks:
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
                return obj, None
        except json.JSONDecodeError:
            continue

    brace_starts = [m.start() for m in re.finditer(r"\{", response_text)]
    for start in brace_starts:
        try:
            candidate = response_text[start:]
            depth = 0
            end = 0
            for i, ch in enumerate(candidate):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            snippet = candidate[:end]
            if len(snippet) > 200:
                obj = json.loads(snippet)
                if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
                    return obj, None
        except (json.JSONDecodeError, ValueError):
            continue

    return None, "no_json_found"


def _validate_local(workflow_json: dict[str, Any], local_root: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["N8N_MCP_INSTALL_ROOT"] = local_root
    try:
        proc = subprocess.run(
            ["node", VALIDATOR_JS],
            input=json.dumps(workflow_json),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if proc.returncode != 0:
            return {
                "valid": False,
                "error_count": 1,
                "warning_count": 0,
                "errors": [{"type": "validator_crash", "message": proc.stderr[:200]}],
                "warnings": [],
                "statistics": {},
            }
        result = json.loads(proc.stdout)
        if isinstance(result, dict):
            result.setdefault("validator_mode", "local")
            result.setdefault("validator_info", build_local_validator_info(local_root))
        return result
    except subprocess.TimeoutExpired:
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [{"type": "timeout", "message": "Validator timed out after 15s"}],
            "warnings": [],
            "statistics": {},
        }


def _validate_cloud(workflow_json: dict[str, Any], url: str) -> dict[str, Any]:
    body = json.dumps({"workflow": workflow_json}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if isinstance(result, dict) and not result.get("validator_info"):
                try:
                    health_payload = fetch_cloud_health(url, timeout_seconds=10)
                except Exception:
                    health_payload = None
                if isinstance(health_payload, dict):
                    result["validator_info"] = health_payload.get("validator_info")
            if isinstance(result, dict):
                result.setdefault("validator_mode", "cloud")
            return result
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
                }
            ],
            "warnings": [],
            "statistics": {},
        }
    except Exception as exc:
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [{"type": "validator_request_error", "message": str(exc)}],
            "warnings": [],
            "statistics": {},
        }


def validate_with_mcp(workflow_json: dict[str, Any]) -> dict[str, Any]:
    """Run the configured eval validator on a workflow JSON object."""
    try:
        target = resolve_scoring_validator_target()
        mode = target.get("effective_mode")
        if mode == "local" and target.get("local_root"):
            return _validate_local(workflow_json, target["local_root"])
        if mode == "cloud" and target.get("cloud_url"):
            return _validate_cloud(workflow_json, target["cloud_url"])

        reason = target.get("reason") or "validator target is not configured"
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [{"type": "validator_not_configured", "message": reason}],
            "warnings": [],
            "statistics": {},
            "validator_info": None,
            "validator_mode": None,
        }
    except Exception as exc:
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [{"type": "error", "message": str(exc)}],
            "warnings": [],
            "statistics": {},
            "validator_info": None,
            "validator_mode": None,
        }


def summarize_validation_basic(validation: dict[str, Any], max_errors: int = 8) -> list[str]:
    """Return a short deduped list of raw validator issues without plugin enrichment."""
    seen = set()
    summary: list[str] = []
    for err in validation.get("errors", []):
        msg = normalize_message(err.get("message", "unknown validator error"))
        if not msg or msg in seen:
            continue
        seen.add(msg)
        summary.append(msg)
        if len(summary) >= max_errors:
            break
    return summary


def inspect_response_text(
    response_text: str,
    max_errors: int = 8,
    enrichment_mode: str = "basic",
) -> dict[str, Any]:
    """Inspect a model response and return validation + repair feedback."""
    use_plugin_enrichment = enrichment_mode == "plugin"
    workflow, extract_err = extract_workflow_json(response_text)
    if workflow is None:
        repair_messages = [
            "Return a single complete importable n8n workflow JSON object inside a ```json code block.",
            "Include both a 'nodes' array and a 'connections' object.",
        ]
        return {
            "valid": False,
            "has_json": False,
            "extract_error": extract_err,
            "workflow": None,
            "issues": [
                {
                    "message": extract_err or "no_json_found",
                    "repair_message": repair_messages[0],
                    "node": None,
                    "path": None,
                    "current_value": None,
                    "current_value_preview": None,
                    "suggested_value": None,
                    "suggested_value_preview": None,
                    "suggested_action": "Return exactly one complete workflow object inside a ```json``` block.",
                    "fix_strategy": "return_json_block",
                }
            ],
            "validation": {
                "valid": False,
                "error_count": 1,
                "warning_count": 0,
                "errors": [{"type": "extract_error", "message": extract_err or "no_json_found"}],
                "warnings": [],
                "statistics": {},
            },
            "repair_messages": repair_messages,
            "feedback_block": "\n".join(f"- {msg}" for msg in repair_messages),
            "issues_block": (
                build_issue_block(
                    [
                        {
                            "path": None,
                            "current_value_preview": None,
                            "suggested_value_preview": None,
                            "suggested_action": "Return exactly one complete workflow object inside a ```json``` block.",
                            "repair_message": repair_messages[0],
                        }
                    ]
                )
                if use_plugin_enrichment
                else ""
            ),
        }

    validation = validate_with_mcp(workflow)
    issues = (
        build_structured_issues(validation, workflow, max_errors=max_errors)
        if use_plugin_enrichment
        else []
    )
    repair_messages = (
        summarize_validation(validation, max_errors=max_errors)
        if use_plugin_enrichment
        else summarize_validation_basic(validation, max_errors=max_errors)
    )
    feedback_block = "\n".join(f"- {msg}" for msg in repair_messages)
    return {
        "valid": bool(validation.get("valid")),
        "has_json": True,
        "extract_error": None,
        "workflow": workflow,
        "issues": issues,
        "validation": validation,
        "repair_messages": repair_messages,
        "feedback_block": feedback_block,
        "issues_block": build_issue_block(issues) if use_plugin_enrichment else "",
        "enrichment_mode": enrichment_mode,
    }


def inspect_response_file(
    response_file: str,
    max_errors: int = 8,
    enrichment_mode: str = "basic",
    workflow_file: str | None = None,
) -> dict[str, Any]:
    """Load an eval JSON response file and inspect it.

    If workflow_file is provided, that file IS the deliverable (e.g. the plugin
    condition, where the model writes the workflow to a file and is told NOT to
    paste JSON into its response). In that case the file is authoritative and the
    response text is not scraped for workflow JSON.
    """
    payload = json.load(open(response_file))
    response_text = payload.get("result", "")
    inspection = inspect_response_text(
        response_text,
        max_errors=max_errors,
        enrichment_mode=enrichment_mode,
    )
    if workflow_file:
        wf_path = Path(workflow_file)
        if wf_path.exists():
            try:
                workflow = json.loads(wf_path.read_text())
                if isinstance(workflow, dict) and ("nodes" in workflow or "connections" in workflow):
                    inspection = inspect_response_text(
                        "",
                        max_errors=max_errors,
                        enrichment_mode=enrichment_mode,
                    )
                    inspection["workflow"] = workflow
                    inspection["has_json"] = True
                    inspection["extract_error"] = None
                    validation = validate_with_mcp(workflow)
                    use_plugin_enrichment = enrichment_mode == "plugin"
                    issues = (
                        build_structured_issues(validation, workflow, max_errors=max_errors)
                        if use_plugin_enrichment
                        else []
                    )
                    repair_messages = (
                        summarize_validation(validation, max_errors=max_errors)
                        if use_plugin_enrichment
                        else summarize_validation_basic(validation, max_errors=max_errors)
                    )
                    inspection["valid"] = bool(validation.get("valid"))
                    inspection["issues"] = issues
                    inspection["validation"] = validation
                    inspection["repair_messages"] = repair_messages
                    inspection["feedback_block"] = "\n".join(f"- {msg}" for msg in repair_messages)
                    inspection["issues_block"] = build_issue_block(issues) if use_plugin_enrichment else ""
                    inspection["enrichment_mode"] = enrichment_mode
                    inspection["workflow_source"] = "scratch_file"
            except Exception:
                pass
    inspection["response_file"] = response_file
    inspection["response_text"] = response_text
    return inspection


def build_repair_prompt(
    original_prompt: str,
    response_text: str,
    inspection: dict[str, Any],
) -> str:
    """Build a repair prompt that is reusable across evals and live flows."""
    lines = [
        "Revise the n8n workflow JSON so it passes validator checks.",
        "Apply the smallest targeted edits needed to the current workflow draft.",
        "Do not rewrite unrelated nodes, fields, or connections unless a validator issue directly requires it.",
        "",
        "Original user request:",
        original_prompt.strip(),
        "",
        "Validator feedback to fix first:",
        inspection.get("feedback_block") or "- Return valid importable workflow JSON.",
        "",
        "Rules:",
        "- Preserve the user's requested behavior and the existing workflow structure where possible.",
        "- Fix validator-reported schema, operation, typeVersion, expression, and required-field issues first.",
        "- Treat the current workflow draft as canonical and patch only the specific failing fields when possible.",
        "- If a fix only touches one field, keep the rest of that node byte-for-byte equivalent where practical.",
        "- Return exactly one corrected importable n8n workflow JSON inside a ```json code block.",
        "- Do not include prose before or after the JSON block.",
    ]

    issues = inspection.get("issues") or []
    if issues:
        lines.extend(["", "Structured patch targets:"])
        for idx, issue in enumerate(issues, start=1):
            path = issue.get("path") or "<unknown path>"
            action = issue.get("suggested_action") or issue.get("repair_message")
            lines.append(f"{idx}. Path: {path}")
            if issue.get("current_value_preview") is not None:
                lines.append(f"   Current: {issue['current_value_preview']}")
            if issue.get("suggested_value_preview") is not None:
                lines.append(f"   Suggested replacement: {issue['suggested_value_preview']}")
            lines.append(f"   Action: {action}")

    workflow = inspection.get("workflow")
    if workflow is not None:
        lines.extend(
            [
                "",
                "Current workflow draft JSON:",
                "```json",
                json.dumps(workflow, indent=2),
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Previous response (no extractable workflow JSON was found):",
                response_text.strip()[:8000],
            ]
        )

    return "\n".join(lines) + "\n"
