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
    written = sorted(glob.glob(os.path.join(cond_dir, stem + ".workflow", "*.json")))
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
