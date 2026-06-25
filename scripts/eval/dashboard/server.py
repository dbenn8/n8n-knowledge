#!/usr/bin/env python3
"""Local eval dashboard — view eval.db (summary + row level), filter/sort/select, and run
common actions on a selection (judge / retry-as-new-run / re-ingest / export).

Stdlib only (no Flask). Binds 127.0.0.1 — local tool, not for exposure.

Run:  python3 scripts/eval/dashboard/server.py   # then open http://127.0.0.1:8765
Actions execute as background jobs (Hybrid model): judge & re-ingest run immediately;
retry returns a command + cost estimate and only runs on an explicit confirm POST.
"""
import http.server
import importlib.util
import json
import os
import re
import socketserver
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse


def _median_s(times_ms):
    vals = [t for t in times_ms if t is not None]
    return round(statistics.median(vals) / 1000) if vals else None

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.abspath(os.path.join(HERE, ".."))          # scripts/eval
REPO = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))    # repo root
DB = os.path.join(REPO, "out", "eval", "eval.db")
JOBS_DIR = os.path.join(REPO, "out", "eval", "_dashboard_jobs")
os.makedirs(JOBS_DIR, exist_ok=True)
PORT = int(os.environ.get("EVAL_DASH_PORT", "8765"))
HOST = os.environ.get("EVAL_DASH_HOST", "127.0.0.1")  # service sets 0.0.0.0 for tailnet access
PY = sys.executable  # robust under launchd (bare PATH); shell wrappers rely on plist PATH

# Reuse report.py's exact metric math for the summary so numbers match the CLI.
_spec = importlib.util.spec_from_file_location("report", os.path.join(SCRIPT_DIR, "report.py"))
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

JOBS = {}  # job_id -> {"status","label","cmds","log","rc"}
_JOB_SEQ = [0]

ALLOWED_BACKENDS = {"deepseek", "claude"}
ALLOWED_CONDITIONS = {"plugin", "mcp", "bare"}


def db():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------ data ----
ROWS_SQL = """
SELECT re.run_id, r.ts, r.backend, re.condition, COALESCE(r.gate,'-') gate,
       r.system_prompt_version sysprompt, re.prompt_idx, re.run_number,
       re.is_error, re.validated, jv.intent_fit,
       re.gotcha_prompt, re.gotcha_addressed, re.num_turns,
       COALESCE(re.cost_usd_actual, re.cost_usd) cost,
       re.node_count, re.workflow_filename, r.excluded_reason, r.n_timeout
FROM results re JOIN runs r ON r.run_id=re.run_id
LEFT JOIN judge_verdicts jv ON jv.run_id=re.run_id AND jv.condition=re.condition
     AND jv.prompt_idx=re.prompt_idx AND jv.run_number=re.run_number
"""


def get_rows():
    con = db()
    rows = [dict(x) for x in con.execute(ROWS_SQL)]
    con.close()
    # eligible = not integrity-excluded
    for x in rows:
        x["eligible"] = 0 if x["excluded_reason"] else 1

    # The newest/newest_full flags must match report.py's published cell semantics EXACTLY so the
    # Rows view and Summary never disagree: (a) only clean rows (eligible, non-error = v_clean_results),
    # (b) exclude n_timeout>0 runs (report's default filter), (c) report ignores gate for non-plugin
    # conditions (mcp/bare are gate-agnostic). cell_key encodes (c).
    def cell_key(x):
        return (x["backend"], x["condition"], x["gate"] if x["condition"] == "plugin" else "*", x["prompt_idx"])

    def clean(x):
        return x["eligible"] and not x["is_error"] and not x["n_timeout"]

    best = {}
    for x in rows:
        if not clean(x):
            continue
        cur = best.get(cell_key(x))
        if cur is None or (x["ts"], x["run_id"], x["run_number"]) > cur[0]:
            best[cell_key(x)] = ((x["ts"], x["run_id"], x["run_number"]), x)
    newest_ids = {id(v[1]) for v in best.values()}
    # newest_full = newest among COMPLETE runs only (run+condition covering >=N distinct clean
    # prompts) — matches report.py basis='newest_full'. Keeps gap-fill runs out.
    cov = {}
    for x in rows:
        if clean(x):
            cov.setdefault((x["run_id"], x["condition"]), set()).add(x["prompt_idx"])
    complete = {k for k, v in cov.items() if len(v) >= report.COMPLETE_MIN_PROMPTS}
    bestf = {}
    for x in rows:
        if not clean(x) or (x["run_id"], x["condition"]) not in complete:
            continue
        cur = bestf.get(cell_key(x))
        if cur is None or (x["ts"], x["run_id"], x["run_number"]) > cur[0]:
            bestf[cell_key(x)] = ((x["ts"], x["run_id"], x["run_number"]), x)
    newest_full_ids = {id(v[1]) for v in bestf.values()}
    for x in rows:
        x["newest"] = 1 if id(x) in newest_ids else 0
        x["newest_full"] = 1 if id(x) in newest_full_ids else 0
        x["cost"] = round(x["cost"], 4) if x["cost"] is not None else None
    return rows


