#!/usr/bin/env python3
"""Post-hoc LLM judge for eval result directories.

Scores every result in an out/eval/*-v2 dir on two dimensions nothing else
measures: intent fit (does the workflow accomplish the user's request) and
gotcha coverage (does the design avoid the known bug). Verdicts come from
Opus via headless `claude -p` running in an ISOLATED scratch config dir
(no plugins, no hooks, no MCP) with credentials symlinked, never copied.

Spec: docs/superpowers/specs/2026-06-12-llm-judge-design.md
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = "opus"
DEFAULT_CONCURRENCY = 16
SECONDS_PER_CALL_ESTIMATE = 30
PARSE_RETRIES = 2
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFFS = (5, 15, 45)

INTENT_VALUES = ("pass", "fail")
GOTCHA_VALUES = ("pass", "fail", "not_applicable")
CONFIDENCE_VALUES = ("high", "low")

AUTH_ERROR_RE = re.compile(r"401|authenticat|logged in|/login", re.IGNORECASE)
RATE_LIMIT_RE = re.compile(r"429|rate.?limit|overloaded", re.IGNORECASE)


class VerdictParseError(ValueError):
    """The judge's response did not contain a parseable JSON verdict."""


class AuthError(RuntimeError):
    """A judge call failed authentication — the whole pass must halt."""


# ---------------------------------------------------------------------------
# Verdict parsing & validation
# ---------------------------------------------------------------------------

