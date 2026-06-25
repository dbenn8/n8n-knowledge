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

    # Output ordering lives in output_names/outputs on newer nodes.db builds; guard so
    # older DBs (without these columns) still work.
    _cols = {r[1] for r in db.execute("PRAGMA table_info(nodes)")}
    _out_sel = (", output_names, outputs"
                if ("output_names" in _cols or "outputs" in _cols) else ", NULL, NULL")

    for nt in node_types[:_MAX_NODES]:
        row = db.execute(
            "SELECT display_name, version, operations, properties_schema"
            + _out_sel + " FROM nodes WHERE node_type=?",
            (nt,),
        ).fetchone()

        if not row:
            continue

        display_name, version, ops_json, props_json, outnames_json, outputs_json = row

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

        # Output ordering for multi-output nodes. Wiring a branch to the wrong output
        # INDEX produces a schema-VALID but broken workflow (e.g. Split In Batches
        # done=0/loop=1, If true=0/false=1) — a top real-world failure that validation
        # cannot catch. The connection index order is fixed and often counterintuitive.
        out_names = None
        for col in (outnames_json, outputs_json):
            if not col:
                continue
            try:
                parsed = json.loads(col)
            except Exception:
                continue
            if isinstance(parsed, list) and len(parsed) > 1 and all(isinstance(x, str) for x in parsed):
                out_names = parsed
                break
        if out_names:
            lines.append("Outputs — wire each branch to the EXACT output index below "
                         "(order is fixed and can be counterintuitive):")
            for i, name in enumerate(out_names):
                lines.append(f'  output index {i} = "{name}"')

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


def workflow_node_types(workflow):
    """Distinct node types present in a workflow JSON, normalized to nodes.db form
    (n8n-nodes-base.X -> nodes-base.X, @n8n/n8n-nodes-langchain.X -> nodes-langchain.X)."""
    out, seen = [], set()
    for n in (workflow.get("nodes") or []):
        nt = n.get("type", "")
        if not nt:
            continue
        if nt.startswith("@n8n/n8n-"):
            db = nt[len("@n8n/n8n-"):]
        elif nt.startswith("n8n-"):
            db = nt[len("n8n-"):]
        else:
            db = nt
        if db not in seen:
            seen.add(db)
            out.append(db)
    return out


def multi_output_note(workflow, db_path=None):
    """Compact output-ordering reminder for every multi-output node ACTUALLY USED in
    the workflow — independent of whether the node was named in the prompt. This is the
    targeted fix for model-ADDED loop/branch nodes (Split In Batches, If, Switch, …)
    being wired to the wrong output index (schema-valid but broken). Returns a string
    or None."""
    db_path = db_path or _find_db()
    if not db_path:
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return None
    cols = {r[1] for r in db.execute("PRAGMA table_info(nodes)")}
    if not ("output_names" in cols or "outputs" in cols):
        return None
    lines = []
    for nt in workflow_node_types(workflow):
        row = db.execute(
            "SELECT display_name, output_names, outputs FROM nodes WHERE node_type=?", (nt,)
        ).fetchone()
        if not row:
            continue
        display_name, outn_json, outs_json = row
        names = None
        for col in (outn_json, outs_json):
            if not col:
                continue
            try:
                parsed = json.loads(col)
            except Exception:
                continue
            if isinstance(parsed, list) and len(parsed) > 1 and all(isinstance(x, str) for x in parsed):
                names = parsed
                break
        if names:
            order = ", ".join(f'index {i}="{n}"' for i, n in enumerate(names))
            lines.append(f"  {display_name}: {order}")
    if not lines:
        return None
    return ("Multi-output node wiring — these nodes you used have a FIXED, often "
            "counterintuitive output order. Wire each branch to the correct index "
            "(e.g. the loop/iteration body to \"loop\", post-loop steps to \"done\"):\n"
            + "\n".join(lines))


def _reaches(start_nodes, target, conns):
    """True if any node in start_nodes can reach `target` by following main connections."""
    seen, stack = set(), list(start_nodes)
    while stack:
        n = stack.pop()
        if n == target:
            return True
        if n in seen:
            continue
        seen.add(n)
        for branch in (conns.get(n, {}).get("main", []) or []):
            for b in (branch or []):
                stack.append(b["node"])
    return False