# Publishable presets — SINGLE SOURCE OF TRUTH for both Summary and Rows views, so they
# always stay in sync (and in sync with report.py, which the summary endpoint calls directly).
# `summary` drives the report params; `rows` is the equivalent row-filter set.
PRESETS = [
    {"id": "canonical", "label": "Canonical (newest · V2 · all)",
     "summary": {"basis": "newest", "scope": "all", "sysprompt": "v2-fileoutput"},
     "rows": {"newest": "1", "eligible": "1", "is_error": "0", "sysprompt": "v2-fileoutput"}},
    {"id": "canonical_full", "label": "Newest · full-coverage runs only",
     "summary": {"basis": "newest_full", "scope": "all", "sysprompt": "v2-fileoutput"},
     "rows": {"newest_full": "1", "eligible": "1", "is_error": "0", "sysprompt": "v2-fileoutput"}},
    {"id": "gotcha", "label": "Gotcha headline (newest · V2 · 28 gotchas)",
     "summary": {"basis": "newest", "scope": "gotcha", "sysprompt": "v2-fileoutput"},
     "rows": {"newest": "1", "eligible": "1", "is_error": "0", "sysprompt": "v2-fileoutput", "gotcha_prompt": "1"}},
    {"id": "pooled", "label": "Pooled diagnostic (all runs · V2)",
     "summary": {"basis": "pooled", "scope": "all", "sysprompt": "v2-fileoutput"},
     "rows": {"eligible": "1", "is_error": "0", "sysprompt": "v2-fileoutput"}},
    {"id": "legacy", "label": "Legacy V1 (newest · pre-fileoutput)",
     "summary": {"basis": "newest", "scope": "all", "sysprompt": "legacy-pre-fileoutput"},
     "rows": {"newest": "1", "eligible": "1", "is_error": "0", "sysprompt": "legacy-pre-fileoutput"}},
    {"id": "off", "label": "Off — no preset (all rows)",
     "summary": {"basis": "newest", "scope": "all", "sysprompt": "v2-fileoutput"},
     "rows": {}},
]


_CACHE = {"rows": None, "ts": 0.0}
_CAT_COLS = ["backend", "condition", "gate", "sysprompt", "validated", "is_error",
             "intent_fit", "gotcha_prompt", "gotcha_addressed", "newest", "newest_full",
             "eligible", "excluded_reason"]
_NUM_COLS = ["prompt_idx", "run_number", "num_turns", "cost", "node_count", "n_timeout"]


def cached_rows(refresh=False):
    """Full processed row set, cached ~10s so paging/sorting/filtering don't re-scan the DB
    on every keystroke. Server-side now (mobile can't sort 9k rows client-side)."""
    if refresh or _CACHE["rows"] is None or (time.time() - _CACHE["ts"]) > 10:
        _CACHE["rows"] = get_rows()
        _CACHE["ts"] = time.time()
    return _CACHE["rows"]


