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
import sqlite3
import sys

from plugin_config import find_n8n_mcp_install_root

# Character budget — keep total injection under 8K to stay within
# Claude Code's 10K additionalContext limit alongside recall results.
_MAX_CHARS = 6000
_MAX_NODES = 5


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

    for nt in node_types[:_MAX_NODES]:
        row = db.execute(
            "SELECT display_name, version, operations FROM nodes WHERE node_type=?",
            (nt,),
        ).fetchone()

        if not row:
            continue

        display_name, version, ops_json = row
        if not ops_json:
            continue

        ops = json.loads(ops_json)
        by_resource = {}
        for op in ops:
            r = op.get("resource", "")
            by_resource.setdefault(r, []).append(op["operation"])

        # Restore full node type for Claude (nodes-base.slack → n8n-nodes-base.slack)
        full_nt = f"n8n-{nt}" if nt.startswith("nodes-") else nt

        lines = [f"### {display_name} ({full_nt}, typeVersion {version})"]
        lines.append("Valid resource/operation combinations — use these EXACT values:")
        for res in sorted(by_resource):
            ops_str = " | ".join(sorted(by_resource[res]))
            lines.append(f'  resource="{res}": operation must be one of: {ops_str}')

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

    # Hard cap to stay within additionalContext limit
    if len(result) > _MAX_CHARS:
        result = result[:_MAX_CHARS] + "\n... (truncated)"

    return result


def main():
    node_types = sys.argv[1:]
    cheatsheet = build_cheatsheet(node_types)
    if cheatsheet:
        print(cheatsheet)


if __name__ == "__main__":
    main()
