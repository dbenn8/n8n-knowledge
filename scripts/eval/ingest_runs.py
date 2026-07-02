#!/usr/bin/env python3
"""Ingest every eval run under out/eval/ into a queryable SQLite database.

Why this exists
---------------
Run folders accumulated across weeks with drifting meta/validation schemas (additive,
but ~8/11 variants), and only ~1/4 carry a config manifest. Reconstructing comparable
statistics by hand is error-prone. This script turns the whole pile into two tables you
can query with SQL and publish from:

  runs     — one row per run: config provenance (backend, gate, validator, prompt set,
             hashes of the prompt + rules files, completeness, provenance confidence).
  results  — one row per (run, condition, prompt_idx, run_number): the per-session FACTS
             parsed straight from meta.json/validation.json, PLUS the gotcha score
             computed with the SAME canonical primitives the published scorer uses.

Design rules:
  * Numbers are ALWAYS parsed/scored by code, never guessed — the DB is auditable.
  * Defensive parsing: missing keys -> NULL, so every schema variant ingests cleanly.
  * Idempotent: re-running UPSERTs (INSERT OR REPLACE on the natural keys).
  * Provenance honesty: runs with a manifest are 'manifest'; the rest are 'reconstructed'
    (backend from cost_model, gate from transcript content, prompt set from present files)
    so published queries can filter to high-confidence runs.

Usage:
  python3 scripts/eval/ingest_runs.py                 # ingest all of out/eval/ -> out/eval/eval.db
  python3 scripts/eval/ingest_runs.py --db /path.db --root out/eval --only 20260617-114714-v2
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# Reuse the canonical scoring primitives so DB scores == published scores.
from gotcha_scoring import (  # noqa: E402
    load_rules,
    load_gotcha_patterns,
    check_rule,
    _load_workflow,
)

RUN_FILE_RE = re.compile(r"prompt-(\d+)-run(\d+)\.meta\.json$")
# A REAL run folder is named YYYYMMDD-HHMMSS(-v2). Anything else under out/eval that
# carries result rows (e.g. *-combined, *-best-combined) is a hand-built aggregate that
# re-counts generations already present as real runs — never let it into a report.
TS_RE = re.compile(r"^\d{8}-\d{6}")
GATE_MARKER = "Known-bug design gate"

# System-prompt version registry (single source of truth; keep 'current' updated).
_SP = json.load(open(os.path.join(SCRIPT_DIR, "system_prompts.json")))
SP_CURRENT = _SP["current"]
SP_BOUNDARY = datetime.fromisoformat(SP_CURRENT["since_utc"].replace("Z", "+00:00"))


def resolve_system_prompt(run_id: str, manifest: dict | None):
    """Return (version_label, hash_or_None, source). Manifest stamp is authoritative;
    otherwise infer the version by the run's timestamp vs the current-prompt boundary
    (label only — we did NOT capture the actual prompt for pre-manifest runs)."""
    if manifest and manifest.get("system_prompt_hash"):
        return (manifest.get("system_prompt_version") or "manifest",
                manifest["system_prompt_hash"], "manifest")
    m = re.match(r"(\d{8})-(\d{6})", run_id)
    if not m:
        return ("unknown", None, "uncaptured")
    try:
        ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ("unknown", None, "uncaptured")
    if ts >= SP_BOUNDARY:
        return (SP_CURRENT["version"], None, "inferred-by-date")  # label only; hash unverified
    return ("legacy-pre-fileoutput", None, "inferred-by-date")


def sha8(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]


def jload(path: str):
    try:
        return json.load(open(path))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-(condition, prompt, run) file resolution — parametrized by run number
# (the canonical helpers hardcode run01; we need every run for --runs > 1).
# ---------------------------------------------------------------------------

def _base(cond_dir: str, idx: int, run: int) -> str:
    return os.path.join(cond_dir, f"prompt-{idx:03d}-run{run:02d}")


def _workflow_path(base: str) -> str | None:
    for wf in sorted(glob.glob(base + ".workflow/*.json")) + [
        base + ".candidate.workflow.json",
        base + ".scratch.workflow.json",
    ]:
        if os.path.exists(wf):
            return wf
    return None


def classify_error_type(payload: dict) -> str | None:
    """Classify an errored claude -p / harness payload into a diagnostic label.

    Pure + dependency-free: operates only on the passed-in dict (no I/O, no globals).
    Priority order:
      1. Not an error (no is_error and no error) -> None.
      2. Harness-injected top-level `error` (truthy) -> returned VERBATIM
         (preserves the model_invocation_timeout / model_invocation_failed paths).
      3. Classify from the `result` text (case-insensitive substring), first match wins.
      4. Errored but unmatched -> "unknown_error" (no errored row stays NULL).
    """
    if not payload:
        return None
    if not (payload.get("is_error") or payload.get("error")):
        return None
    harness_err = payload.get("error")
    if harness_err:
        return harness_err  # verbatim passthrough
    r = (payload.get("result") or "").lower()
    if "output token maximum" in r or ("exceeded" in r and "token" in r):
        return "output_token_limit"
    if "request timed out" in r or "timed out" in r:
        return "request_timeout"
    if ("failed to authenticate" in r or "not logged in" in r
            or "invalid bearer token" in r or "401" in r):
        return "auth_error"
    # Claude usage caps (retryable after reset, NOT real failures — same halt/resume recovery):
    # weekly cap resets in days, session cap in hours. "You've hit your session limit · resets ...".
    if "weekly limit" in r or ("week" in r and ("limit" in r or "reset" in r)):
        return "weekly_limit"
    if "session limit" in r or "usage limit" in r or "hit your" in r or "reached your" in r:
        return "session_limit"
    if "rate limit" in r or "429" in r or "overloaded" in r:
        return "rate_limit"
    return "unknown_error"


def _output_text(base: str) -> tuple[str, bool]:
    resp = base + ".json"
    if not os.path.exists(resp):
        return "", True
    parts, is_error = [], False
    payload = jload(resp)
    if payload is None:
        return "", True
    is_error = bool(payload.get("is_error") or payload.get("error"))
    parts.append(payload.get("result", "") or "")
    wf = _workflow_path(base)
    if wf:
        try:
            parts.append(open(wf).read())
        except Exception:
            pass
    return "\n".join(parts), is_error


def score_one(cond_dir: str, idx: int, run: int, rules: dict, patterns: dict):
    """Return (addressed:bool|None, method:str, detail:str). None when idx isn't a gotcha prompt."""
    if idx not in rules and idx not in patterns:
        return None, None, None
    base = _base(cond_dir, idx, run)
    text, is_error = _output_text(base)
    if is_error:
        return None, "error", "transport/timeout error"
    rule = rules.get(idx)
    workflow = _load_workflow(_workflow_path(base))
    if rule and workflow:
        addressed, reason, method = check_rule(workflow, rule, text)
    elif rule and not workflow:
        if rule.get("check_type") != "llm_only":
            return False, "skipped", "no workflow file found; cannot check deterministically"
        addressed, reason, method = check_rule({}, rule, text)
    else:
        _gid, terms = patterns.get(idx, ("?", []))
        if not terms:
            return None, "none", "no rule or pattern"
        rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
        addressed = bool(rx.search(text))
        reason = f"term {'match' if addressed else 'miss'}: {terms[:3]}"
        method = "heuristic"
    return addressed, method, reason


