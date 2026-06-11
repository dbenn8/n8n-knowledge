#!/usr/bin/env python3
"""
Inject compact n8n node schema from nodes.db into hook context.

Produces a cheatsheet with valid resource/operation combinations for each
detected node type. Fixes the #1 validation error: "Invalid value for
'operation'" — Claude uses wrong enum values without this context.

Node types are in nodes-base.slack format (matches nodes.db directly).

Usage (standalone):
  python3 nodes_db_inject.py nodes-base.slack nodes-base.googleSheets

Env vars:
  N8N_KNOWLEDGE_NODES_DB  path to nodes.db (optional, auto-discovered via glob)
"""
import glob
import json
import os
import re
import sqlite3
import sys

from plugin_config import find_n8n_mcp_install_root

# Character budget — keep total injection under 8K to stay within
# Claude Code's 10K additionalContext limit alongside recall results.
_MAX_CHARS = 6000
_MAX_NODES = 5
# Characters reserved at the tail of _MAX_CHARS so the omission marker always
# fits inside the budget even when the body is truncated. The marker line is
# short and bounded, so 200 chars is ample headroom.
_MARKER_RESERVE = 200
_MARKER_BUDGET = _MAX_CHARS - _MARKER_RESERVE


def _find_db():
    env_path = os.environ.get("N8N_KNOWLEDGE_NODES_DB")
    if env_path and os.path.exists(env_path):
        return env_path
    install_root = find_n8n_mcp_install_root()
    if install_root:
        db_path = os.path.join(install_root, "data", "nodes.db")
        if os.path.exists(db_path):
            return db_path
    candidates = glob.glob(
        os.path.expanduser("~/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db")
    )
    if candidates:
        return max(candidates, key=os.path.getmtime)
    return None


def _extract_resource_locator_fields(props_json):
    """Return {field_name: [mode_names]} for resourceLocator-type properties."""
    try:
        props = json.loads(props_json)
    except Exception:
        return {}
    fields = {}
    for prop in props:
        if prop.get("type") != "resourceLocator":
            continue
        name = prop.get("name", "")
        if not name or name in fields:
            continue
        modes = []
        for m in prop.get("modes", []):
            mode_name = m.get("name", "")
            if mode_name:
                modes.append(mode_name)
        if modes:
            fields[name] = modes
    return fields


def order_error_node_types(workflow, result):
    """Return error node types ordered by descending error count.

    Counts validator issues per node, maps each erroring node name to its node
    type, and also catches node types named directly inside error messages.
    Node types are ordered by descending total error count; ties are broken by
    first appearance in the workflow's ``nodes`` array, making the order fully
    deterministic (the prior ``set()`` iteration was not).

    When no error node can be identified the result is an EMPTY list — callers
    must then SKIP spec injection rather than injecting every workflow node
    (the old fallback injected ALL nodes, P2#9).

    Node types are returned in their original ``n8n-...`` form; the caller is
    responsible for converting to the ``nodes-base.X`` db form.
    """
    nodes = workflow.get("nodes", []) or []

    # First-appearance index per node type (tie-breaker).
    first_index = {}
    name_to_type = {}
    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        node_type = node.get("type", "")
        if node_type and node_type not in first_index:
            first_index[node_type] = idx
        name = node.get("name")
        if name is not None:
            name_to_type[name] = node_type

    all_node_types = set(first_index.keys())

    # Count errors per node type.
    counts = {}
    for issue in result.get("issues", []) or []:
        name = issue.get("node")
        if name and name in name_to_type:
            nt = name_to_type[name]
            if nt:
                counts[nt] = counts.get(nt, 0) + 1

    # Also catch node types mentioned directly in error messages. Attribute one
    # count per matching message so message-only errors still rank a node.
    for issue in result.get("issues", []) or []:
        msg = issue.get("message", "") or ""
        for m in re.finditer(r"n8n-[\w.-]+", msg):
            nt = m.group(0)
            if nt in all_node_types:
                counts[nt] = counts.get(nt, 0) + 1

    if not counts:
        return []

    # Descending count, ties broken by first appearance (ascending index).
    return sorted(
        counts.keys(),
        key=lambda nt: (-counts[nt], first_index.get(nt, len(nodes))),
    )


