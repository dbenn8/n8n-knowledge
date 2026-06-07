#!/usr/bin/env python3
"""Format injected context for human-readable debug output."""
import json
import re
import sys


def parse_results(ctx):
    """Extract result blocks from the raw context string."""
    results = []
    for match in re.finditer(
        r'<result\s+([^>]+)>(.*?)</result>', ctx, re.DOTALL
    ):
        attrs_str = match.group(1)
        body = match.group(2).strip()
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', attrs_str))
        results.append({"attrs": attrs, "body": body})
    return results


def format_node_spec(r, mode):
    """Format a node-spec result."""
    body = r["body"]
    lines = []

    node_match = re.search(r'Node:\s*(.+)', body)
    op_match = re.search(r'Operation:\s*(.+)', body)
    node = node_match.group(1).strip() if node_match else ""
    op = op_match.group(1).strip() if op_match else ""

    if node and op:
        lines.append(f"  {node} — {op}")
    elif node:
        lines.append(f"  {node}")

    desc_lines = []
    field_lines = []
    in_fields = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Node:") or stripped.startswith("Operation:"):
            continue
        if stripped.startswith("Full property spec"):
            continue
        if stripped.startswith("Source: n8n node"):
            continue
        if stripped.startswith("Fields (") or stripped.startswith("Fields:"):
            in_fields = True
            if mode == "summary":
                field_names = []
                continue
            else:
                lines.append(f"  {stripped}")
                continue
        if in_fields:
            if mode == "summary":
                name_match = re.match(r'(\w+)\s*\(', stripped)
                if name_match:
                    req = " *" if "required" in stripped else ""
                    field_names.append(name_match.group(1) + req)
            else:
                lines.append(f"    {stripped}")
            continue
        if stripped and not stripped.startswith("Node description:"):
            desc_lines.append(stripped)

    if mode == "summary" and in_fields:
        lines.append(f"  Fields: {', '.join(field_names)}")
    if desc_lines:
        lines.append(f"  {desc_lines[0][:120]}")

    return "\n".join(lines)


def format_github(r, mode):
    """Format a GitHub issue/PR result."""
    body = r["body"]
    lines = []

    state_match = re.match(r'\[([^\]]+)\]', body)
    state = state_match.group(1) if state_match else ""
    text = body[state_match.end():].strip() if state_match else body

    source_match = re.search(r'Source:\s*(https?://\S+)', body)
    source = source_match.group(1) if source_match else ""
    source_short = re.sub(r'https?://(github\.com/)', r'\1', source)

    meta_match = re.search(r'(\d+)\s+reactions?,\s*(\d+)\s+comments?', body)
    meta = f"{meta_match.group(1)} reactions · {meta_match.group(2)} comments" if meta_match else ""

    issue_match = re.search(r'#(\d+)', text)
    issue_num = f"#{issue_match.group(1)}" if issue_match else ""

    text_clean = re.sub(r'\s*\|[^|]*$', '', text)
    text_clean = re.sub(r'Source:.*', '', text_clean).strip()
    text_clean = re.sub(r'\d+\s+reactions?,\s*\d+\s+comments?', '', text_clean).strip()

    if mode == "summary":
        text_clean = text_clean[:150]

    if state:
        lines.append(f"  [{state}] {issue_num}")
    lines.append(f"  {text_clean}")
    parts = [p for p in [source_short, meta] if p]
    if parts:
        lines.append(f"  {' · '.join(parts)}")

    return "\n".join(lines)


def format_community(r, mode):
    """Format a community post result."""
    body = r["body"]
    lines = []

    source_match = re.search(r'Source:\s*(https?://\S+)', body)
    source = source_match.group(1) if source_match else ""
    source_short = re.sub(r'https?://(community\.n8n\.io/)', r'\1', source)

    meta_parts = []
    if "solved" in body.lower().split("source")[0][:20] or "| solved" in body:
        meta_parts.append("solved")
    for label, pattern in [("votes", r'(\d+)\s+votes'), ("likes", r'(\d+)\s+likes'), ("views", r'(\d+)\s+views')]:
        m = re.search(pattern, body)
        if m and m.group(1) != "0":
            meta_parts.append(f"{m.group(1)} {label}")

    text_clean = re.sub(r'\s*Source:.*', '', body, flags=re.DOTALL).strip()
    text_clean = re.sub(r'\s*\|[^|]*$', '', text_clean).strip()
    if mode == "summary":
        text_clean = text_clean[:150]

    lines.append(f"  {text_clean}")
    parts = [p for p in [source_short] + [' · '.join(meta_parts)] if p]
    if parts:
        lines.append(f"  {' · '.join([p for p in parts if p])}")

    return "\n".join(lines)


def format_synthesis(r, mode):
    """Format a synthesis result."""
    body = r["body"]
    sources_count = r["attrs"].get("sources", "?")

    source_line = ""
    sources_match = re.search(r'sources:\s*(.+)', body)
    if sources_match:
        source_line = sources_match.group(1).strip()

    text_clean = re.sub(r'\nsources:.*', '', body, flags=re.DOTALL).strip()
    text_clean = re.sub(r'\nnote: machine-distilled.*', '', text_clean, flags=re.DOTALL).strip()
    if mode == "summary":
        text_clean = text_clean[:150]

    lines = [f"  {text_clean}"]
    if source_line:
        if mode == "summary":
            source_line = source_line[:120]
        lines.append(f"  Sources: {source_line}")

    return "\n".join(lines)


def format_debug(ctx, mode, source_label="auto-recall"):
    """Format the full context for debug output."""
    results = parse_results(ctx)
    if not results:
        return f"┌─── n8n-knowledge: {source_label} (no results) ───┐\n└────────────────────────────────────────────────────┘\n"

    out = [f"\n┌─── n8n-knowledge: {source_label} ({len(results)} results) ───┐\n"]

    for r in results:
        attrs = r["attrs"]
        n = attrs.get("n", "?")
        kind = attrs.get("kind", "?").upper()
        conf = attrs.get("confidence", "?")
        source = attrs.get("source", "")
        sources = attrs.get("sources", "")

        label_parts = [kind]
        if source:
            label_parts = [source.upper()]
        label_parts.append(conf)
        if sources:
            label_parts.append(f"{sources} sources")
        label = " · ".join(label_parts)

        out.append(f"━━━ {n} ━━━ {label} ━━━")

        if kind == "NODE-SPEC":
            out.append(format_node_spec(r, mode))
        elif source == "github":
            out.append(format_github(r, mode))
        elif source == "community":
            out.append(format_community(r, mode))
        elif kind == "SYNTHESIS":
            out.append(format_synthesis(r, mode))
        else:
            body = r["body"].strip()
            if mode == "summary":
                body = body[:200]
            out.append(f"  {body}")

        out.append("")

    out.append("└────────────────────────────────────────────────────┘\n")
    return "\n".join(out)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "summary"
    label = sys.argv[2] if len(sys.argv) > 2 else "auto-recall"
    ctx = sys.stdin.read()
    print(format_debug(ctx, mode, label))
