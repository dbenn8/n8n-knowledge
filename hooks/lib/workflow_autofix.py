#!/usr/bin/env python3
"""Generic, node-agnostic auto-fixes for n8n workflow validation errors.

Design rules for adding a fix:
  - Unambiguous: only one correct transformation is possible
  - Generic: applies to any node type, any field — no node-specific knowledge
  - Safe: never changes the workflow's intended behavior, only its syntax

To add a new fix:
  1. Write fix_<name>(workflow, issues) -> (workflow, changes_list)
  2. Append it to ALL_FIXES
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _resolve_path(container: Any, path: str) -> tuple[Any, Any]:
    """Navigate a dotted path like 'nodes[2].parameters.url'.
    Returns (parent_container, leaf_key) or (None, None) if path is invalid.
    """
    parts: list[Any] = []
    for part in path.split("."):
        if "[" in part and part.endswith("]"):
            key, idx = part[:-1].split("[", 1)
            if key:
                parts.append(key)
            parts.append(int(idx))
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
    return (current, parts[-1]) if parts else (None, None)


def _get(container: Any, path: str) -> Any:
    parent, leaf = _resolve_path(container, path)
    if parent is None or leaf is None:
        raise KeyError(path)
    if isinstance(leaf, int):
        return parent[leaf]
    return parent[leaf]


def _set(container: Any, path: str, value: Any) -> bool:
    parent, leaf = _resolve_path(container, path)
    if parent is None or leaf is None:
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


# ── Fix functions ──────────────────────────────────────────────────────────────

def fix_expression_prefix(workflow: dict, issues: list) -> tuple[dict, list]:
    """Add = prefix to expression fields that contain {{ but don't start with =.

    n8n requires all expressions to start with = (e.g. ={{ $json.email }}).
    Models omit this because {{ }} is standard in Jinja/Handlebars templating
    where no prefix is needed. The fix is always unambiguous.
    """
    changes: list[str] = []
    for issue in issues:
        if "Expression format error" not in issue.get("message", ""):
            continue
        path = issue.get("path")
        if not isinstance(path, str):
            continue
        try:
            current = _get(workflow, path)
        except (KeyError, TypeError, IndexError):
            continue
        if isinstance(current, str) and "{{" in current and not current.startswith("="):
            if _set(workflow, path, "=" + current):
                node_name = issue.get("node_name") or "?"
                leaf = path.split(".")[-1].split("[")[0]
                preview = current[:60].replace("\n", " ")
                changes.append(
                    f"added '=' prefix in node '{node_name}' field '{leaf}': {preview!r}"
                )
    return workflow, changes


# ── Registry — add new generic fixes here ─────────────────────────────────────

ALL_FIXES: list = [
    fix_expression_prefix,
]


def apply_all_fixes(workflow: dict, issues: list) -> tuple[dict, list]:
    """Apply every registered fix in order. Returns (fixed_workflow, all_changes)."""
    all_changes: list[str] = []
    for fix_fn in ALL_FIXES:
        workflow, changes = fix_fn(workflow, issues)
        all_changes.extend(changes)
    return workflow, all_changes


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    """Usage: workflow_autofix.py <workflow-file> <validator-result-json>

    Reads the workflow from file, applies all registered fixes in-place,
    and prints JSON: {"changed": bool, "changes": [str]}.
    """
    if len(sys.argv) != 3:
        print(json.dumps({"changed": False, "changes": []}))
        return 0

    import pathlib

    workflow_path = pathlib.Path(sys.argv[1])
    result_raw = sys.argv[2]

    try:
        workflow = json.loads(workflow_path.read_text())
        result = json.loads(result_raw)
    except Exception:
        print(json.dumps({"changed": False, "changes": []}))
        return 0

    issues: list = result.get("issues") or []
    fixed_workflow, changes = apply_all_fixes(workflow, issues)

    if changes:
        workflow_path.write_text(json.dumps(fixed_workflow))

    print(json.dumps({"changed": bool(changes), "changes": changes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
