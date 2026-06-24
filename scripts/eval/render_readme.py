"""Render the README eval tables from the canonical stats (single source of truth)."""
import json
import sys

BACKEND_LABEL = {"claude": "Claude Sonnet 4.6", "deepseek": "DeepSeek v4 Flash"}
# Editorial provenance suffix per backend (kept here so re-rendering preserves it).
SUFFIX = {
    "claude": " — a full 128-prompt run on the current shipped plugin:",
    "deepseek": " — latest available per prompt:",
}
HEADER = "| Condition | valid% | correct% | works% | pitfall% | $/run | turns |\n|---|---|---|---|---|---|---|"


def _cell(cells, backend, prefix):
    for c in cells:
        if c["backend"] == backend and c["condition"].startswith(prefix):
            return c
    return None


def _row(c, bold):
    vals = [f"{c['valid_pct']}%", f"{c['correct_pct']}%", f"{c['works_pct']}%",
            f"{c['gotcha_pct']}%", f"${c['avg_cost']}", f"{c['turns']}"]
    if bold:
        cells = ["**plugin (gate-ON, ship default)**"] + [f"**{v}**" for v in vals]
    else:
        cells = ["n8n-mcp"] + vals
    return "| " + " | ".join(cells) + " |"


def render_eval_tables(presets):
    canon = next(p for p in presets if p["id"] == "canonical")
    cells = canon["summary"]["cells"]
    blocks = []
    for backend in ("claude", "deepseek"):
        plugin = _cell(cells, backend, "plugin gate-ON")
        mcp = _cell(cells, backend, "mcp")
        if not plugin or not mcp:
            continue
        blocks.append(
            f"**{BACKEND_LABEL[backend]}**{SUFFIX.get(backend, '')}\n\n{HEADER}\n{_row(plugin, True)}\n{_row(mcp, False)}")
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
