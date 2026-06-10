#!/usr/bin/env python3
"""Shared client-side enrichment for raw validator responses.

This module intentionally stays on the client/plugin side so local and cloud
validator modes can share the same enrichment logic without changing raw
validator semantics.
"""

from __future__ import annotations

import json
import re
from typing import Any


def normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message or "").strip()


def json_preview(value: Any, max_chars: int = 240) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True)
    except Exception:
        text = repr(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def workflow_path_to_segments(path: str) -> list[Any]:
    segments: list[Any] = []
    for part in path.split("."):
        match = re.fullmatch(r"([A-Za-z0-9_\-]+)(?:\[(\d+)\])?", part)
        if not match:
            segments.append(part)
            continue
        key, index = match.groups()
        segments.append(key)
        if index is not None:
            segments.append(int(index))
    return segments


def resolve_path(value: Any, path: str) -> Any:
    current = value
    for segment in workflow_path_to_segments(path):
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                raise KeyError(path)
            current = current[segment]
            continue
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(path)
        current = current[segment]
    return current


def find_node(workflow: dict[str, Any], node_name: str) -> dict[str, Any] | None:
    for node in workflow.get("nodes", []):
        if isinstance(node, dict) and node.get("name") == node_name:
            return node
    return None


def maybe_get_node_field(
    workflow: dict[str, Any], node_name: str, field_name: str
) -> tuple[str | None, Any]:
    node = find_node(workflow, node_name)
    if not node:
        return None, None
    params = node.get("parameters", {})
    if isinstance(params, dict) and field_name in params:
        return f"nodes[name={node_name}].parameters.{field_name}", params.get(field_name)
    return f"nodes[name={node_name}].parameters.{field_name}", None


def rewrite_message(message: str) -> str:
    msg = normalize_message(message)

    if msg == "Expected object but got string":
        return (
            "A field that should be a JSON object is currently a quoted string. "
            "Return real JSON objects for structured fields instead of wrapping them in quotes. "
            f"(validator: {msg})"
        )

    if msg == "Expected object but got array":
        return (
            "Return one workflow JSON object, not a top-level array. "
            f"(validator: {msg})"
        )

    if msg in {
        "Filter must have a combinator field",
        "Filter must have a conditions field",
    }:
        return (
            "IF/filter nodes need the newer `conditions` structure with a lowercase `combinator` and a "
            "`conditions` array. "
            f"(validator: {msg})"
        )

    if msg.startswith("Invalid combinator value:"):
        return (
            "Use lowercase `and` or `or` for IF/filter combinators. "
            f"(validator: {msg})"
        )

    if "operator is missing or not an object" in msg:
        return (
            "Each IF/filter rule needs an `operator` object, not a bare string. "
            f"(validator: {msg})"
        )

    if msg.startswith("Operation '") and "not valid for type" in msg:
        return (
            "Replace the invalid operator with one that the validator supports for that field type, and patch "
            "only the failing operator field. "
            f"(validator: {msg})"
        )

    if msg == "Webhook path is required":
        return (
            "Webhook nodes need a non-empty `parameters.path` string. "
            f"(validator: {msg})"
        )

    if "Cannot read properties of undefined" in msg:
        return (
            "This looks like a follow-on validator/runtime crash caused by an earlier schema problem. Fix the "
            "earlier validator errors first, then revalidate. "
            f"(validator: {msg})"
        )

    if "responseNode mode requires onError" in msg or "Node-level properties onError are in the wrong location" in msg:
        return (
            "Move `onError` to the node level instead of nesting it under `parameters`. "
            f"(validator: {msg})"
        )

    if msg.startswith("Expression format error"):
        return (
            "When a field mixes literal text with `{{ ... }}` expressions, prefix the entire field with `=` so "
            "n8n treats it as one expression value. "
            f"(validator: {msg})"
        )

    if "resourceLocator" in msg and "mode" in msg:
        return (
            "Add the missing `mode` field on the failing resource locator object instead of rewriting the whole node. "
            f"(validator: {msg})"
        )

    if msg.startswith("Duplicate node ID:"):
        return (
            "Every node must have a unique non-empty `id`. "
            f"(validator: {msg})"
        )

    return msg


def build_structured_issues(
    validation: dict[str, Any],
    workflow: dict[str, Any] | None,
    max_errors: int = 8,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for err in validation.get("errors", []):
        raw_message = err.get("message", "unknown validator error")
        message = normalize_message(raw_message)
        repair_message = rewrite_message(raw_message)
        node_name = err.get("node")
        path: str | None = None
        current_value: Any = None
        suggested_value: Any = None
        fix_strategy = "minimal_patch"
        action = repair_message

        match = re.match(r"^([A-Za-z0-9_.\[\]\-]+):\s+(.*)$", message)
        if match:
            candidate_path = match.group(1)
            path = candidate_path
            if workflow is not None:
                try:
                    current_value = resolve_path(workflow, candidate_path)
                except KeyError:
                    current_value = None

        expr_match = re.search(
            r"Expression format error in node '([^']+)': Field '([^']+)'", message
        )
        if expr_match and workflow is not None:
            node_name = expr_match.group(1)
            field_name = expr_match.group(2)
            path, current_value = maybe_get_node_field(workflow, node_name, field_name)
            if (
                isinstance(current_value, str)
                and "{{" in current_value
                and not current_value.startswith("=")
            ):
                suggested_value = "=" + current_value
                action = (
                    f"Prefix `{path}` with `=` and leave the rest of the field unchanged."
                )

        if message == "Expected object but got string":
            action = (
                "Replace the quoted structured field with a real JSON object. Keep unrelated nodes and fields unchanged."
            )
            fix_strategy = "replace_field"

        if message == "Expected object but got array":
            action = (
                "Return one top-level workflow object instead of an array."
            )
            fix_strategy = "rewrite_outer_shape"

        if message.startswith("Invalid combinator value:"):
            action = "Replace the combinator value with lowercase `and` or `or` only."
            fix_strategy = "replace_field"

        if "operator is missing or not an object" in message:
            action = "Replace the failing operator field with a proper JSON object."
            fix_strategy = "replace_field"

        if message.startswith("Operation '") and "not valid for type" in message:
            action = (
                "Patch only the invalid operator value at the failing IF/filter rule. Do not rewrite the full workflow."
            )
            fix_strategy = "replace_field"

        missing_op = re.match(
            r'^([A-Za-z0-9_.\[\]\-]+): missing required field "operation"$', message
        )
        if missing_op:
            path = missing_op.group(1)
            action = f"Add the missing `operation` field at `{path}`."
            fix_strategy = "insert_field"

        if "resourceLocator" in message and "mode" in message:
            locator_match = re.search(r"resourceLocator '([^']+)'", message)
            field_name = locator_match.group(1) if locator_match else None
            if node_name and field_name and workflow is not None:
                path, current_value = maybe_get_node_field(workflow, node_name, field_name)
            elif field_name:
                path = field_name
            action = (
                "Add the missing `mode` field on that resource locator object. Keep the rest of the node unchanged."
            )
            fix_strategy = "insert_field"

        if message.startswith("Duplicate node ID:"):
            action = "Assign a unique `id` only to the conflicting node ids."
            fix_strategy = "replace_field"

        key = (repair_message, path, node_name)
        if key in seen:
            continue
        seen.add(key)

        issues.append(
            {
                "message": message,
                "repair_message": repair_message,
                "node": node_name,
                "path": path,
                "current_value": current_value,
                "current_value_preview": (
                    json_preview(current_value) if current_value is not None else None
                ),
                "suggested_value": suggested_value,
                "suggested_value_preview": (
                    json_preview(suggested_value) if suggested_value is not None else None
                ),
                "suggested_action": action,
                "fix_strategy": fix_strategy,
            }
        )
        if len(issues) >= max_errors:
            break

    return issues


def summarize_validation(validation: dict[str, Any], max_errors: int = 8) -> list[str]:
    seen = set()
    summary: list[str] = []
    for err in validation.get("errors", []):
        msg = rewrite_message(err.get("message", "unknown validator error"))
        if not msg or msg in seen:
            continue
        seen.add(msg)
        summary.append(msg)
        if len(summary) >= max_errors:
            break
    return summary


def build_issue_block(issues: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, issue in enumerate(issues, start=1):
        lines.append(f"{idx}. Path: {issue.get('path') or '<unknown path>'}")
        if issue.get("current_value_preview") is not None:
            lines.append(f"   Current: {issue['current_value_preview']}")
        if issue.get("suggested_value_preview") is not None:
            lines.append(f"   Suggested replacement: {issue['suggested_value_preview']}")
        lines.append(
            f"   Action: {issue.get('suggested_action') or issue.get('repair_message')}"
        )
    return "\n".join(lines)
