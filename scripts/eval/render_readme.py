"""Render the README eval tables from the canonical stats (single source of truth)."""
import json
import sys

# Display label per (backend, tier) group.
TIER_LABEL = {
    ("claude", None): "Claude Sonnet 4.6",
    ("deepseek", "flash"): "DeepSeek v4 Flash",
    ("deepseek", "pro"): "DeepSeek v4 Pro",
}
# Editorial provenance suffix per group (kept here so re-rendering preserves it).
SUFFIX = {
    ("claude", None): " — a full 128-prompt run on the current shipped plugin:",
    ("deepseek", "flash"): " — latest available per prompt:",
    ("deepseek", "pro"): " — clean v4 Pro run (gate-ON vs n8n-mcp):",
}
HEADER = ("| Condition | valid% | correct% | works% | pitfall% | $/run | turns | time (mean / median) |\n"
          "|---|---|---|---|---|---|---|---|")


def _cell(cells, backend, tier, prefix):
    for c in cells:
        if c["backend"] == backend and c.get("tier") == tier and c["condition"].startswith(prefix):
            return c
    return None


def _row(c, label, bold=False):
    def pct(v):
        return "—" if v is None else f"{v}%"  # bare is unjudged → no correct%/works%
    vals = [pct(c['valid_pct']), pct(c['correct_pct']), pct(c['works_pct']),
            pct(c['gotcha_pct']), f"${c['avg_cost']}", f"{c['turns']}",
            f"{c['time_mean_s']}s / {c['time_median_s']}s"]
    cells = ([f"**{label}**"] + [f"**{v}**" for v in vals]) if bold else ([label] + vals)
    return "| " + " | ".join(cells) + " |"


def render_eval_tables(presets):
    canon = next(p for p in presets if p["id"] == "canonical")
    cells = canon["summary"]["cells"]
    blocks = []
    for backend, tier in [("claude", None), ("deepseek", "flash"), ("deepseek", "pro")]:
        plugin = _cell(cells, backend, tier, "plugin gate-ON")
        mcp = _cell(cells, backend, tier, "mcp")
        if not plugin or not mcp:
            continue
        rows = [_row(plugin, "plugin (gate-ON, ship default)", bold=True), _row(mcp, "n8n-mcp")]
        bare = _cell(cells, backend, tier, "bare")
        if bare:
            # Raw model, no tools — the status-quo baseline. Unjudged, so the story is valid%.
            # Qualify with its prompt count when only partially covered.
            n, full = bare["cover"].split("/")
            blabel = "raw model — no tools" + (f" ({bare['cover']})" if n != full else "")
            rows.append(_row(bare, blabel))
        label = TIER_LABEL[(backend, tier)]
        blocks.append(f"**{label}**{SUFFIX.get((backend, tier), '')}\n\n{HEADER}\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def splice(text, section, content):
    start = f"<!-- AUTOGEN:{section} START -->"
    end = f"<!-- AUTOGEN:{section} END -->"
    if start not in text or end not in text:
        raise ValueError(f"missing AUTOGEN markers for section '{section}'")
    pre = text.split(start)[0]
    post = text.split(end, 1)[1]
    return f"{pre}{start}\n{content}\n{end}{post}"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stats", required=True, help="published_stats.json path")
    ap.add_argument("--readme", default="README.md")
    args = ap.parse_args(argv)
    stats = json.load(open(args.stats))
    tables = render_eval_tables(stats["presets"])
    with open(args.readme) as f:
        text = f.read()
    out = splice(text, "eval-tables", tables)
    with open(args.readme, "w") as f:
        f.write(out)
    print(f"rendered eval tables into {args.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