def _match_num(val, f):
    f = f.strip()
    if not f:
        return True
    m = re.fullmatch(r"([<>]=?|=)\s*(-?\d+\.?\d*)", f)
    if m and val is not None:
        n = float(m.group(2)); op = m.group(1); x = float(val)
        return {">": x > n, ">=": x >= n, "<": x < n, "<=": x <= n, "=": x == n}[op]
    return f.lower() in str(val).lower()


def filter_rows(rows, filters):
    out = rows
    for col, f in (filters or {}).items():
        if f == "" or f is None:
            continue
        if col in _CAT_COLS:
            out = [r for r in out if str(r.get(col) if r.get(col) is not None else "") == str(f)]
        elif col in _NUM_COLS:
            out = [r for r in out if _match_num(r.get(col), str(f))]
        else:
            out = [r for r in out if str(f).lower() in str(r.get(col) or "").lower()]
    return out


def sort_rows(rows, key, direction):
    if not key:
        return rows
    d = 1 if int(direction) >= 0 else -1
    def k(r):
        v = r.get(key)
        return (v is None, v if v is not None else "")
    try:
        return sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse=(d < 0))
    except TypeError:
        return sorted(rows, key=lambda r: (r.get(key) is None, str(r.get(key))), reverse=(d < 0))


def facets(rows):
    f = {}
    for c in _CAT_COLS:
        f[c] = sorted({str(r.get(c) if r.get(c) is not None else "") for r in rows})
    return f


ROW_KEY_COLS = ("run_id", "condition", "prompt_idx", "run_number")


def _common_idxs(cells):
    """Prompt indices the cells are compared over. Only (near-)complete cells — those
    covering >= 90% of the best-covered cell — define the common set, so an incomplete cell
    (e.g. a 66/128 bare run) is still reported over its OWN coverage (and qualified by its
    `cover` count) but can NOT shrink the comparison basis for the complete cells. `or cells`
    keeps the all-partial fallback (e.g. gap-fill-only scopes)."""
    cells = [c for c in cells if c[1]]
    if not cells:
        return []
    maxcov = max(len(d) for _, d in cells)
    comparable = [c for c in cells if len(c[1]) >= 0.9 * maxcov] or cells
    return sorted(set.intersection(*[set(d) for _, d in comparable]))


def get_summary(sysprompt, scope, basis, pro_run_ids=()):
    con = db()
    where = "1=1"
    params = []
    if sysprompt != "any":
        where += " AND r.system_prompt_version=?"
        params.append(sysprompt)
    where += " AND r.n_timeout=0"
    gotcha_only = scope == "gotcha"
    universe = 28 if gotcha_only else 128
    out = []

    # (group_key_backend, tier, run_clause, run_params)
    if pro_run_ids:
        ph = ",".join("?" * len(pro_run_ids))
        groups = [
            ("claude", None, "", []),
            ("deepseek", "flash", f" AND r.run_id NOT IN ({ph})", list(pro_run_ids)),
            ("deepseek", "pro", f" AND r.run_id IN ({ph})", list(pro_run_ids)),
        ]
    else:
        groups = [("claude", None, "", []), ("deepseek", None, "", [])]

    for backend, tier, run_clause, run_params in groups:
        where_g = where + run_clause
        params_g = params + run_params
        cells = []
        for label, cond, gate in report.CELLS:
            d = report.cell_prompt_stats(con, where_g, params_g, backend, cond, gate, gotcha_only, basis)
            if d:
                cells.append((label, d))
        if not cells:
            continue
        common = _common_idxs(cells)
        for label, d in cells:
            idxs = [i for i in common if i in d]
            if not idxs:
                continue
            mean = report._mean
            valid = 100 * mean([d[i]["val"] / d[i]["inst"] for i in idxs])
            ij = sum(d[i]["judged"] for i in idxs)
            intent = (100 * mean([d[i]["ipass"] / d[i]["judged"] for i in idxs if d[i]["judged"]])) if ij else None
            works_rate = (100 * mean([d[i]["works"] / d[i]["judged"] for i in idxs if d[i]["judged"]])) if ij else None
            gidxs = [i for i in idxs if d[i]["isg"]]
            gotcha = (100 * mean([d[i]["addr"] / d[i]["inst"] for i in gidxs])) if gidxs else None
            nvalp = sum(1 for i in idxs if d[i]["val"])
            njudp = sum(1 for i in idxs if d[i]["judged"])
            times = [d[i]["tms"] for i in idxs]
            out.append({
                "backend": backend, "tier": tier, "condition": label, "cover": f"{len(d)}/{universe}",
                "gotcha_pct": round(gotcha) if gotcha is not None else None,
                "valid_pct": round(valid),
                "correct_pct": round(valid * intent / 100) if intent is not None else None,
                "works_pct": round(valid * works_rate / 100) if works_rate is not None else None,
                "judged_pct": round(100 * njudp / nvalp) if nvalp else None,
                "avg_cost": round(mean([d[i]["cost"] for i in idxs]), 3),
                "turns": round(mean([d[i]["turns"] for i in idxs]), 1),
                "time_mean_s": round(mean([t for t in times if t is not None]) / 1000) if any(t is not None for t in times) else None,
                "time_median_s": _median_s(times),
                "inst": sum(d[i]["inst"] for i in idxs),
            })
    con.close()
    return {"basis": basis, "scope": scope, "sysprompt": sysprompt, "cells": out}


