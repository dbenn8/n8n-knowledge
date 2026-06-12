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
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    """Remove the scratch dir without ever following the credentials symlink."""
    for name in os.listdir(cfg):
        p = os.path.join(cfg, name)
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
    os.rmdir(cfg)


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
