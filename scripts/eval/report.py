#!/usr/bin/env python3
"""Fair, publishable comparison numbers from the eval database (out/eval/eval.db).

Three methodology rules are baked in so we never hand-roll a biased number again:

  1. INTEGRITY FIRST — every query reads `v_eligible_runs`, the view that excludes
     synthetic aggregate runs and exact re-ingests (see ingest_runs.py:tag_integrity).
     You physically cannot pull a duplicated/synthetic generation into a report.

  2. MACRO-AVERAGE — each distinct prompt contributes ONCE. We compute every prompt's
     value (gotcha rate, validity, cost, turns) first, then average across prompts. A
     micro-average (pool all rows) silently over-weights prompts that ran in more rounds,
     skewing toward whichever questions are over-sampled. This now applies to cost and
     turns too, not just rates — so duplicate/over-sampled rows wash out after the
     integrity filter, exactly as they should.

  3. COMMON-SET ONLY — when comparing conditions, every condition is scored on the
     INTERSECTION of prompts they all cover, never the union. Comparing one condition
     over 128 prompts against another over a different 40 is apples-to-oranges.

Comparability filters (on by default; the DB carries the provenance to enforce them):
  - system_prompt_version = current ('v2-fileoutput')  — the shared prompt is identical
  - n_timeout = 0                                        — no run truncated by a timeout

Scope:
  --scope gotcha   (default)  the 28 known-bug prompts; headline column is gotcha coverage
  --scope all                 all 128 prompts; headline is general workflow validity

Usage:
  python3 scripts/eval/report.py                          # gotcha view, both backends
  python3 scripts/eval/report.py --scope all              # full-suite validity/cost/turns
  python3 scripts/eval/report.py --backend claude --scope all
  python3 scripts/eval/report.py --system-prompt any --include-truncated   # loosen filters
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "..", "..", "out", "eval", "eval.db")

# A run counts as "complete" (for the newest_full basis) if it covered at least this many
# distinct prompts. SINGLE SOURCE — the dashboard imports this so the two never drift.
COMPLETE_MIN_PROMPTS = 120

# The cells we compare per backend. gate=None means "ignore gate" (mcp/bare have no gate).
CELLS = [
    ("plugin gate-ON (ship default)", "plugin", "on"),
    ("plugin gate-OFF", "plugin", "off"),
    ("mcp", "mcp", None),
    ("bare", "bare", None),
]


def cell_prompt_stats(con, where, params, backend, cond, gate, gotcha_only, basis="newest"):
    """Per-prompt facts for one comparison cell, over INTEGRITY-CLEAN rows only
    (reads v_eligible_runs). Returns {prompt_idx: Row(inst, val, cost, turns, addr, isg)}.
    cost/turns are per-prompt means (the macro building blocks).

    basis='newest' (default, CANONICAL): each prompt contributes ONLY its newest run, so
      validity, intent, gotcha and works are all measured on the SAME workflow the judge
      scored — consistent denominators, genuinely judge-complete (no validity-pooled vs
      intent-sampled mismatch). 'pooled' keeps every integrity-clean run per prompt (more
      validity samples, but intent is judged on only ~1 run/prompt → mismatched denominators).
    """
    g = " AND r.gate=?" if gate else ""
    p = list(params) + [backend, cond] + ([gate] if gate else [])
    gflt = " AND re.gotcha_prompt=1" if gotcha_only else ""
    # newest basis: rank each prompt's runs by recency and keep only rank 1 (rows sharing the
    # newest run_id all rank 1, so runs>1 within that run are retained). DENSE_RANK ties on ts
    # are broken by run_id so exactly one run wins per prompt.
    cell_filter = f"{where} AND r.backend=? AND re.condition=? {g}{gflt}"
    # Read the CANONICAL clean view (single source of truth): v_clean_results = integrity-eligible
    # AND is_error=0 (no-output operational failures only — timeouts/auth/caps; logic failures are
    # is_error=0 and stay IN). So ranking over it = "newest SUCCESSFUL run" per prompt.
    if basis in ("newest", "newest_full"):
        # newest_full = same as newest, but only consider runs that COVERED the full set
        # (a complete eval run), so scattered gap-fill runs never feed a published number.
        complete = (f" AND EXISTS (SELECT 1 FROM v_clean_results cc WHERE cc.run_id=re.run_id "
                    f"AND cc.condition=re.condition "
                    f"GROUP BY cc.run_id HAVING COUNT(DISTINCT cc.prompt_idx) >= {COMPLETE_MIN_PROMPTS})"
                    if basis == "newest_full" else "")
        # self-contained subquery carries the cell filter; outer keeps only the newest run.
        src = (f"""(SELECT re.*, DENSE_RANK() OVER
                          (PARTITION BY re.prompt_idx ORDER BY r.ts DESC, r.run_id DESC) rnk
                   FROM v_eligible_runs r JOIN v_clean_results re ON re.run_id=r.run_id
                   WHERE {cell_filter}{complete}) re""")
        outer_where = "re.rnk=1"
    else:
        # pooled: cell filter must come AFTER the LEFT JOIN, so it lives in the outer WHERE.
        src = "v_eligible_runs r JOIN v_clean_results re ON re.run_id=r.run_id"
        outer_where = cell_filter
    # Cost: prefer cost_usd_actual (backend-correct — DeepSeek runs through an Anthropic-
    # compatible endpoint, so Claude Code's cost_usd is a SONNET-priced estimate; the
    # deepseek-pro-priced figure lives in cost_usd_actual). Sonnet has no _actual (its
    # cost_usd IS real), so COALESCE gives the right number for every backend.
    rows = con.execute(
        f"""SELECT re.prompt_idx idx,
                  SUM(CASE WHEN re.is_error=0 THEN 1 ELSE 0 END) inst,
                  SUM(CASE WHEN re.is_error=0 THEN re.validated ELSE 0 END) val,
                  AVG(CASE WHEN re.is_error=0 THEN COALESCE(re.cost_usd_actual, re.cost_usd) END) cost,
                  AVG(CASE WHEN re.is_error=0 THEN re.num_turns END) turns,
                  AVG(CASE WHEN re.is_error=0 THEN re.total_input_tokens END) tin,
                  AVG(CASE WHEN re.is_error=0 THEN re.output_tokens END) tout,
                  AVG(CASE WHEN re.is_error=0 THEN re.time_ms END) tms,
                  SUM(CASE WHEN re.is_error=0 THEN re.gotcha_addressed ELSE 0 END) addr,
                  SUM(CASE WHEN re.is_error=0 AND re.validated=1 AND jv.intent_fit IS NOT NULL THEN 1 ELSE 0 END) judged,
                  SUM(CASE WHEN re.is_error=0 AND re.validated=1 AND jv.intent_fit='pass' THEN 1 ELSE 0 END) ipass,
                  -- works = judged run that is intent-correct AND gotcha-safe (gotcha-safe = not a
                  -- gotcha prompt, OR the gotcha was addressed). A flow that matches intent but walks
                  -- into a known n8n bug will NOT actually run, so it is not "working".
                  SUM(CASE WHEN re.is_error=0 AND re.validated=1 AND jv.intent_fit='pass'
                            AND (re.gotcha_prompt=0 OR re.gotcha_addressed=1) THEN 1 ELSE 0 END) works,
                  MAX(re.gotcha_prompt) isg
           FROM {src}
           LEFT JOIN judge_verdicts jv ON jv.run_id=re.run_id AND jv.condition=re.condition
                                      AND jv.prompt_idx=re.prompt_idx AND jv.run_number=re.run_number
           WHERE {outer_where}
           GROUP BY re.prompt_idx HAVING inst>0""",
        p,
    ).fetchall()
    return {r["idx"]: r for r in rows}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else float("nan")


def _fmt_tokens(n):
    if n is None or n != n:  # NaN
        return "—"
    return f"{n/1e6:.2f}M" if n >= 1e6 else f"{n/1e3:.0f}k"


def _fmt_time(ms):
    if ms is None or ms != ms:
        return "—"
    return f"{ms/1000:.0f}s"


def report(db, backends, sysprompt, include_truncated, scope, show_intent=False, basis="newest",
           min_run_date=None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    where = "1=1"
    params: list = []
    if sysprompt != "any":
        where += " AND r.system_prompt_version=?"
        params.append(sysprompt)
    if not include_truncated:
        where += " AND r.n_timeout=0"
    if min_run_date:
        # publish-baseline: drop runs generated under code OLDER than this (ts is the run_id
        # timestamp prefix, e.g. '20260621'). Excludes older-logic runs from a published cut.
        where += " AND r.ts >= ?"
        params.append(min_run_date)

    universe = 28 if scope == "gotcha" else 128
    gotcha_only = scope == "gotcha"
    basis_note = {
        "newest": "newest run per prompt — consistent denominators, judge-complete",
        "newest_full": "newest run per prompt, COMPLETE runs only (no gap-fill runs)",
        "pooled": "POOLED over all runs per prompt — DIAGNOSTIC: validity pooled, intent judged on ~1 run/prompt",
    }.get(basis, basis)
    print("Eval report — macro-average over the COMMON prompt set (integrity-clean: v_eligible_runs)")
    print(f"basis: {basis} ({basis_note})")
    print(f"scope: {scope} (universe {universe})  "
          f"filters: system_prompt={sysprompt}  "
          f"truncated_runs={'included' if include_truncated else 'EXCLUDED (n_timeout=0)'}\n")

    for backend in backends:
        cells = []
        for label, cond, gate in CELLS:
            d = cell_prompt_stats(con, where, params, backend, cond, gate, gotcha_only, basis)
            if d:
                cells.append((label, cond, gate, d))
        if not cells:
            print(f"### {backend.upper()}: no comparable data\n")
            continue
        # Common set is defined by the WELL-COVERED cells only (>=50% of the universe).
        # A thinly-covered cell (e.g. a current-era `bare` baseline, or an in-progress
        # run) must NOT shrink the common set and nuke the real plugin-vs-mcp comparison —
        # it is still shown, but flagged "(partial)" and scored over what it does cover.
        thresh = math.ceil(0.5 * universe)
        comparable = [c for c in cells if len(c[3]) >= thresh]
        basis = comparable if comparable else cells
        common = sorted(set.intersection(*[set(d) for _, _, _, d in basis]))
        # gotcha prompts within the common set (for the gotcha% column under --scope all)
        gcommon = [i for i in common if basis[0][3][i]["isg"]]

        print(f"### {backend.upper()}   (common set: {len(common)}/{universe} prompts "
              f"from {len(basis)} well-covered cells; {len(gcommon)} of them gotchas)")
        if len(common) < (20 if scope == "gotcha" else 90):
            print(f"  ⚠️  thin common set ({len(common)}) — even the well-covered cells overlap little; low-confidence.")
        # correct% (valid% × intent%) is the headline accuracy metric — fraction of ALL attempts
        # that are completely correct. The raw conditional intent% (P(correct | schema-valid)) is a
        # diagnostic only, hidden unless --show-intent, because on its own it misleads: a tool with
        # high intent% over a small valid base is NOT producing more correct outputs.
        # works% = correct% AND gotcha-safe = fraction of ALL attempts that would actually run
        # (schema-valid + matches intent + doesn't walk into a known n8n bug). This is the truest
        # "did it produce a working flow" metric. judged% = share of valid runs the LLM judge
        # actually scored — low judged% means correct%/works% are estimates over a small sample.
        icol = f"{'intent%':>8} " if show_intent else ""
        hdr = (f"{'condition':32} {'cover':>7} {'gotcha%':>8} {'valid%':>7} {icol}{'correct%':>9} "
               f"{'works%':>7} {'judged%':>8} {'avg$':>8} {'turns':>6} {'tok_in':>8} {'tok_out':>8} {'time':>7} {'inst':>6}")
        print(hdr)
        print("-" * len(hdr))
        for label, cond, gate, d in cells:
            idxs = [i for i in common if i in d]          # full common for comparable cells
            gidxs = [i for i in gcommon if i in d]
            if not idxs:
                print(f"{label:32} {len(d)}/{universe:<5}  (no overlap with common set)")
                continue
            valid = 100 * _mean([d[i]["val"] / d[i]["inst"] for i in idxs])
            ijudged = sum(d[i]["judged"] for i in idxs)
            intent = (100 * _mean([d[i]["ipass"] / d[i]["judged"] for i in idxs if d[i]["judged"]])
                      ) if ijudged else float("nan")
            cost = _mean([d[i]["cost"] for i in idxs])
            turns = _mean([d[i]["turns"] for i in idxs])
            tin = _mean([d[i]["tin"] for i in idxs])
            tout = _mean([d[i]["tout"] for i in idxs])
            tms = _mean([d[i]["tms"] for i in idxs])
            gmacro = (100 * _mean([d[i]["addr"] / d[i]["inst"] for i in gidxs])) if gidxs else float("nan")
            inst = sum(d[i]["inst"] for i in idxs)
            cover = f"{len(d)}/{universe}"
            gtxt = f"{gmacro:.0f}%" if gidxs else "—"
            partial = len(idxs) < len(common)
            flag = "  ⚠️ partial" if partial else ""
            # correct% = valid% × intent% = P(schema-valid AND intent-correct).
            correct = (valid * intent / 100) if ijudged else float("nan")
            ctxt = f"{correct:.0f}%" if ijudged else "—"
            # works% = valid% × P(intent-pass AND gotcha-safe | valid&judged) = P(actually runs).
            worksrate = (100 * _mean([d[i]["works"] / d[i]["judged"] for i in idxs if d[i]["judged"]])
                         ) if ijudged else float("nan")
            works = (valid * worksrate / 100) if ijudged else float("nan")
            wtxt = f"{works:.0f}%" if ijudged else "—"
            # judged% = of the prompts that produced a valid workflow, the share that have a judge
            # verdict (reliability of correct%/works%). PROMPT coverage, not run coverage: the judge
            # scores one run per prompt by design, so run-coverage would read misleadingly low.
            nvalp = sum(1 for i in idxs if d[i]["val"])
            njudp = sum(1 for i in idxs if d[i]["judged"])
            jpct = (100 * njudp / nvalp) if nvalp else float("nan")
            jtxt = f"{jpct:.0f}%" if nvalp else "—"
            icell = (f"{(f'{intent:.0f}%' if ijudged else '—'):>8} ") if show_intent else ""
            print(f"{label:32} {cover:>7} {gtxt:>8} {valid:>6.0f}% {icell}{ctxt:>9} "
                  f"{wtxt:>7} {jtxt:>8} {cost:>8.3f} {turns:>6.1f} {_fmt_tokens(tin):>8} {_fmt_tokens(tout):>8} "
                  f"{_fmt_time(tms):>7} {inst:>6}{flag}")
        print()
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.abspath(DEFAULT_DB))
    ap.add_argument("--backend", default="all", help="deepseek | claude | all")
    ap.add_argument("--scope", default="gotcha", choices=["gotcha", "all"],
                    help="gotcha = 28 known-bug prompts (default); all = full 128-prompt suite")
    ap.add_argument("--system-prompt", default="v2-fileoutput", help="version label, or 'any'")
    ap.add_argument("--include-truncated", action="store_true",
                    help="include timeout-truncated runs (default: excluded)")
    ap.add_argument("--show-intent", action="store_true",
                    help="also show the raw conditional intent% (P(correct|valid)) diagnostic column; "
                         "by default only the headline correct% (valid%×intent%) is shown")
    ap.add_argument("--basis", default="newest", choices=["newest", "newest_full", "pooled"],
                    help="newest (default, canonical): one representative run per prompt — all metrics "
                         "on the same judged workflow. pooled: every run per prompt (diagnostic; "
                         "validity pooled but intent judged on ~1 run/prompt → mismatched denominators)")
    a = ap.parse_args()
    backends = ["deepseek", "claude"] if a.backend == "all" else [a.backend]
    report(os.path.abspath(a.db), backends, a.system_prompt, a.include_truncated, a.scope,
           a.show_intent, a.basis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