def build_cheatsheet(node_types, db_path=None):
    """Return a compact schema cheatsheet string, or None if unavailable."""
    if not node_types:
        return None

    db_path = db_path or _find_db()
    if not db_path:
        return None

    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return None

    sections = []
    # Node types dropped purely because of the _MAX_NODES cap (they were never
    # queried). Char-cap truncation can drop more; tracked separately below.
    omitted_by_node_cap = max(0, len(node_types) - _MAX_NODES)

    for nt in node_types[:_MAX_NODES]:
        row = db.execute(
            "SELECT display_name, version, operations, properties_schema FROM nodes WHERE node_type=?",
            (nt,),
        ).fetchone()

        if not row:
            continue

        display_name, version, ops_json, props_json = row

        # Restore full node type for Claude (nodes-base.slack → n8n-nodes-base.slack)
        full_nt = f"n8n-{nt}" if nt.startswith("nodes-") else nt

        lines = [f"### {display_name} ({full_nt}, typeVersion {version})"]

        if ops_json:
            ops = json.loads(ops_json)
            by_resource = {}
            for op in ops:
                r = op.get("resource", "")
                by_resource.setdefault(r, []).append(op["operation"])
            lines.append("Valid resource/operation combinations — use these EXACT values:")
            for res in sorted(by_resource):
                ops_str = " | ".join(sorted(by_resource[res]))
                lines.append(f'  resource="{res}": operation must be one of: {ops_str}')

        if props_json:
            rl_fields = _extract_resource_locator_fields(props_json)
            if rl_fields:
                lines.append("Resource locator fields — must use object format, NOT bare strings:")
                for field_name, modes in rl_fields.items():
                    modes_str = " | ".join(modes)
                    lines.append(
                        f'  {field_name}: {{"__rl": true, "value": "...", "mode": "{modes[0]}"}}'
                        f"  (modes: {modes_str})"
                    )
            has_filter = any(
                p.get("type") == "filter" for p in json.loads(props_json)
            )
            if has_filter:
                lines.append("IF/Filter conditions — use this EXACT structure (v2):")
                lines.append('  "conditions": {"combinator": "and", "conditions": [')
                lines.append('    {"id": "...", "leftValue": "={{ $json.field }}", '
                             '"rightValue": "...",')
                lines.append('     "operator": {"type": "string", "operation": "equals"}}]}')
                lines.append('  Valid operator types: string, number, boolean, dateTime')
                lines.append('  String operations: equals, notEquals, contains, '
                             'startsWith, endsWith, regex')
                lines.append('  Number operations: equals, notEquals, gt, gte, lt, lte')

        sections.append("\n".join(lines))

    db.close()

    if not sections:
        return None

    header = (
        "## n8n Node Schema (nodes.db)\n"
        "IMPORTANT: Use ONLY the operation values listed — any other value fails validation.\n"
    )
    body = "\n\n".join(sections)
    result = header + "\n" + body

    omitted = omitted_by_node_cap

    # Hard cap to stay within additionalContext limit. Reserve room so the
    # omission marker itself always fits inside _MAX_CHARS even after truncation.
    truncated = False
    if len(result) > _MAX_CHARS:
        truncated = True
        # Count how many whole sections survive the budget so the marker can
        # report an accurate omitted total (cap-dropped + truncation-dropped).
        kept_sections = 0
        running = len(header) + 1  # header + the leading "\n"
        for i, sec in enumerate(sections):
            added = len(sec) + (2 if i > 0 else 0)  # "\n\n" between sections
            if running + added > _MARKER_BUDGET:
                break
            running += added
            kept_sections += 1
        if kept_sections < 1:
            kept_sections = 1  # always show at least one section
        omitted += len(sections) - kept_sections
        body = "\n\n".join(sections[:kept_sections])
        result = header + "\n" + body
        if len(result) > _MARKER_BUDGET:
            result = result[:_MARKER_BUDGET] + "\n... (truncated)"

    if omitted > 0:
        result += (
            f"\n\n(+{omitted} more node schemas omitted — re-validate after fixing "
            "the nodes above to see them)"
        )

    return result


def main():
    node_types = sys.argv[1:]
    cheatsheet = build_cheatsheet(node_types)
    if cheatsheet:
        print(cheatsheet)


if __name__ == "__main__":
    main()