def parse_verdict(text: str) -> dict:
    """Leniently extract a JSON object from the judge's response text."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise VerdictParseError("no JSON object found in response")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise VerdictParseError(f"JSON decode failed: {e}") from e
    if not isinstance(obj, dict):
        raise VerdictParseError("top-level JSON value is not an object")
    return obj


def validate_verdict(v: dict, checklist_mode: bool) -> list[str]:
    """Return a list of problems (empty = valid verdict)."""
    errors: list[str] = []
    if v.get("intent_fit") not in INTENT_VALUES:
        errors.append(f"intent_fit must be one of {INTENT_VALUES}, got {v.get('intent_fit')!r}")
    if not isinstance(v.get("intent_reasoning"), str) or not v.get("intent_reasoning"):
        errors.append("intent_reasoning must be a non-empty string")
    if v.get("gotcha_handled") not in GOTCHA_VALUES:
        errors.append(f"gotcha_handled must be one of {GOTCHA_VALUES}, got {v.get('gotcha_handled')!r}")
    if not isinstance(v.get("gotcha_reasoning"), str) or not v.get("gotcha_reasoning"):
        errors.append("gotcha_reasoning must be a non-empty string")
    if v.get("confidence") not in CONFIDENCE_VALUES:
        errors.append(f"confidence must be one of {CONFIDENCE_VALUES}, got {v.get('confidence')!r}")
    if checklist_mode:
        crits = v.get("criteria")
        if not isinstance(crits, list) or not crits:
            errors.append("criteria must be a non-empty list in checklist mode")
        else:
            for i, c in enumerate(crits):
                if not isinstance(c, dict):
                    errors.append(f"criteria[{i}] must be an object")
                    continue
                if not isinstance(c.get("criterion"), str):
                    errors.append(f"criteria[{i}].criterion must be a string")
                if not isinstance(c.get("met"), bool):
                    errors.append(f"criteria[{i}].met must be a boolean")
    return errors


# ---------------------------------------------------------------------------
# Loaders & artifact gathering
# ---------------------------------------------------------------------------

def load_ground_truth(path: str) -> dict[int, dict]:
    """Return {line_index -> entry} from ground_truth.jsonl."""
    out: dict[int, dict] = {}
    with open(path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if line:
                out[i] = json.loads(line)
    return out


def load_by_prompt_idx(path: str) -> dict[int, dict]:
    """Return {prompt_idx -> entry} from a JSONL file; {} if file absent."""
    out: dict[int, dict] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                e = json.loads(line)
                out[e["prompt_idx"]] = e
    return out


def find_workflow(cond_dir: str, stem: str) -> tuple[str | None, str]:
    """Return (workflow_text, source) with source in validated|candidate|written|missing."""
    for suffix, source in ((".validated.workflow.json", "validated"),
                           (".candidate.workflow.json", "candidate")):
        p = os.path.join(cond_dir, stem + suffix)
        if os.path.exists(p):
            with open(p) as f:
                return f.read(), source
    wdir = os.path.join(cond_dir, stem + ".workflow")
    meta_path = os.path.join(cond_dir, stem + ".meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                named = json.load(f).get("workflow_filename")
        except (json.JSONDecodeError, OSError):
            named = None
        if named and os.path.exists(os.path.join(wdir, named)):
            with open(os.path.join(wdir, named)) as f:
                return f.read(), "written"
    written = sorted(glob.glob(os.path.join(wdir, "*.json")))
    if written:
        with open(written[0]) as f:
            return f.read(), "written"
    return None, "missing"


def load_validation_summary(cond_dir: str, stem: str) -> dict | None:
    """Extract ONLY provenance-free fields from .validation.json.

    The raw file contains enrichment_mode and absolute paths — provenance that
    must never reach the (blinded) judge.
    """
    p = os.path.join(cond_dir, stem + ".validation.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return {"valid": bool(raw.get("valid")),
            "error_count": int(raw.get("error_count") or 0),
            "warning_count": int(raw.get("warning_count") or 0)}


@dataclass
class JudgeInput:
    fileidx: int
    stem: str
    prompt_text: str
    workflow_text: str | None
    workflow_source: str
    validation: dict | None
    gotcha: dict | None
    criteria: dict | None


def gather_input(cond_dir: str, fileidx: int, run: str,
                 ground_truth: dict[int, dict], rules: dict[int, dict],
                 criteria: dict[int, dict]) -> JudgeInput:
    stem = f"prompt-{fileidx:03d}-{run}"
    workflow_text, source = find_workflow(cond_dir, stem)
    gt = ground_truth.get(fileidx, {})
    return JudgeInput(
        fileidx=fileidx,
        stem=stem,
        prompt_text=gt.get("prompt", ""),
        workflow_text=workflow_text,
        workflow_source=source,
        validation=load_validation_summary(cond_dir, stem),
        gotcha=rules.get(fileidx),
        criteria=criteria.get(fileidx),
    )


def discover_results(cond_dir: str) -> list[tuple[int, str]]:
    """Return sorted [(fileidx, stem)] for every meta.json in a condition dir."""
    out = []
    for p in sorted(glob.glob(os.path.join(cond_dir, "prompt-*-run*.meta.json"))):
        stem = os.path.basename(p)[: -len(".meta.json")]
        m = re.match(r"prompt-(\d+)-run\d+$", stem)
        if m:
            out.append((int(m.group(1)), stem))
    return out


# ---------------------------------------------------------------------------
# Judge prompt (BLINDED: no condition/model/path provenance may appear here)
# ---------------------------------------------------------------------------

def build_prompt(ji: JudgeInput) -> str:
    parts: list[str] = []
    parts.append(
        "You are an expert n8n workflow reviewer. Judge the workflow below "
        "strictly on the evidence in its JSON — node types, parameters, "
        "connections. Quote node names/types/params in your reasoning."
    )
    parts.append("## User request\n" + ji.prompt_text)
    parts.append("## Workflow JSON\n" + (ji.workflow_text or "(missing)"))

    if ji.validation is not None:
        v = ji.validation
        status = "schema-valid" if v["valid"] else f"schema-invalid ({v['error_count']} errors)"
        parts.append(
            f"## Validator context\nThe workflow is {status} with "
            f"{v['warning_count']} warning(s). NOTE: schema validity does NOT "
            "imply the workflow accomplishes the request — judge intent fit "
            "independently."
        )
    else:
        parts.append(
            "## Validator context\nNo validator report available. NOTE: schema "
            "validity does NOT imply intent fit — judge independently."
        )

    if ji.gotcha:
        parts.append(
            "## Known gotcha to check\n"
            f"Bug: {ji.gotcha.get('gotcha', '')}\n"
            f"Documented workaround: {ji.gotcha.get('workaround', '')}\n"
            "Set gotcha_handled to pass only if the workflow's design avoids "
            "the bug (e.g. applies the workaround); fail if it walks into it."
        )
        gotcha_instruction = '"gotcha_handled": "pass" or "fail"'
    else:
        gotcha_instruction = '"gotcha_handled": "not_applicable"'

    schema_lines = [
        '"intent_fit": "pass" or "fail"',
        '"intent_reasoning": "<evidence-citing paragraph>"',
        gotcha_instruction,
        '"gotcha_reasoning": "<one sentence>"',
        '"confidence": "high" or "low"',
    ]

    if ji.criteria:
        must = ji.criteria.get("must", [])
        nice = ji.criteria.get("nice", [])
        crit_lines = "\n".join(f"- [must] {c}" for c in must)
        if nice:
            crit_lines += "\n" + "\n".join(f"- [nice] {c}" for c in nice)
        parts.append(
            "## Criteria checklist\nEvaluate each criterion against the "
            "workflow and report it in a criteria array. Set intent_fit to "
            "pass ONLY if every [must] criterion is met; [nice] criteria "
            "never affect intent_fit:\n" + crit_lines
        )
        schema_lines.append('"criteria": [{"criterion": "<the exact criterion text without the [must]/[nice] tag>", "met": true|false}, ...] (one entry per listed criterion)')

    parts.append(
        "## Your verdict\nRespond with a single JSON object and nothing else:\n"
        "{\n  " + ",\n  ".join(schema_lines) + "\n}"
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Isolated scratch config (no plugins/hooks/MCP; credentials SYMLINKED)
# ---------------------------------------------------------------------------

def make_scratch_config(creds_source: str | None = None) -> str:
    """Create an isolated CLAUDE_CONFIG_DIR for judge sessions.

    - mktemp dir (0700): a hard kill orphans at most a dangling symlink,
      never credential bytes.
    - settings.json {} : no plugins, no hooks.
    - empty-mcp.json   : no MCP servers (used with --strict-mcp-config).
    - .credentials.json: SYMLINK to the live file so token rotation is always
      visible (a cp went stale mid-run and 401'd an entire eval arm, 2026-06-12).
    """
    if creds_source is None:
        creds_source = os.path.expanduser("~/.claude/.credentials.json")
    cfg = tempfile.mkdtemp(prefix="judge-config-")
    os.chmod(cfg, 0o700)
    with open(os.path.join(cfg, "settings.json"), "w") as f:
        json.dump({}, f)
    with open(os.path.join(cfg, "empty-mcp.json"), "w") as f:
        json.dump({"mcpServers": {}}, f)
    if os.path.exists(creds_source):
        os.symlink(os.path.realpath(creds_source),
                   os.path.join(cfg, ".credentials.json"))
    return cfg


def cleanup_scratch_config(cfg: str) -> None:
    """Remove the scratch dir without ever following the credentials symlink.

    The claude CLI writes state subdirectories (projects/, sessions/,
    backups/, ...) into its config dir during a pass, so removal must be
    recursive. shutil.rmtree unlinks symlinks rather than descending into
    them, so the live credentials file is never touched.
    """
    shutil.rmtree(cfg)


def build_cmd(model: str, config_dir: str) -> tuple[list[str], dict]:
    cmd = [
        "claude", "-p", "--model", model,
        "--strict-mcp-config",
        "--mcp-config", os.path.join(config_dir, "empty-mcp.json"),
        "--settings", os.path.join(config_dir, "settings.json"),
    ]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    return cmd, env


def run_claude(prompt: str, model: str, config_dir: str) -> str:
    """Real runner: one judge call.

    Raises AuthError on 401-family failures — detected on stderr, or on a
    SHORT stdout error envelope (a verdict-bearing stdout is long, and may
    legitimately discuss authentication). Retries rate-limit errors and
    timeouts with backoff; other failures raise RuntimeError.
    """
    cmd, env = build_cmd(model, config_dir)
    last_err = ""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True,
                                  text=True, env=env, timeout=600)
        except subprocess.TimeoutExpired:
            last_err = "timed out after 600s"
            if attempt < RATE_LIMIT_RETRIES - 1:
                time.sleep(RATE_LIMIT_BACKOFFS[attempt])
                continue
            break
        if proc.returncode == 0:
            return proc.stdout
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        auth_surface = stderr
        if len(stdout.strip()) < 500:
            auth_surface += "\n" + stdout
        if AUTH_ERROR_RE.search(auth_surface):
            raise AuthError(auth_surface.strip()[:300])
        last_err = (stdout + stderr).strip()[:300]
        if RATE_LIMIT_RE.search(stdout + stderr) and attempt < RATE_LIMIT_RETRIES - 1:
            time.sleep(RATE_LIMIT_BACKOFFS[attempt])
            continue
        break
    raise RuntimeError(f"claude call failed: {last_err}")


# ---------------------------------------------------------------------------
# Single-result judgment
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(v: dict, ji: JudgeInput, model: str) -> dict:
    v["judge_model"] = model
    v["workflow_source"] = ji.workflow_source
    v["judged_at"] = _now_iso()
    return v


def _crit_key(s: str) -> str:
    """Normalize criterion text for matching (judge echo may drift in case/whitespace)."""
    return " ".join(s.split()).casefold()


def rollup_checklist(verdict: dict, criteria: dict) -> dict:
    """Recompute intent_fit from the judge's per-criterion calls (mutates in place).

    We trust the judge on each criterion but never on the roll-up:
    pass iff every 'must' criterion is met. 'nice' items are informational.
    Keying is normalized (case/whitespace); unknown criteria are ignored and
    missing ones default to unmet (fail-closed).
    """
    met = {_crit_key(c["criterion"]): c["met"] for c in verdict.get("criteria", [])}
    verdict["intent_fit"] = "pass" if all(met.get(_crit_key(m), False) for m in criteria.get("must", [])) else "fail"
    return verdict


def judge_one(ji: JudgeInput, runner, model: str = DEFAULT_MODEL) -> dict:
    """Judge one result. Raises AuthError / VerdictParseError upward.

    Fail-closed: a missing workflow is a local verdict — no claude call.
    """
    if ji.workflow_text is None:
        return _stamp({
            "intent_fit": "fail",
            "intent_reasoning": "no parseable workflow artifact",
            "gotcha_handled": "fail" if ji.gotcha else "not_applicable",
            "gotcha_reasoning": "no parseable workflow artifact",
            "confidence": "high",
        }, ji, model)

    checklist_mode = ji.criteria is not None
    prompt = build_prompt(ji)
    last_error = ""
    for attempt in range(PARSE_RETRIES + 1):
        text = runner(prompt if attempt == 0 else
                      prompt + f"\n\nYour previous response could not be used "
                      f"(parse/validation error: {last_error}). Respond with "
                      "ONLY the JSON object this time.")
        try:
            verdict = parse_verdict(text)
        except VerdictParseError as e:
            last_error = str(e)
            continue
        problems = validate_verdict(verdict, checklist_mode)
        if problems:
            last_error = "; ".join(problems)
            continue
        if checklist_mode:
            verdict = rollup_checklist(verdict, ji.criteria)
        return _stamp(verdict, ji, model)
    raise VerdictParseError(f"retries exhausted: {last_error}")


# ---------------------------------------------------------------------------
# Pass runner (concurrency, cache, 401 halt) + auth preflight
# ---------------------------------------------------------------------------

def preflight_ok(creds_path: str, n_calls: int, concurrency: int,
                 assume_yes: bool, now_s: float | None = None,
                 confirm=None) -> bool:
    """Warn (and optionally abort) if the OAuth token expires mid-pass."""
    if now_s is None:
        now_s = time.time()
    if confirm is None:
        confirm = lambda msg: input(msg + " [y/N] ").strip().lower() == "y"
    try:
        with open(creds_path) as f:
            expires_ms = json.load(f)["claudeAiOauth"]["expiresAt"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        print("WARNING: could not read credentials expiry - proceeding.", file=sys.stderr)
        return True
    est_s = (n_calls / max(concurrency, 1)) * SECONDS_PER_CALL_ESTIMATE
    remaining_s = expires_ms / 1000 - now_s
    if remaining_s > est_s:
        return True
    msg = (f"WARNING: OAuth token expires in {int(remaining_s)}s but the pass "
           f"is estimated at {int(est_s)}s. Run /login first, or proceed anyway?")
    print(msg, file=sys.stderr)
    return True if assume_yes else confirm("Proceed?")


def run_pass(result_dir: str, conditions: list[str], runner,
             ground_truth_path: str, rules_path: str, criteria_path: str,
             model: str = DEFAULT_MODEL, concurrency: int = DEFAULT_CONCURRENCY,
             force: bool = False, prompts: set[int] | None = None) -> dict:
    """Judge every (cached-miss) result under each condition dir."""
    gt = load_ground_truth(ground_truth_path)
    rules = load_by_prompt_idx(rules_path)
    criteria = load_by_prompt_idx(criteria_path)

    work: list[tuple[str, JudgeInput, str]] = []  # (cond, input, verdict_path)
    stats = {"judged": 0, "skipped": 0, "errors": 0, "halted": False,
             "error_stems": []}
    for cond in conditions:
        cond_dir = os.path.join(result_dir, cond)
        for fileidx, stem in discover_results(cond_dir):
            if prompts is not None and fileidx not in prompts:
                continue
            vpath = os.path.join(cond_dir, stem + ".judge.json")
            if os.path.exists(vpath) and not force:
                stats["skipped"] += 1
                continue
            run = stem.rsplit("-", 1)[-1]  # "run01"
            work.append((cond, gather_input(cond_dir, fileidx, run, gt, rules, criteria), vpath))

    halt = threading.Event()

    def do_one(item):
        cond, ji, vpath = item
        # Halt is best-effort: items already past this gate finish and write valid verdicts.
        if halt.is_set():
            return ("halted", ji.stem)
        try:
            verdict = judge_one(ji, runner, model=model)
        except AuthError:
            halt.set()
            return ("auth", ji.stem)
        except Exception as e:  # parse exhaustion, claude failures
            return ("error", f"{cond}/{ji.stem}: {e}")
        tmp = vpath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(verdict, f, indent=2)
        os.replace(tmp, vpath)
        return ("ok", ji.stem)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for status, detail in pool.map(do_one, work):
            if status == "ok":
                stats["judged"] += 1
            elif status == "error":
                stats["errors"] += 1
                stats["error_stems"].append(detail)
            elif status == "auth":
                stats["halted"] = True
    if stats["halted"]:
        print("AUTH ERROR: pass halted (no refresh attempted - rerun after "
              "/login; completed verdicts are cached).", file=sys.stderr)
    return stats


# ---------------------------------------------------------------------------
# Aggregation & CLI
# ---------------------------------------------------------------------------

def aggregate(result_dir: str, conditions: list[str]) -> dict:
    """Read all .judge.json files, compute per-condition stats, write summary."""
    summary = {"generated_at": _now_iso(), "conditions": {}}
    for cond in conditions:
        cond_dir = os.path.join(result_dir, cond)
        judged = skipped = malformed = 0
        intent_pass = gotcha_pass = gotcha_fail = gotcha_na = 0
        fails: list[dict] = []
        for fileidx, stem in discover_results(cond_dir):
            vpath = os.path.join(cond_dir, stem + ".judge.json")
            if not os.path.exists(vpath):
                skipped += 1
                continue
            try:
                with open(vpath) as f:
                    v = json.load(f)
                intent = v["intent_fit"]
                g = v["gotcha_handled"]
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                malformed += 1
                continue
            judged += 1
            if intent == "pass":
                intent_pass += 1
            else:
                fails.append({"stem": stem, "dimension": "intent",
                              "reason": v.get("intent_reasoning", "")[:200]})
            if g == "pass":
                gotcha_pass += 1
            elif g == "fail":
                gotcha_fail += 1
                fails.append({"stem": stem, "dimension": "gotcha",
                              "reason": v.get("gotcha_reasoning", "")[:200]})
            else:
                gotcha_na += 1
        applicable = gotcha_pass + gotcha_fail
        summary["conditions"][cond] = {
            "judged": judged,
            "unjudged": skipped,
            "malformed": malformed,
            "intent_fit_pct": round(100 * intent_pass / judged, 1) if judged else None,
            "gotcha_applicable": applicable,
            "gotcha_handled_pct": round(100 * gotcha_pass / applicable, 1) if applicable else None,
            "gotcha_na": gotcha_na,
            "fails": fails,
        }
    out = os.path.join(result_dir, "judge-summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def format_table(summary: dict) -> str:
    conds = list(summary["conditions"].keys())
    w = max([9] + [len(c) for c in conds])
    lines = [f"{'condition':<{w}} {'judged':>6} {'intent fit':>10} "
             f"{'gotcha ok':>9} {'(n/a)':>5} {'unjudged':>8}"]
    for cond, c in summary["conditions"].items():
        intent = f"{c['intent_fit_pct']}%" if c["intent_fit_pct"] is not None else "-"
        gotcha = f"{c['gotcha_handled_pct']}%" if c["gotcha_handled_pct"] is not None else "-"
        lines.append(f"{cond:<{w}} {c['judged']:>6} {intent:>10} "
                     f"{gotcha:>9} {c['gotcha_na']:>5} {c['unjudged']:>8}")
    malformed_total = sum(c.get("malformed", 0) for c in summary["conditions"].values())
    if malformed_total:
        lines.append(f"\nMALFORMED VERDICTS: {malformed_total}")
    fails = [(cond, f) for cond, c in summary["conditions"].items() for f in c["fails"]]
    if fails:
        lines.append("\nFAILS:")
        for cond, f in fails:
            lines.append(f"  [{cond}] {f['stem']} ({f['dimension']}): {f['reason']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Post-hoc LLM judge for eval result dirs")
    ap.add_argument("result_dir")
    ap.add_argument("--conditions", help="comma-separated; default: all subdirs")
    ap.add_argument("--prompts", help="comma-separated fileidx filter")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip preflight confirm")
    ap.add_argument("--ground-truth", default=os.path.join(SCRIPT_DIR, "ground_truth.jsonl"))
    ap.add_argument("--rules", default=os.path.join(SCRIPT_DIR, "gotcha_rules.jsonl"))
    ap.add_argument("--criteria", default=os.path.join(SCRIPT_DIR, "judge_criteria.jsonl"))
    args = ap.parse_args(argv)

    if args.conditions:
        conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    else:
        conditions = sorted(d for d in os.listdir(args.result_dir)
                            if os.path.isdir(os.path.join(args.result_dir, d)))
    try:
        prompts = ({int(p) for p in args.prompts.split(",") if p.strip()}
                   if args.prompts else None)
    except ValueError:
        ap.error("--prompts must be a comma-separated list of integers")

    if args.dry_run:
        gt = load_ground_truth(args.ground_truth)
        rules = load_by_prompt_idx(args.rules)
        criteria = load_by_prompt_idx(args.criteria)
        for cond in conditions:
            cond_dir = os.path.join(args.result_dir, cond)
            for fileidx, stem in discover_results(cond_dir):
                if prompts is not None and fileidx not in prompts:
                    continue
                run = stem.rsplit("-", 1)[-1]
                ji = gather_input(cond_dir, fileidx, run, gt, rules, criteria)
                print(f"=== {cond}/{stem} ===")
                print(build_prompt(ji))
        return 0

    n_calls = 0
    for c in conditions:
        cond_dir = os.path.join(args.result_dir, c)
        for fileidx, stem in discover_results(cond_dir):
            if prompts is not None and fileidx not in prompts:
                continue
            if not args.force and os.path.exists(os.path.join(cond_dir, stem + ".judge.json")):
                continue
            n_calls += 1
    creds = os.path.expanduser("~/.claude/.credentials.json")
    if not preflight_ok(creds, n_calls, args.concurrency, assume_yes=args.yes):
        return 1

    cfg = make_scratch_config()
    try:
        runner = lambda prompt: run_claude(prompt, args.model, cfg)
        stats = run_pass(args.result_dir, conditions, runner,
                         ground_truth_path=args.ground_truth,
                         rules_path=args.rules, criteria_path=args.criteria,
                         model=args.model, concurrency=args.concurrency,
                         force=args.force, prompts=prompts)
    finally:
        cleanup_scratch_config(cfg)

    print(f"judged={stats['judged']} skipped={stats['skipped']} "
          f"errors={stats['errors']} halted={stats['halted']}")
    for e in stats["error_stems"]:
        print(f"  ERROR {e}", file=sys.stderr)
    print(format_table(aggregate(args.result_dir, conditions)))
    return 2 if stats["halted"] else 0


if __name__ == "__main__":
    sys.exit(main())
