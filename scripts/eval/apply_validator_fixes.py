#!/usr/bin/env python3
"""Apply only conservative, generic deterministic fixes for validator failures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from workflow_validation import validate_with_mcp


def _resolve_parent(container: Any, path: str) -> tuple[Any, Any] | tuple[None, None]:
    parts: list[Any] = []
    for part in path.split("."):
        if "[" in part and part.endswith("]"):
            key, index = part[:-1].split("[", 1)
            parts.append(key)
            parts.append(int(index))
        else:
            parts.append(part)
    current = container
    for segment in parts[:-1]:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return None, None
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return None, None
            current = current[segment]
    return current, parts[-1]


def _set_path_value(container: Any, path: str, value: Any) -> bool:
    parent, leaf = _resolve_parent(container, path)
    if parent is None:
        return False
    if isinstance(leaf, int):
        if not isinstance(parent, list) or leaf >= len(parent):
            return False
        parent[leaf] = value
        return True
    if not isinstance(parent, dict):
        return False
    parent[leaf] = value
    return True


def _get_path_value(container: Any, path: str) -> Any:
    parent, leaf = _resolve_parent(container, path)
    if parent is None:
        raise KeyError(path)
    if isinstance(leaf, int):
        if not isinstance(parent, list) or leaf >= len(parent):
            raise KeyError(path)
        return parent[leaf]
    if not isinstance(parent, dict) or leaf not in parent:
        raise KeyError(path)
    return parent[leaf]


def _fix_expression_issue(workflow: dict[str, Any], issue: dict[str, Any]) -> int:
    path = issue.get("path")
    if not isinstance(path, str):
        return 0
    try:
        current = _get_path_value(workflow, path)
    except KeyError:
        return 0
    if isinstance(current, str) and "{{" in current and not current.startswith("="):
        if _set_path_value(workflow, path, "=" + current):
            return 1
    return 0


def apply_fixes(workflow: dict[str, Any], feedback: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    changes: list[str] = []

    expr_changes = 0
    for issue in feedback.get("issues", []):
        if "Expression format error" not in issue.get("message", ""):
            continue
        expr_changes += _fix_expression_issue(workflow, issue)
    if expr_changes:
        changes.append(f"prefixed {expr_changes} expression field(s) with '='")

    return workflow, changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-file", required=True)
    parser.add_argument("--feedback-file", required=True)
    parser.add_argument("--output-file", required=True)
    args = parser.parse_args()

    workflow = json.loads(Path(args.workflow_file).read_text())
    feedback = json.loads(Path(args.feedback_file).read_text())

    fixed_workflow, changes = apply_fixes(workflow, feedback)
    Path(args.output_file).write_text(json.dumps(fixed_workflow, indent=2))

    validation = validate_with_mcp(fixed_workflow) if changes else {"valid": False}
    print(
        json.dumps(
            {
                "changed": bool(changes),
                "changes": changes,
                "valid": bool(validation.get("valid")),
                "error_count": validation.get("error_count"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