# --------------------------------------------------------------- actions ----
def _new_job(label, cmds):
    _JOB_SEQ[0] += 1
    jid = f"job{_JOB_SEQ[0]}"
    logpath = os.path.join(JOBS_DIR, f"{jid}.log")
    JOBS[jid] = {"status": "running", "label": label, "log": logpath, "rc": None}

    def run():
        rc = 0
        with open(logpath, "w") as fh:
            for c in cmds:
                fh.write(f"\n$ {' '.join(c)}\n"); fh.flush()
                p = subprocess.run(c, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT)
                rc = p.returncode
                if rc != 0:
                    fh.write(f"\n[exit {rc}]\n")
                    break
            fh.write("\n=== JOB DONE ===\n")
        JOBS[jid]["status"] = "done" if rc == 0 else "failed"
        JOBS[jid]["rc"] = rc

    threading.Thread(target=run, daemon=True).start()
    return jid


def _ints(seq):
    return sorted({int(x) for x in seq})


def action_judge(rows):
    """rows: [{run_id, condition, prompt_idx}] -> judge_results.py per (run_id, condition)."""
    groups = {}
    for r in rows:
        rid, cond = r["run_id"], r["condition"]
        if not re.fullmatch(r"[0-9a-zA-Z._-]+", rid) or cond not in ALLOWED_CONDITIONS:
            continue
        groups.setdefault((rid, cond), set()).add(int(r["prompt_idx"]))
    cmds = []
    for (rid, cond), idxs in sorted(groups.items()):
        d = os.path.join(REPO, "out", "eval", rid)
        if not os.path.isdir(d):
            continue
        cmds.append([PY, os.path.join(SCRIPT_DIR, "judge_results.py"), d,
                     "--conditions", cond, "--prompts", ",".join(map(str, sorted(idxs))),
                     "--model", "opus", "--yes"])
    cmds.append([PY, os.path.join(SCRIPT_DIR, "ingest_runs.py")])
    return _new_job(f"judge {sum(len(v) for v in groups.values())} prompts", cmds)


def retry_preview(prompts, backend, condition, runs):
    idxs = _ints(prompts)
    runs = max(1, min(5, int(runs)))
    # cost estimate from historical avg per (backend,condition,prompt)
    con = db()
    q = f"""SELECT AVG(COALESCE(cost_usd_actual,cost_usd)) c FROM v_eligible_runs r
            JOIN results re ON re.run_id=r.run_id
            WHERE r.backend=? AND re.condition=? AND re.is_error=0
              AND re.prompt_idx IN ({','.join('?'*len(idxs))})"""
    avg = con.execute(q, [backend, condition, *idxs]).fetchone()["c"] if idxs else None
    con.close()
    est = (avg or 0) * len(idxs) * runs
    wrapper = "deepseek.sh" if backend == "deepseek" else "claude.sh"
    cmd = (f"EVAL_PLUGIN_VALIDATOR_MODE=local bash scripts/eval/{wrapper} "
           f"--model claude-sonnet-4-6 --conditions {condition} --groups a,b,c "
           f"--prompt-file-idxs {','.join(map(str, idxs))} --runs {runs}")
    note = ("DeepSeek API ($)" if backend == "deepseek" else "Sonnet — uses Claude quota; auth preflight runs")
    return {"command": cmd, "prompts": idxs, "runs": runs, "backend": backend,
            "condition": condition, "est_cost_usd": round(est, 2), "note": note}


