#!/usr/bin/env python3
"""Post-hoc n8n workflow JSON validator for eval results.

Uses n8n-mcp's full validation engine (MIT licensed) as the yardstick.
Extracts workflow JSON from Claude responses, pipes through n8n-mcp's
WorkflowValidator, and aggregates results per condition.

Usage:
    python3 scripts/eval/validate_workflow.py out/eval/<run-dir>
    python3 scripts/eval/validate_workflow.py out/eval/<run-dir> --details
"""
import json
import glob
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VALIDATOR_JS = os.path.join(SCRIPT_DIR, "validate-with-mcp.js")


def extract_workflow_json(response_text):
    """Extract n8n workflow JSON from a Claude response.

    Looks for ```json code blocks containing workflow structure,
    then falls back to bare JSON object detection.
    """
    json_blocks = re.findall(r'```(?:json)?\s*\n(.*?)```', response_text, re.DOTALL)

    for block in json_blocks:
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
                return obj, None
        except json.JSONDecodeError:
            continue

    brace_starts = [m.start() for m in re.finditer(r'\{', response_text)]
    for start in brace_starts:
        try:
            candidate = response_text[start:]
            depth = 0
            end = 0
            for i, ch in enumerate(candidate):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            snippet = candidate[:end]
            if len(snippet) > 200:
                obj = json.loads(snippet)
                if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
                    return obj, None
        except (json.JSONDecodeError, ValueError):
            continue

    return None, "no_json_found"


def validate_with_mcp(workflow_json):
    """Run n8n-mcp's full validator on a workflow JSON object."""
    try:
        proc = subprocess.run(
            ["node", VALIDATOR_JS],
            input=json.dumps(workflow_json),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return {
                "valid": False,
                "error_count": 1,
                "warning_count": 0,
                "errors": [{"type": "validator_crash", "message": proc.stderr[:200]}],
                "warnings": [],
                "statistics": {},
            }
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [{"type": "timeout", "message": "Validator timed out after 15s"}],
            "warnings": [],
            "statistics": {},
        }
    except Exception as e:
        return {
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [{"type": "error", "message": str(e)}],
            "warnings": [],
            "statistics": {},
        }


def analyze_results(results_dir, show_details=False):
    """Analyze all eval results for workflow validity using n8n-mcp validator."""
    conditions = {}

    for cond_dir in sorted(glob.glob(os.path.join(results_dir, "*"))):
        if not os.path.isdir(cond_dir):
            continue
        cond = os.path.basename(cond_dir)
        if cond.endswith(".json"):
            continue

        results = []
        response_files = sorted(glob.glob(os.path.join(cond_dir, "*.json")))
        response_files = [f for f in response_files if not f.endswith(".meta.json")]

        for response_file in response_files:
            try:
                data = json.load(open(response_file))
                response = data.get("result", "")
                prompt_match = re.search(r'prompt-(\d+)', os.path.basename(response_file))
                prompt_idx = int(prompt_match.group(1)) if prompt_match else -1

                workflow, extract_err = extract_workflow_json(response)
                if workflow is None:
                    results.append({
                        "file": os.path.basename(response_file),
                        "prompt_idx": prompt_idx,
                        "has_json": False,
                        "passed": False,
                        "error_count": 1,
                        "warning_count": 0,
                        "errors": [extract_err or "no_json"],
                        "warnings": [],
                        "node_count": 0,
                    })
                else:
                    validation = validate_with_mcp(workflow)
                    results.append({
                        "file": os.path.basename(response_file),
                        "prompt_idx": prompt_idx,
                        "has_json": True,
                        "passed": validation["valid"],
                        "error_count": validation["error_count"],
                        "warning_count": validation["warning_count"],
                        "errors": [e["message"][:80] for e in validation.get("errors", [])],
                        "warnings": [w["message"][:80] for w in validation.get("warnings", [])],
                        "node_count": validation.get("statistics", {}).get("totalNodes", 0),
                        "trigger_count": validation.get("statistics", {}).get("triggerNodes", 0),
                    })
            except Exception as e:
                results.append({
                    "file": os.path.basename(response_file),
                    "prompt_idx": -1,
                    "has_json": False,
                    "passed": False,
                    "error_count": 1,
                    "warning_count": 0,
                    "errors": [f"parse_error:{str(e)[:60]}"],
                    "warnings": [],
                    "node_count": 0,
                })

        conditions[cond] = results

    print(f"Workflow Validation (n8n-mcp engine): {results_dir}")
    print()

    print(f"{'CONDITION':<12} {'TOTAL':>6} {'HAS_JSON':>10} {'VALID':>8} {'RATE':>8} {'AVG_ERR':>8} {'AVG_WARN':>9}")
    print("-" * 65)
    for cond, results in sorted(conditions.items()):
        total = len(results)
        has_json = sum(1 for r in results if r["has_json"])
        valid = sum(1 for r in results if r["passed"])
        rate = f"{valid/total*100:.1f}%" if total > 0 else "N/A"
        json_rate = f"({has_json}/{total})"
        avg_err = sum(r["error_count"] for r in results) / max(total, 1)
        avg_warn = sum(r["warning_count"] for r in results) / max(total, 1)
        print(f"{cond:<12} {total:>6} {has_json:>10} {valid:>8} {rate:>8} {avg_err:>8.1f} {avg_warn:>9.1f}")

    if show_details:
        print()
        print("=" * 90)
        print("FAILURES (first 20 per condition)")
        print("=" * 90)
        for cond, results in sorted(conditions.items()):
            failures = [r for r in results if not r["passed"]]
            if failures:
                print(f"\n--- {cond} ({len(failures)} failures) ---")
                for r in failures[:20]:
                    errs = "; ".join(r["errors"][:2])
                    print(f"  p{r['prompt_idx']:>3} [{r['node_count']}n]: {errs}")

    print()
    error_types = {}
    for cond, results in conditions.items():
        for r in results:
            for err in r["errors"]:
                err_short = err.split(".")[0][:30] if "." in err else err[:30]
                error_types.setdefault(cond, {}).setdefault(err_short, 0)
                error_types[cond][err_short] += 1

    if error_types:
        print("TOP ERROR TYPES")
        print("-" * 60)
        for cond in sorted(error_types.keys()):
            top = sorted(error_types[cond].items(), key=lambda x: -x[1])[:5]
            print(f"  {cond}: {', '.join(f'{k}({v})' for k,v in top)}")

    out_file = os.path.join(results_dir, "validation_results.json")
    with open(out_file, "w") as f:
        json.dump(conditions, f, indent=2)
    print(f"\nDetailed results: {out_file}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results-dir> [--details]")
        sys.exit(1)

    results_dir = sys.argv[1]
    details = "--details" in sys.argv
    analyze_results(results_dir, details)