def detect_reversed_loop(workflow, db_path=None):
    """Conservative, data-driven detector for reversed loop-node wiring (the schema-VALID
    but functionally-dead #1 failure). For any node whose nodes.db output_names include
    'loop', find which output index actually loops back to the node; if a NON-'loop'
    output loops back (i.e. the loop body is on 'done' instead of 'loop'), it's reversed.
    Only flags genuine reversals — correct or non-looping wirings return nothing, so it
    can't push a correct workflow into a fix. Returns a list of issue dicts."""
    db_path = db_path or _find_db()
    if not db_path:
        return []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return []
    cols = {r[1] for r in db.execute("PRAGMA table_info(nodes)")}
    if not ("output_names" in cols or "outputs" in cols):
        return []
    conns = workflow.get("connections", {}) or {}
    issues = []
    for node in (workflow.get("nodes") or []):
        nt, name = node.get("type", ""), node.get("name")
        if not name:
            continue
        if nt.startswith("@n8n/n8n-"):
            db_nt = nt[len("@n8n/n8n-"):]
        elif nt.startswith("n8n-"):
            db_nt = nt[len("n8n-"):]
        else:
            db_nt = nt
        row = db.execute("SELECT output_names, outputs FROM nodes WHERE node_type=?", (db_nt,)).fetchone()
        if not row:
            continue
        names = None
        for col in row:
            if not col:
                continue
            try:
                parsed = json.loads(col)
            except Exception:
                continue
            if isinstance(parsed, list) and parsed and all(isinstance(x, str) for x in parsed):
                names = parsed
                break
        lower = [n.lower() for n in (names or [])]
        if not names or "loop" not in lower:
            continue  # only loop nodes have a reversal failure mode
        loop_idx = lower.index("loop")
        node_main = conns.get(name, {}).get("main", []) or []
        back_idx = None
        for i, branch in enumerate(node_main):
            starts = [b["node"] for b in (branch or [])]
            if starts and _reaches(starts, name, conns):
                back_idx = i
                break
        if back_idx is not None and back_idx != loop_idx:
            issues.append({
                "node": name, "wrong_output": names[back_idx] if back_idx < len(names) else f"index {back_idx}",
                "wrong_index": back_idx, "loop_output": names[loop_idx], "loop_index": loop_idx,
            })
    return issues


# Per-caveat body cap and how many caveat lines to emit — keep this injection compact
# (it rides alongside the schema cheatsheet inside the 10K additionalContext budget).
_CAVEAT_BODY_MAX = 220
_CAVEAT_MAX = 6


def _clean_md(s):
    """Strip Markdown link syntax and MkDocs attribute lists down to plain prose so a
    caveat reads cleanly in injected context: [text](url){:attrs} -> text, drop {:...}."""
    if not s:
        return ""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # [text](url) -> text
    s = re.sub(r"\{:[^}]*\}", "", s)                # {:target=_blank .external-link}
    s = re.sub(r"\{[^}]*\}", "", s)                 # any leftover {attr} block
    return re.sub(r"\s+", " ", s).strip()


def _parse_warning_admonitions(documentation):
    """Extract n8n's curated `/// warning | Title ... ///` admonition blocks from a
    node's documentation. Returns a list of (title, body) tuples, both cleaned of
    Markdown. ONLY the explicit `warning` admonition is matched — incidental substrings
    ('unimportant'), doc-lint HTML comments, and operation names like 'Warninglist' are
    deliberately ignored (they are not real caveats)."""
    if not documentation:
        return []
    out = []
    for raw in re.findall(r"///\s*warning\b(.*?)///", documentation, re.S | re.I):
        block = raw.strip()
        if block.startswith("|"):
            block = block[1:].strip()
        title, _, body = block.partition("\n")
        out.append((_clean_md(title), _clean_md(body)))
    return out


def node_caveats_note(workflow, db_path=None):
    """Compact 'known caveats' note for the nodes ACTUALLY USED in a workflow, sourced
    from n8n's curated `/// warning` admonitions in nodes.db (deprecations, free-plan
    limits, queue-mode/SSL restrictions, …). These are real user-facing gotchas the
    workflow validator can never surface. Returns a string or None.

    Like multi_output_note, this keys off the nodes present in the workflow JSON (so it
    also covers model-ADDED nodes), not just nodes named in the prompt."""
    db_path = db_path or _find_db()
    if not db_path:
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return None
    cols = {r[1] for r in db.execute("PRAGMA table_info(nodes)")}
    if "documentation" not in cols:
        return None
    lines = []
    for nt in workflow_node_types(workflow):
        row = db.execute(
            "SELECT display_name, documentation FROM nodes WHERE node_type=?", (nt,)
        ).fetchone()
        if not row:
            continue
        display_name, doc = row
        for title, body in _parse_warning_admonitions(doc):
            if len(body) > _CAVEAT_BODY_MAX:
                body = body[:_CAVEAT_BODY_MAX].rstrip() + "…"
            caveat = f"{title} — {body}" if body else title
            lines.append(f"  {display_name}: {caveat}")
            if len(lines) >= _CAVEAT_MAX:
                break
        if len(lines) >= _CAVEAT_MAX:
            break
    db.close()
    if not lines:
        return None
    return ("Node caveats — official n8n warnings for nodes in this workflow (deprecations, "
            "plan/version limits, or restrictions). Account for these or pick a different "
            "node where one is deprecated/unsupported:\n" + "\n".join(lines))


def main():
    node_types = sys.argv[1:]
    cheatsheet = build_cheatsheet(node_types)
    if cheatsheet:
        print(cheatsheet)


if __name__ == "__main__":
    main()