def retry_run(prompts, backend, condition, runs):
    if backend not in ALLOWED_BACKENDS or condition not in ALLOWED_CONDITIONS:
        return None
    idxs = _ints(prompts)
    runs = max(1, min(5, int(runs)))
    wrapper = "deepseek.sh" if backend == "deepseek" else "claude.sh"
    env_cmd = ["bash", os.path.join(SCRIPT_DIR, wrapper), "--model", "claude-sonnet-4-6",
               "--conditions", condition, "--groups", "a,b,c",
               "--prompt-file-idxs", ",".join(map(str, idxs)), "--runs", str(runs)]
    # EVAL_PLUGIN_VALIDATOR_MODE=local via env wrapper
    full = ["env", "EVAL_PLUGIN_VALIDATOR_MODE=local"] + env_cmd
    jid = _new_job(f"retry {backend}/{condition} {len(idxs)}p x{runs}", [full, [PY, os.path.join(SCRIPT_DIR, "ingest_runs.py")]])
    return jid


def action_reingest():
    return _new_job("re-ingest eval.db", [[PY, os.path.join(SCRIPT_DIR, "ingest_runs.py")]])


# --------------------------------------------------------------- server -----
class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(fh.read(), ctype="text/html; charset=utf-8")
        if u.path in ("/api/rows", "/api/keys"):
            try:
                filters = json.loads(q.get("filters", ["{}"])[0])
            except Exception:
                filters = {}
            rows = filter_rows(cached_rows("refresh" in q), filters)
            rows = sort_rows(rows, q.get("sort", [""])[0], q.get("dir", ["1"])[0])
            if u.path == "/api/keys":
                return self._send({"keys": ["|".join(str(r[c]) for c in ROW_KEY_COLS) for r in rows]})
            size = max(1, min(500, int(q.get("size", ["100"])[0])))
            page = max(0, int(q.get("page", ["0"])[0]))
            total = len(rows)
            page_rows = rows[page * size:(page + 1) * size]
            return self._send({"rows": page_rows, "total": total, "page": page, "size": size,
                               "pages": (total + size - 1) // size, "facets": facets(cached_rows())})
        if u.path == "/api/presets":
            return self._send({"presets": PRESETS})
        if u.path == "/api/summary":
            return self._send(get_summary(q.get("sysprompt", ["v2-fileoutput"])[0],
                                          q.get("scope", ["all"])[0],
                                          q.get("basis", ["newest"])[0]))
        if u.path == "/api/job":
            jid = q.get("id", [""])[0]
            j = JOBS.get(jid)
            if not j:
                return self._send({"error": "no such job"}, 404)
            tail = ""
            if os.path.exists(j["log"]):
                with open(j["log"]) as fh:
                    tail = fh.read()[-6000:]
            return self._send({"status": j["status"], "label": j["label"], "rc": j["rc"], "log": tail})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/judge":
            return self._send({"job": action_judge(body.get("rows", []))})
        if u.path == "/api/retry/preview":
            return self._send(retry_preview(body.get("prompts", []), body.get("backend", "deepseek"),
                                            body.get("condition", "plugin"), body.get("runs", 1)))
        if u.path == "/api/retry/run":
            jid = retry_run(body.get("prompts", []), body.get("backend", "deepseek"),
                            body.get("condition", "plugin"), body.get("runs", 1))
            return self._send({"job": jid} if jid else {"error": "bad args"}, 200 if jid else 400)
        if u.path == "/api/reingest":
            return self._send({"job": action_reingest()})
        return self._send({"error": "not found"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print(f"eval dashboard → http://{HOST}:{PORT}   (db: {DB})")
    ThreadingServer((HOST, PORT), Handler).serve_forever()
