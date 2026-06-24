#!/usr/bin/env python3
"""Export publishable eval stats from eval.db to a single stats.json for the
n8n-knowledge benchmark site.

Reuses the local dashboard's get_summary / get_rows / filter_rows / PRESETS so the
published numbers match the dashboard and report.py EXACTLY (single source of
truth — no re-implemented metric math here).

Usage:
  python3 scripts/eval/export_stats.py --out ../n8n-knowledge-site/src/data/stats.json
  python3 scripts/eval/export_stats.py --out stats.json --generated-at 2026-06-24T00:00:00Z

The site bakes the resulting JSON in at build time and filters it client-side; the
source eval.db never leaves the machine. `generated_at` is stamped here (passed in
or computed once) so the static build stays deterministic.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))  # scripts/eval

# Presets published to the public site. Deliberately a curated subset:
#   canonical      — the headline (newest run/prompt · V2 · full 128 battery)
#   gotcha         — the 28 known-bug-prompt view (pitfall avoidance denominator)
#   canonical_full — newest among COMPLETE runs only (no gap-fill runs)
# Excluded on purpose: 'off' (debug), 'pooled' (all-runs diagnostic — misleads a
# marketing audience), and 'legacy' (old V1 pre-fileoutput data; also currently
# trips a pre-existing None-guard gap in get_summary). These are NOT silently
# dropped — they are simply not publishable cuts.
PUBLISH_PRESET_IDS = ("canonical", "gotcha", "canonical_full")

# DeepSeek Pro run-id cohort (override via env DEEPSEEK_PRO_RUN_IDS="a,b"). Flash = deepseek
# runs NOT in this set; Pro = deepseek runs IN this set. Passed to get_summary so the published
# stats split DeepSeek into v4 Flash / v4 Pro tiers (the Pro run is newest per prompt and would
# otherwise silently overwrite Flash under the canonical newest-run-per-prompt basis).
DEEPSEEK_PRO_RUN_IDS = tuple(
    os.environ.get("DEEPSEEK_PRO_RUN_IDS", "20260624-135803-v2,20260624-122555-v2").split(",")
)


def load_server():
    """Load dashboard/server.py as a module (defines PRESETS, get_summary, get_rows,
    filter_rows). Importing has no network/server side effects — the HTTP server only
    starts under `if __name__ == '__main__'`."""
    sys.path.insert(0, HERE)  # server.py loads report.py by path; keep imports resolvable
    spec = importlib.util.spec_from_file_location(
        "dash_server", os.path.join(HERE, "dashboard", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_stats(server, generated_at, db_sha256):
    """Assemble the full publishable stats payload from the dashboard's own helpers.

    Returns a JSON-serializable dict:
      {generated_at, db_sha256, presets: [{id, label, summary, rows}, ...]}
    where `summary` is verbatim get_summary() output (basis/scope/sysprompt/cells)
    and `rows` is filter_rows(get_rows(), preset['rows']) — the per-prompt rows the
    explorer drills into.
    """
    by_id = {p["id"]: p for p in server.PRESETS}
    rows_all = server.get_rows()
    presets = []
    for pid in PUBLISH_PRESET_IDS:
        p = by_id[pid]
        s = p["summary"]
        summary = server.get_summary(s["sysprompt"], s["scope"], s["basis"], pro_run_ids=DEEPSEEK_PRO_RUN_IDS)
        rows = server.filter_rows(rows_all, p.get("rows") or {})
        presets.append({"id": pid, "label": p["label"], "summary": summary, "rows": rows})
    return {"generated_at": generated_at, "db_sha256": db_sha256, "presets": presets}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="path to write stats.json")
    ap.add_argument("--generated-at", default=None,
                    help="ISO8601 timestamp to stamp (default: now, UTC)")
    ap.add_argument("--readme-stats", default=None,
                    help="also write a rows-stripped summary JSON (for README rendering via render_readme.py)")
    args = ap.parse_args(argv)

    server = load_server()
    generated_at = args.generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_sha256 = sha256_file(server.DB)
    stats = build_stats(server, generated_at, db_sha256)

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"wrote {out}: {len(stats['presets'])} presets, "
          f"generated_at={generated_at}, db_sha256={db_sha256[:12]}…")

    if args.readme_stats:
        slim = {"generated_at": stats["generated_at"], "db_sha256": stats["db_sha256"],
                "presets": [{"id": p["id"], "label": p["label"], "summary": p["summary"]}
                            for p in stats["presets"]]}
        ro = os.path.abspath(args.readme_stats)
        os.makedirs(os.path.dirname(ro), exist_ok=True)
        with open(ro, "w") as f:
            json.dump(slim, f, indent=2)
        print(f"wrote {ro} (rows-stripped, for README rendering)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
