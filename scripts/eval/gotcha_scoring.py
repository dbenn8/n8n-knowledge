#!/usr/bin/env python3
"""Gotcha-handling scorer for eval results.

The n8n-knowledge plugin's headline value is surfacing known gotchas (real n8n
bugs) so the model DESIGNS AROUND them. Schema validity does NOT capture this —
a workflow can be schema-valid yet walk straight into a known runtime bug.

Two scoring modes:

1. DETERMINISTIC (gotcha_rules.jsonl) — checks the actual workflow JSON:
   - node_swap: the broken node type must be ABSENT and the workaround node PRESENT
   - param_check: specific node parameters must match expected patterns
   - llm_only: cannot be checked deterministically; falls back to term-match heuristic

2. HEURISTIC (ground_truth.jsonl term match) — legacy fallback for prompts without
   a deterministic rule, or as a secondary signal. Matches expected_gotcha terms
   against response text + workflow file content.

Each rule in gotcha_rules.jsonl carries human-readable 'gotcha' and 'workaround'
descriptions so the rules double as documentation and can feed an LLM judge later.
"""

from __future__ import annotations

import glob
import json
import os
import re

GOTCHA_CATEGORIES = ("gotcha_build", "build_with_gotcha")


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def load_rules(rules_path: str) -> dict[int, dict]:
    """Return {prompt_idx -> rule_dict} from gotcha_rules.jsonl."""
    rules: dict[int, dict] = {}
    if not os.path.exists(rules_path):
        return rules
    with open(rules_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rule = json.loads(line)
            rules[rule["prompt_idx"]] = rule
    return rules


# ---------------------------------------------------------------------------
# Legacy: term-match patterns from ground_truth.jsonl
# ---------------------------------------------------------------------------

def load_gotcha_patterns(ground_truth_path: str) -> dict[int, tuple[str, list[str]]]:
    """Return {fileidx -> (prompt_id, [terms])} for gotcha-targeting prompts."""
    patterns: dict[int, tuple[str, list[str]]] = {}
    with open(ground_truth_path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("category") not in GOTCHA_CATEGORIES:
                continue
            raw = e.get("expected_gotcha") or e.get("gotcha_trigger") or e.get("known_issue") or ""
            terms = [t.strip() for t in raw.split("|") if t.strip()]
            if terms:
                patterns[i] = (e.get("id", f"idx{i}"), terms)
    return patterns


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------

def _find_workflow_file(cond_dir: str, fileidx: int) -> str | None:
    """Return the path to the workflow JSON file for a given run, or None."""
    base = os.path.join(cond_dir, f"prompt-{fileidx:03d}-run01")
    wf_candidates = sorted(glob.glob(base + ".workflow/*.json"))
    wf_candidates += [base + ".candidate.workflow.json", base + ".scratch.workflow.json"]
    for wf in wf_candidates:
        if os.path.exists(wf):
            return wf
    return None


def _load_workflow(wf_path: str | None) -> dict | None:
    """Parse and return a workflow JSON dict, or None on failure."""
    if not wf_path:
        return None
    try:
        data = json.load(open(wf_path))
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _output_text_for(cond_dir: str, fileidx: int) -> tuple[str, bool]:
    """Concatenate the model's response text + workflow file content for one run.

    Returns (text, is_error).
    """
    base = os.path.join(cond_dir, f"prompt-{fileidx:03d}-run01")
    response_path = base + ".json"
    if not os.path.exists(response_path):
        return "", True

    text_parts: list[str] = []
    is_error = False
    try:
        payload = json.load(open(response_path))
        is_error = bool(payload.get("is_error") or payload.get("error"))
        text_parts.append(payload.get("result", "") or "")
    except Exception:
        is_error = True

    wf_path = _find_workflow_file(cond_dir, fileidx)
    if wf_path:
        try:
            text_parts.append(open(wf_path).read())
        except Exception:
            pass

    return "\n".join(text_parts), is_error


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

def _extract_node_types(workflow: dict) -> set[str]:
    """Return set of all node type strings in the workflow, normalized."""
    types: set[str] = set()
    for node in workflow.get("nodes", []):
        node_type = node.get("type", "")
        types.add(node_type)
        # Also add a shortened form for matching (models sometimes omit prefixes)
        if node_type.startswith("@n8n/"):
            types.add(node_type[5:])
    return types


def _node_param_text(node: dict) -> str:
    """Serialized node parameters for regex matching."""
    try:
        return json.dumps(node.get("parameters", {}), ensure_ascii=False)
    except Exception:
        return ""


def _check_node_swap(workflow: dict, rule: dict, response_text: str = "") -> tuple[bool, str]:
    """Check that avoided nodes are absent and required nodes are present.

    Param-aware extensions (added after the 2026-06-11 LLM rule review):
    - require_node_param_regex {type -> regex}: a required node only counts
      when its parameters match the regex (e.g. httpRequest must actually
      target Supabase, a Code node must show real slicing/iteration).
    - avoid_param_patterns [{node_type_pattern, param_regex}]: a node is
      "buggy" only when its type matches AND its params match the regex
      (e.g. Merge is unsafe only in positional combine; keyed merge is the
      safe behavior the workaround recommends).
    - warned_ok_terms (regex): buggy node present but the response explicitly
      warns the user about the bug -> counts as addressed (matters when the
      user explicitly asked for the buggy node, e.g. "use Loop Over Items").

    Returns (addressed, reason).
    """
    node_types = _extract_node_types(workflow)
    nodes = workflow.get("nodes", [])

    avoid = rule.get("avoid_node_types", [])
    require = rule.get("require_node_types", [])
    avoid_params = rule.get("avoid_param_patterns", [])
    require_param_regex = rule.get("require_node_param_regex", {})

    avoided_found = [a for a in avoid if a in node_types]

    param_violations = []
    for ap in avoid_params:
        patt = ap.get("node_type_pattern", "").lower()
        rx = re.compile(ap.get("param_regex", ""), re.IGNORECASE)
        for node in nodes:
            if patt and patt not in node.get("type", "").lower():
                continue
            if rx.search(_node_param_text(node)):
                param_violations.append(
                    f"{node.get('name', node.get('type'))} matches /{ap.get('param_regex')}/"
                )

    required_found = []
    for r in require:
        rx_str = require_param_regex.get(r)
        for node in nodes:
            ntype = node.get("type", "")
            if ntype != r and not (ntype.startswith("@n8n/") and ntype[5:] == r):
                continue
            if rx_str and not re.search(rx_str, _node_param_text(node), re.IGNORECASE):
                continue
            required_found.append(r)
            break

    avoid_ok = len(avoided_found) == 0 and len(param_violations) == 0
    require_ok = len(required_found) > 0 if require else True

    if avoid_ok and require_ok:
        parts = []
        if avoid or avoid_params:
            parts.append(f"correctly avoided {avoid or [ap.get('param_regex') for ap in avoid_params]}")
        if require:
            parts.append(f"used workaround {required_found}")
        return True, "; ".join(parts) or "no buggy pattern present"

    # Escape hatch: the buggy node is present, but the user is explicitly
    # warned about the bug in the response/notes.
    warned_terms = rule.get("warned_ok_terms")
    if not avoid_ok and warned_terms and response_text and re.search(
        warned_terms, response_text, re.IGNORECASE
    ):
        return True, "buggy node present but user explicitly warned about the bug"

    parts = []
    if avoided_found:
        parts.append(f"used buggy node(s) {avoided_found}")
    if param_violations:
        parts.append(f"unsafe params: {param_violations}")
    if not require_ok:
        parts.append(f"missing workaround node(s) {require}")
    return False, "; ".join(parts)


def _check_param(workflow: dict, rule: dict) -> tuple[bool, str]:
    """Check parameter-level conditions on the workflow.

    Returns (addressed, reason).
    """
    checks = rule.get("param_checks", [])
    if not checks:
        return True, "no param checks defined"

    all_ok = True
    reasons = []
    for check in checks:
        pattern = check.get("node_type_pattern", "")
        check_name = check.get("check", "")

        if check_name == "explicit_timezone_no_wait":
            # Workflow-level check (not tied to one node): an explicit IANA
            # timezone must be set (workflow settings or a Schedule Trigger
            # node param) AND no Wait node may be present (issue #29160).
            tz = (workflow.get("settings") or {}).get("timezone", "")
            node_tz = [
                n.get("parameters", {}).get("timezone", "")
                for n in workflow.get("nodes", [])
                if "scheduletrigger" in n.get("type", "").lower()
            ]
            tz_ok = bool(tz) or any(node_tz)
            wait_nodes = [
                n.get("name", "?") for n in workflow.get("nodes", [])
                if n.get("type", "") == "n8n-nodes-base.wait"
            ]
            if tz_ok and not wait_nodes:
                src = f"settings.timezone='{tz}'" if tz else f"scheduleTrigger timezone={[t for t in node_tz if t]}"
                reasons.append(f"explicit timezone set ({src}); no Wait node")
            else:
                all_ok = False
                if not tz_ok:
                    reasons.append("no explicit timezone (settings.timezone or Schedule Trigger param)")
                if wait_nodes:
                    reasons.append(f"Wait node present {wait_nodes} (issue #29160 hazard)")
            continue

        matching_nodes = [
            n for n in workflow.get("nodes", [])
            if pattern.lower() in n.get("type", "").lower()
        ]

        if not matching_nodes:
            continue

        if check_name == "node_name_no_single_quotes":
            for node in matching_nodes:
                name = node.get("name", "")
                if "'" in name:
                    all_ok = False
                    reasons.append(f"node '{name}' contains single quote")
                else:
                    reasons.append(f"node '{name}' has safe name (no single quotes)")

        elif check_name == "response_mode_not_immediate":
            for node in matching_nodes:
                params = node.get("parameters", {})
                mode = params.get("responseMode", "")
                if mode in ("responseNode", "lastNode"):
                    reasons.append(f"webhook responseMode='{mode}' (safe)")
                elif mode:
                    all_ok = False
                    reasons.append(f"webhook responseMode='{mode}' (may fire before processing)")
                elif not mode and "trigger" not in node.get("type", "").lower():
                    pass

        elif check_name == "merge_uses_keyed_mode":
            for node in matching_nodes:
                params = node.get("parameters", {})
                mode = params.get("mode", "")
                combo = params.get("combinationMode", "")
                has_key = bool(
                    params.get("mergeByFields")
                    or params.get("fieldsToMatch")
                    or combo == "mergeByFields"
                )
                if has_key:
                    reasons.append(f"merge uses keyed mode ({combo or mode})")
                elif mode == "combine" and not has_key:
                    all_ok = False
                    reasons.append(f"merge uses positional combine (timing hazard)")
                else:
                    reasons.append(f"merge mode='{mode}' (non-positional)")

    return all_ok, "; ".join(reasons) if reasons else "param checks passed"


def check_rule(workflow: dict, rule: dict, response_text: str = "") -> tuple[bool, str, str]:
    """Apply a single rule against a workflow.

    Returns (addressed, reason, method) where method is 'deterministic' or 'heuristic'.
    """
    check_type = rule.get("check_type", "llm_only")

    if check_type == "node_swap":
        addressed, reason = _check_node_swap(workflow, rule, response_text)
        return addressed, reason, "deterministic"

    elif check_type == "param_check":
        addressed, reason = _check_param(workflow, rule)
        return addressed, reason, "deterministic"

    elif check_type == "llm_only":
        # Fall back to term-match heuristic. A rule may carry a curated
        # "heuristic_terms" list (preferred — auto-extracted terms produced
        # false positives, e.g. 'Get Row(s)' matching a mere operation label,
        # and false negatives, e.g. rules whose gotcha text has no quoted
        # strings extract zero terms and can never match).
        key_terms = rule.get("heuristic_terms")
        if not key_terms:
            gotcha_text = rule.get("gotcha", "")
            workaround_text = rule.get("workaround", "")
            key_terms = _extract_heuristic_terms(gotcha_text, workaround_text)
        if key_terms and response_text:
            rx = re.compile("|".join(re.escape(t) for t in key_terms), re.IGNORECASE)
            if rx.search(response_text):
                return True, f"heuristic term match: {key_terms}", "heuristic"
        return False, "no deterministic check available; heuristic miss", "heuristic"

    return False, f"unknown check_type: {check_type}", "unknown"


def _extract_heuristic_terms(gotcha: str, workaround: str) -> list[str]:
    """Extract key technical terms from gotcha/workaround descriptions for heuristic matching."""
    terms: list[str] = []
    # Pull issue numbers
    for m in re.finditer(r"#(\d{4,6})", gotcha):
        terms.append(m.group(0))
    # Pull error messages in quotes
    for m in re.finditer(r"'([^']{5,60})'", gotcha):
        terms.append(m.group(1))
    return terms


# ---------------------------------------------------------------------------
# Top-level scoring
# ---------------------------------------------------------------------------

def score_gotchas(results_dir: str, ground_truth_path: str) -> dict[str, dict]:
    """Per-condition gotcha-addressed counts using deterministic rules + heuristic fallback.

    Returns {cond -> {"addressed": int, "scored": int, "ran": int,
                       "deterministic": int, "heuristic": int, "details": [...]}}
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    gt_stem = os.path.splitext(os.path.basename(ground_truth_path))[0]
    standalone_rules = os.path.join(script_dir, f"{gt_stem}_rules_standalone.jsonl")
    if os.path.exists(standalone_rules):
        rules_path = standalone_rules
    else:
        rules_path = os.path.join(script_dir, "gotcha_rules.jsonl")
    rules = load_rules(rules_path)
    patterns = load_gotcha_patterns(ground_truth_path)

    all_gotcha_idxs = set(patterns.keys()) | set(rules.keys())

    out: dict[str, dict] = {}
    for cond_dir in sorted(glob.glob(os.path.join(results_dir, "*"))):
        if not os.path.isdir(cond_dir):
            continue
        cond = os.path.basename(cond_dir)
        addressed = scored = ran = 0
        n_deterministic = n_heuristic = 0
        details: list[dict] = []

        for fileidx in sorted(all_gotcha_idxs):
            text, is_error = _output_text_for(cond_dir, fileidx)
            base = os.path.join(cond_dir, f"prompt-{fileidx:03d}-run01")
            if not text and is_error and not os.path.exists(base + ".json"):
                continue
            ran += 1
            if is_error:
                details.append({
                    "prompt_idx": fileidx,
                    "result": "error",
                    "reason": "transport/timeout error",
                })
                continue
            scored += 1

            rule = rules.get(fileidx)
            wf_path = _find_workflow_file(cond_dir, fileidx)
            workflow = _load_workflow(wf_path)

            result_addressed = False
            reason = ""
            method = "none"

            if rule and workflow:
                result_addressed, reason, method = check_rule(workflow, rule, text)
            elif rule and not workflow:
                # No workflow file — fall back to heuristic on response text
                if rule.get("check_type") != "llm_only":
                    reason = "no workflow file found; cannot check deterministically"
                    method = "skipped"
                else:
                    result_addressed, reason, method = check_rule({}, rule, text)
            else:
                # No rule — use legacy term-match from ground_truth
                gid, terms = patterns.get(fileidx, ("?", []))
                if terms:
                    rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
                    if rx.search(text):
                        result_addressed = True
                        reason = f"legacy term match: {terms[:3]}"
                        method = "heuristic"
                    else:
                        reason = f"legacy term miss: {terms[:3]}"
                        method = "heuristic"

            if result_addressed:
                addressed += 1
            if method == "deterministic":
                n_deterministic += 1
            elif method == "heuristic":
                n_heuristic += 1

            prompt_id = ""
            if rule:
                prompt_id = rule.get("prompt_id", "")
            elif fileidx in patterns:
                prompt_id = patterns[fileidx][0]

            details.append({
                "prompt_idx": fileidx,
                "prompt_id": prompt_id,
                "result": "addressed" if result_addressed else "missed",
                "method": method,
                "reason": reason,
            })

        if ran:
            out[cond] = {
                "addressed": addressed,
                "scored": scored,
                "ran": ran,
                "deterministic": n_deterministic,
                "heuristic": n_heuristic,
                "details": details,
            }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir> [ground_truth.jsonl] [--details]", file=sys.stderr)
        return 2
    results_dir = sys.argv[1]
    gt = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ground_truth.jsonl"
    )
    show_details = "--details" in sys.argv

    scores = score_gotchas(results_dir, gt)
    if not scores:
        print("No gotcha prompts found in this run.")
        return 0
    print(f"{'CONDITION':<12} {'addressed/scored':>18} {'rate':>8} {'(ran)':>7}  {'determ':>7} {'heuris':>7}")
    for cond in sorted(scores):
        s = scores[cond]
        rate = 100.0 * s["addressed"] / s["scored"] if s["scored"] else 0.0
        frac = f"{s['addressed']}/{s['scored']}"
        print(f"{cond:<12} {frac:>18} {rate:>7.1f}% {s['ran']:>7}  {s['deterministic']:>7} {s['heuristic']:>7}")

    if show_details:
        print("\n--- Per-prompt details ---")
        for cond in sorted(scores):
            print(f"\n  [{cond}]")
            for d in scores[cond].get("details", []):
                status = "OK" if d["result"] == "addressed" else ("ERR" if d["result"] == "error" else "MISS")
                pid = d.get("prompt_id", "")
                print(f"    {d['prompt_idx']:3d} {pid:<24} {status:<5} [{d.get('method','?')}] {d.get('reason','')[:80]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