# ---------------------------------------------------------------------------
# Run-level provenance reconstruction
# ---------------------------------------------------------------------------

def detect_backend(meta_rows: list[dict]) -> str:
    for m in meta_rows:
        cm = m.get("cost_model")
        if cm and "deepseek" in cm:
            return "deepseek"
        if cm:
            return cm
    return "claude"  # no cost_model recorded -> Claude/Sonnet path


def gate_from_manifest(manifest) -> str | None:
    """Authoritative gate label from the run manifest's configured gotcha_gate, or None
    when the field is absent/empty (older manifests didn't record it — fall back to the
    transcript scan). Normalizes the recorded values: default-on/on/1 -> 'on',
    default-off/off/0 -> 'off'. The CONFIG is the right label for the experimental
    condition; transcript-marker presence varies per-prompt and is only a fallback."""
    if not manifest:
        return None
    raw = manifest.get("gotcha_gate")
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "off" in s or s == "0":
        return "off"
    if "on" in s or s == "1":
        return "on"
    return None


def detect_gate(run_dir: str) -> str:
    """Fallback gate detection (used only when the manifest has no gotcha_gate): scan
    transcripts for the gate directive. The marker only appears on prompts where a gotcha
    was recalled, so a run can be gate-ON yet have it absent from some prompts — therefore
    scan ALL transcripts (not a [:8] sample, which mislabeled runs whose first transcripts
    were non-gotcha prompts) and return 'on' if the marker appears in ANY of them."""
    transcripts = glob.glob(os.path.join(run_dir, "*", "*.transcript.jsonl"))
    if not transcripts:
        return "unknown"
    for t in transcripts:
        try:
            with open(t, encoding="utf-8", errors="replace") as fh:
                if GATE_MARKER in fh.read():
                    return "on"
        except Exception:
            continue
    return "off"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, dir TEXT, ts TEXT, backend TEXT, model_label TEXT,
  gate TEXT, validator_mode TEXT, validation_enabled INT, conditions TEXT,
  system_prompt_version TEXT, system_prompt_hash TEXT, system_prompt_source TEXT,
  model_timeout_seconds INT, model_timeout_source TEXT, n_timeout INT,
  prompt_idxs TEXT, prompt_count INT, runs_per_prompt INT,
  ground_truth_hash TEXT, gotcha_rules_hash TEXT,
  n_results INT, n_error INT, status TEXT, provenance TEXT, has_manifest INT,
  manifest_json TEXT, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS results (
  run_id TEXT, condition TEXT, prompt_idx INT, run_number INT,
  is_error INT, error_type TEXT, cost_usd REAL, cost_usd_actual REAL, num_turns INT,
  input_tokens INT, total_input_tokens INT, output_tokens INT, time_ms INT,
  validator_calls INT, autofix_fires INT, autofix_changes INT, workflow_filename TEXT,
  validated INT, has_json INT, error_count INT, node_count INT,
  gotcha_prompt INT, gotcha_addressed INT, gotcha_method TEXT, gotcha_detail TEXT,
  scored_rules_hash TEXT,
  PRIMARY KEY (run_id, condition, prompt_idx, run_number)
);
CREATE TABLE IF NOT EXISTS judge_verdicts (
  run_id TEXT, condition TEXT, prompt_idx INT, run_number INT,
  intent_fit TEXT, intent_reasoning TEXT, gotcha_handled TEXT, gotcha_reasoning TEXT,
  confidence TEXT, workflow_source TEXT, judge_model TEXT, judged_at TEXT, judge_error INT,
  PRIMARY KEY (run_id, condition, prompt_idx, run_number)
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_judge_run ON judge_verdicts(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_backend_gate ON runs(backend, gate);
"""


# ---------------------------------------------------------------------------
# Integrity tagging + views — the single source of truth for "what counts".
#
# This runs as a WHOLE-TABLE post-pass on every ingest (even --only), because
# de-duplication and synthetic detection compare runs against each other. The
# rules are deterministic and explicit so a query against v_eligible_runs /
# v_clean_results can never silently re-include garbage:
#   * synthetic — run_id is not a real timestamped run (hand-built aggregates).
#   * duplicate — identical result-content fingerprint as an earlier run (an
#     exact re-ingest of the SAME generations; keep one canonical, flag the rest).
# Comparability filters (system-prompt version, n_timeout) are NOT applied here —
# those are legitimate runs and stay tunable in report.py.
# ---------------------------------------------------------------------------

def ensure_excluded_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    if "excluded_reason" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN excluded_reason TEXT")


def _run_fingerprint(rows) -> str:
    """Order-independent content hash of a run's result rows. None-safe (no cross-type
    comparisons): each row is stringified, then the row-strings are sorted and hashed."""
    row_strs = sorted("|".join("" if v is None else str(v) for v in row) for row in rows)
    return hashlib.sha256("\n".join(row_strs).encode()).hexdigest()


def tag_integrity(conn):
    """Recompute excluded_reason for all runs. Returns (n_synthetic, n_duplicate)."""
    from collections import defaultdict
    cur = conn.cursor()
    cur.execute("UPDATE runs SET excluded_reason=NULL")

    # 1) synthetic: structurally not a real run id.
    synthetic = [rid for (rid,) in cur.execute("SELECT run_id FROM runs") if not TS_RE.match(rid)]
    for rid in synthetic:
        cur.execute("UPDATE runs SET excluded_reason='synthetic' WHERE run_id=?", (rid,))

    # 2) duplicate: identical result fingerprint among the still-eligible runs.
    elig = {rid for (rid,) in cur.execute("SELECT run_id FROM runs WHERE excluded_reason IS NULL")}
    has_manifest = {rid: hm for rid, hm in cur.execute("SELECT run_id, has_manifest FROM runs")}
    by_run = defaultdict(list)
    for row in cur.execute(
        "SELECT run_id, condition, prompt_idx, run_number, is_error, validated, "
        "ROUND(cost_usd,4), num_turns FROM results"
    ):
        by_run[row[0]].append(row[1:])
    groups = defaultdict(list)
    for rid, rows in by_run.items():
        if rid in elig and rows:
            groups[_run_fingerprint(rows)].append(rid)
    n_dup = 0
    for rids in groups.values():
        if len(rids) < 2:
            continue
        # canonical = prefer a manifest run, then the earliest (lexically-smallest) id.
        keep = sorted(rids, key=lambda x: (-(has_manifest.get(x) or 0), x))[0]
        for rid in rids:
            if rid != keep:
                cur.execute("UPDATE runs SET excluded_reason='duplicate' WHERE run_id=?", (rid,))
                n_dup += 1
    return len(synthetic), n_dup


def create_views(conn):
    # The views are the SSOT for "what counts" AND "which number to trust". They expose a
    # canonical `cost` column = COALESCE(cost_usd_actual, cost_usd) so any ad-hoc query gets
    # the backend-correct dollar figure by default (DeepSeek's cost_usd is a Sonnet-priced
    # estimate; the real deepseek-pro figure is cost_usd_actual — Sonnet has no _actual, so
    # COALESCE is right for every backend). Use `cost`, not the raw columns, in reports.
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_eligible_runs;
        CREATE VIEW v_eligible_runs AS
          SELECT * FROM runs WHERE excluded_reason IS NULL;

        DROP VIEW IF EXISTS v_clean_results;
        CREATE VIEW v_clean_results AS
          SELECT re.*,
                 COALESCE(re.cost_usd_actual, re.cost_usd) AS cost,   -- canonical, backend-correct
                 jv.intent_fit,                       -- LLM-judge fitness-for-purpose (pass/fail/NULL)
                 jv.gotcha_handled AS judge_gotcha,
                 jv.confidence     AS judge_confidence
          FROM results re
          JOIN runs r ON r.run_id = re.run_id
          LEFT JOIN judge_verdicts jv
                 ON jv.run_id = re.run_id AND jv.condition = re.condition
                AND jv.prompt_idx = re.prompt_idx AND jv.run_number = re.run_number
          WHERE r.excluded_reason IS NULL AND re.is_error = 0;
        """
    )


