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


def strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


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

    invalid_value_match = re.match(
        r"^Invalid value for '([^']+)'\. Must be one of:\s*(.*)$",
        msg,
    )
    if invalid_value_match:
        field_name = invalid_value_match.group(1)
        allowed_values = invalid_value_match.group(2).strip()
        allowed_suffix = f" Allowed values: {allowed_values}." if allowed_values else ""
        return (
            f"Replace only the invalid `{field_name}` value with one validator-accepted value."
            " If the field is a select/resource-locator object, keep that object shape and patch just its failing"
            f" selection value or mode.{allowed_suffix} (validator: {msg})"
        )

    required_match = re.match(r"^Required property '([^']+)' cannot be empty$", msg)
    if required_match:
        field_label = required_match.group(1)
        return (
            f"Fill the missing required field `{field_label}` with a non-empty schema-valid value, and leave"
            f" unrelated fields unchanged. (validator: {msg})"
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

        invalid_value_match = re.match(
            r"^Invalid value for '([^']+)'\. Must be one of:\s*(.*)$",
            message,
        )
        if invalid_value_match:
            field_name = invalid_value_match.group(1)
            allowed_values = [
                strip_matching_quotes(part.strip())
                for part in invalid_value_match.group(2).split(",")
                if part.strip()
            ]
            if node_name and workflow is not None:
                path, current_value = maybe_get_node_field(workflow, node_name, field_name)
            elif field_name:
                path = field_name

            action = f"Replace only the invalid `{field_name}` value with one validator-accepted option."
            if allowed_values:
                preview_values = ", ".join(allowed_values[:6])
                if len(allowed_values) > 6:
                    preview_values += ", ..."
                action += f" Allowed values: {preview_values}."

            if isinstance(current_value, dict) and current_value.get("__rl") is True:
                locator_value = current_value.get("value")
                locator_mode = current_value.get("mode")
                locator_path = f"{path}.value" if path else "the resource locator value"
                action = (
                    f"Keep the existing resource-locator object at `{path}`. Replace only `{locator_path}`"
                    " with a validator-accepted selection and keep a valid `mode` on that same object."
                )
                if allowed_values:
                    action += f" Allowed values: {preview_values}."
            fix_strategy = "replace_field"

        required_match = re.match(
            r"^Required property '([^']+)' cannot be empty$",
            message,
        )
        if required_match:
            field_label = required_match.group(1)
            action = (
                f"Fill the required field `{field_label}` with a non-empty schema-valid value. Do not rewrite unrelated fields."
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


def build_warnings_block(validation: dict[str, Any], max_warnings: int = 5) -> str:
    """Render validator warnings as a non-blocking bullet block.

    Warnings share the error shape ({type, message, node}) but are advisory:
    a workflow can be valid and still carry warnings. The block is deduped by
    (normalized message, node), capped at ``max_warnings`` with a
    ``(+N more warnings)`` overflow marker, and carries per-node attribution.
    Returns an empty string when there are no warnings so callers can omit the
    block entirely (no empty header).
    """
    warnings = validation.get("warnings") or []
    seen: set[tuple[str, str | None]] = set()
    bullets: list[str] = []
    total_unique = 0

    for warn in warnings:
        if not isinstance(warn, dict):
            continue
        message = normalize_message(warn.get("message", ""))
        if not message:
            continue
        node = warn.get("node")
        key = (message, node)
        if key in seen:
            continue
        seen.add(key)
        total_unique += 1
        if len(bullets) < max_warnings:
            if node:
                bullets.append(f"- [{node}] {message}")
            else:
                bullets.append(f"- {message}")

    if not bullets:
        return ""

    lines = ["Warnings (non-blocking — review before finalizing):"]
    lines.extend(bullets)
    overflow = total_unique - len(bullets)
    if overflow > 0:
        lines.append(f"(+{overflow} more warnings)")
    return "\n".join(lines)


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