def ingest(db_path: str, root: str, only: str | None):
    gt_path = os.path.join(SCRIPT_DIR, "ground_truth.jsonl")
    rules_path = os.path.join(SCRIPT_DIR, "gotcha_rules.jsonl")
    gt_hash, rules_hash = sha8(gt_path), sha8(rules_path)
    rules = load_rules(rules_path)
    patterns = load_gotcha_patterns(gt_path)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()

    run_dirs = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    if only:
        run_dirs = [d for d in run_dirs if os.path.basename(d) == only or only in os.path.basename(d)]

    n_runs = n_rows = 0
    for run_dir in run_dirs:
        run_id = os.path.basename(run_dir)
        metas = glob.glob(os.path.join(run_dir, "*", "*.meta.json"))
        if not metas:
            continue  # not a result-bearing run folder (config-only/empty)

        manifest = jload(os.path.join(run_dir, "run-manifest.json"))
        has_manifest = manifest is not None
        meta_objs = [m for m in (jload(p) for p in metas[:50]) if m]
        backend = detect_backend(meta_objs)
        # Manifest config is authoritative; transcript scan is only a fallback for older
        # runs whose manifest never recorded gotcha_gate.
        gate = gate_from_manifest(manifest) or detect_gate(run_dir)

        conditions, idxs, rows, judge_rows = set(), set(), [], []
        n_error = n_timeout = 0
        seen_timeout_val = None
        for mp in metas:
            mo = RUN_FILE_RE.search(mp)
            if not mo:
                continue
            idx, run_no = int(mo.group(1)), int(mo.group(2))
            cond_dir = os.path.dirname(mp)
            cond = os.path.basename(cond_dir)
            conditions.add(cond)
            idxs.add(idx)
            meta = jload(mp) or {}
            base = mp[: -len(".meta.json")]
            val = jload(base + ".validation.json") or {}
            is_err = 1 if meta.get("is_error") else 0
            n_error += is_err
            err_type = None
            if is_err:
                payload = jload(base + ".json") or {}
                err_type = classify_error_type(payload)  # harness error verbatim, else classified from result text
                if err_type == "model_invocation_timeout":
                    n_timeout += 1
                    if payload.get("timeout_seconds"):
                        seen_timeout_val = payload["timeout_seconds"]
            addressed, method, detail = score_one(cond_dir, idx, run_no, rules, patterns)
            rows.append((
                run_id, cond, idx, run_no, is_err, err_type,
                meta.get("cost_usd"), meta.get("cost_usd_actual"), meta.get("num_turns"),
                meta.get("input_tokens"), meta.get("total_input_tokens"), meta.get("output_tokens"),
                meta.get("time_ms"), meta.get("validator_calls"),
                meta.get("autofix_fires"), meta.get("autofix_changes"), meta.get("workflow_filename"),
                (1 if val.get("valid") else 0) if val else None,
                (1 if val.get("has_json") else 0) if val else None,
                val.get("error_count"), val.get("node_count"),
                1 if (idx in rules or idx in patterns) else 0,
                (1 if addressed else 0) if addressed is not None else None,
                method, detail, rules_hash,
            ))
            # Judge verdict sidecar (post-hoc LLM judge; present only after a judge pass).
            jv = jload(base + ".judge.json")
            if jv:
                judge_rows.append((
                    run_id, cond, idx, run_no,
                    jv.get("intent_fit"), jv.get("intent_reasoning"),
                    jv.get("gotcha_handled"), jv.get("gotcha_reasoning"),
                    jv.get("confidence"), jv.get("workflow_source"),
                    jv.get("judge_model"), jv.get("judged_at"),
                    0 if jv.get("intent_fit") in ("pass", "fail") else 1,
                ))

        runs_per_prompt = (manifest or {}).get("runs") or (max((r[3] for r in rows), default=1))
        sp_version, sp_hash, sp_source = resolve_system_prompt(run_id, manifest)
        # Timeout: manifest value is authoritative; else the value observed on a fired
        # timeout error; else unknown. n_timeout (how many sessions a timeout killed) is
        # the comparability-critical fact — n_timeout=0 means the run was NOT truncated,
        # regardless of the configured value.
        mf_to = (manifest or {}).get("model_timeout_seconds")
        if mf_to is not None:
            to_val, to_src = mf_to, "manifest"
        elif seen_timeout_val is not None:
            to_val, to_src = seen_timeout_val, "observed-from-timeout-error"
        else:
            to_val, to_src = None, "unknown"
        conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, dir, ts, backend, model_label, gate, validator_mode, validation_enabled, "
            "conditions, system_prompt_version, system_prompt_hash, system_prompt_source, "
            "model_timeout_seconds, model_timeout_source, n_timeout, prompt_idxs, prompt_count, "
            "runs_per_prompt, ground_truth_hash, gotcha_rules_hash, n_results, n_error, status, "
            "provenance, has_manifest, manifest_json, ingested_at) "
            "VALUES (" + ",".join("?" * 27) + ")", (
            run_id, run_dir, run_id[:15], backend, (manifest or {}).get("model"),
            gate, (manifest or {}).get("validator_mode"),
            1 if (manifest or {}).get("plugin_validation_enabled", True) else 0,
            json.dumps(sorted(conditions)),
            sp_version, sp_hash, sp_source,
            to_val, to_src, n_timeout,
            json.dumps(sorted(idxs)), len(idxs), runs_per_prompt,
            gt_hash, rules_hash, len(rows), n_error,
            "complete" if rows and n_error < len(rows) else ("empty" if not rows else "all-error"),
            "manifest" if has_manifest else "reconstructed", 1 if has_manifest else 0,
            json.dumps(manifest) if manifest else None, now,
        ))
        conn.execute("DELETE FROM results WHERE run_id=?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO results VALUES (" + ",".join("?" * 26) + ")", rows
        )
        conn.execute("DELETE FROM judge_verdicts WHERE run_id=?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO judge_verdicts VALUES (" + ",".join("?" * 13) + ")", judge_rows
        )
        n_runs += 1
        n_rows += len(rows)

    # Integrity post-pass (whole-table, deterministic) + views — the report SSOT.
    ensure_excluded_column(conn)
    n_syn, n_dup = tag_integrity(conn)
    create_views(conn)
    conn.commit()
    conn.close()
    print(f"Ingested {n_runs} runs, {n_rows} result rows -> {db_path}")
    print(f"Integrity: flagged {n_syn} synthetic + {n_dup} duplicate run(s) "
          f"(excluded from v_eligible_runs / v_clean_results)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(SCRIPT_DIR, "..", "..", "out", "eval", "eval.db"))
    ap.add_argument("--root", default=os.path.join(SCRIPT_DIR, "..", "..", "out", "eval"))
    ap.add_argument("--only", default=None, help="ingest only run dirs matching this substring")
    args = ap.parse_args()
    ingest(os.path.abspath(args.db), os.path.abspath(args.root), args.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
