#!/usr/bin/env python3
"""Summarize eval result metadata for a results directory."""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict

from gotcha_scoring import score_gotchas


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <results_dir>", file=sys.stderr)
        return 2

    results_dir = sys.argv[1]
    gt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ground_truth.jsonl")
    try:
        gotcha_scores = score_gotchas(results_dir, gt_path)
    except Exception:
        gotcha_scores = {}
    data: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for meta_file in sorted(glob.glob(os.path.join(results_dir, "*", "*.meta.json"))):
        try:
            meta = json.load(open(meta_file))
            cond = meta.get("condition", "?")
            idx = meta.get("prompt_idx", -1)
            validation_file = meta_file.replace(".meta.json", ".validation.json")
            validation = {}
            if os.path.exists(validation_file):
                try:
                    validation = json.load(open(validation_file))
                except Exception:
                    validation = {}
            meta["_validation"] = validation
            data[cond][idx].append(meta)
        except Exception:
            continue

    if not data:
        print("No results found")
        return 1

    conditions = sorted(data.keys())
    has_both = "mcp" in conditions and "plugin" in conditions

    def _all_runs(cond):
        return [meta for runs in data[cond].values() for meta in runs]

    def _compute(cond):
        all_runs = _all_runs(cond)
        all_vals = lambda k: [m[k] for m in all_runs if k in m]

        def _median(vals):
            vals = sorted(vals)
            n = len(vals)
            if not n:
                return 0
            mid = n // 2
            return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2

        validated = sum(1 for m in all_runs if m.get("_validation", {}).get("valid") is True)
        invalid = sum(1 for m in all_runs if m.get("_validation", {}).get("valid") is False)
        total = len(all_runs)
        err_vals = all_vals("is_error")
        error_rate = sum(1 for v in err_vals if v) / max(len(err_vals), 1) * 100
        cost_vals = all_vals("cost_usd")
        avg_cost = sum(cost_vals) / max(len(cost_vals), 1)
        time_vals = all_vals("time_ms")
        avg_time = sum(time_vals) / max(len(time_vals), 1)
        turn_vals = all_vals("num_turns")
        avg_turns = sum(turn_vals) / max(len(turn_vals), 1)
        input_vals = all_vals("total_input_tokens")
        avg_input = sum(input_vals) / max(len(input_vals), 1)
        output_vals = all_vals("output_tokens")
        avg_output = sum(output_vals) / max(len(output_vals), 1)
        resp_vals = all_vals("response_chars")
        avg_resp = sum(resp_vals) / max(len(resp_vals), 1)
        wrote = sum(1 for m in all_runs if m.get("workflow_filename"))
        autofix_fired = sum(1 for m in all_runs if (m.get("autofix_fires") or 0) > 0)
        autofix_vals = all_vals("autofix_changes")
        avg_autofix = sum(autofix_vals) / max(len(autofix_vals), 1)
        validator_vals = [
            m["validator_calls"] for m in all_runs
            if isinstance(m.get("validator_calls"), (int, float))
        ]
        avg_validator_calls = (
            sum(validator_vals) / len(validator_vals) if validator_vals else None
        )
        actual_vals = [
            m["cost_usd_actual"] for m in all_runs
            if isinstance(m.get("cost_usd_actual"), (int, float))
        ]
        avg_cost_actual = sum(actual_vals) / len(actual_vals) if actual_vals else None
        total_cost_actual = sum(actual_vals) if actual_vals else None

        g = gotcha_scores.get(cond) or {}
        gotcha_addr = g.get("addressed", 0)
        gotcha_scored = g.get("scored", 0)
        gotcha_rate = (100.0 * gotcha_addr / gotcha_scored) if gotcha_scored else 0.0

        med_cost_actual = _median(actual_vals) if actual_vals else None

        return {
            "med_cost": _median(cost_vals),
            "med_cost_actual": med_cost_actual,
            "med_time": _median(time_vals),
            "med_turns": _median(turn_vals),
            "med_input": _median(input_vals),
            "validated_rate": validated / max(total, 1) * 100,
            "validated_runs": validated,
            "invalid_runs": invalid,
            "cost_usd": avg_cost,
            "time_ms": avg_time,
            "num_turns": avg_turns,
            "total_input_tokens": avg_input,
            "output_tokens": avg_output,
            "response_chars": avg_resp,
            "is_error": error_rate,
            "wrote_file_runs": wrote,
            "autofix_runs": autofix_fired,
            "autofix_changes": avg_autofix,
            "validator_calls": avg_validator_calls,
            "cost_usd_actual": avg_cost_actual,
            "total_cost_actual": total_cost_actual,
            "gotcha_addressed_frac": f"{gotcha_addr}/{gotcha_scored}",
            "gotcha_rate": gotcha_rate,
            "total_runs": total,
            "total_cost": sum(cost_vals),
        }

    computed = {cond: _compute(cond) for cond in conditions}

    def _delta_str(plugin_val, mcp_val, mode="pp", lower_better=False):
        """Return a delta annotation like (+6.6pp) or (-40%)."""
        if mcp_val == 0:
            return ""
        if mode == "pp":
            d = plugin_val - mcp_val
            sign = "+" if d >= 0 else ""
            return f" ({sign}{d:.1f}pp)"
        elif mode == "pct":
            d = 100 * (plugin_val - mcp_val) / abs(mcp_val)
            sign = "+" if d >= 0 else ""
            return f" ({sign}{d:.0f}%)"
        return ""

    col_width = 15
    if has_both:
        col_width = 20

    print(f"{'METRIC':<25}", end="")
    for cond in conditions:
        print(f" {cond:>{col_width}}", end="")
    print()
    print("-" * (25 + (col_width + 1) * len(conditions)))

    metrics = [
        ("Validated rate", "validated_rate", "pp", False),
        ("Validated runs", "validated_runs", None, False),
        ("Invalid runs", "invalid_runs", None, False),
        ("Avg cost ($)", "cost_usd", "pct", True),
        ("Med cost ($)", "med_cost", "pct", True),
        ("Avg cost actual ($)", "cost_usd_actual", "pct", True),
        ("Med cost actual ($)", "med_cost_actual", "pct", True),
        ("Avg time (ms)", "time_ms", "pct", True),
        ("Med time (ms)", "med_time", "pct", True),
        ("Avg turns", "num_turns", "pct", True),
        ("Med turns", "med_turns", "pct", True),
        ("Avg total input tok", "total_input_tokens", "pct", True),
        ("Med total input tok", "med_input", "pct", True),
        ("Avg output tokens", "output_tokens", None, False),
        ("Avg response (chars)", "response_chars", None, False),
        ("Error rate", "is_error", "pp", True),
        ("Wrote-file runs", "wrote_file_runs", None, False),
        ("Autofix runs", "autofix_runs", None, False),
        ("Avg autofix fixes", "autofix_changes", None, False),
        ("Avg validator calls", "validator_calls", None, False),
        ("Gotcha-addressed", "gotcha_addressed_frac", None, False),
        ("Gotcha-addr rate", "gotcha_rate", "pp", False),
    ]

    for label, key, delta_mode, lower_better in metrics:
        print(f"{label:<25}", end="")
        for cond in conditions:
            v = computed[cond]
            raw = v[key]

            delta = ""
            if has_both and cond == "plugin" and delta_mode and "mcp" in computed:
                mcp_val = computed["mcp"][key]
                if isinstance(raw, (int, float)) and isinstance(mcp_val, (int, float)):
                    delta = _delta_str(raw, mcp_val, delta_mode, lower_better)

            if key in ("cost_usd_actual", "med_cost_actual"):
                if raw is None:
                    cell = f"{'—':>14}"
                    delta = ""
                else:
                    cell = f"${raw:>13.4f}"
            elif key == "validated_rate":
                cell = f"{raw:>13.1f}%"
            elif key == "is_error":
                cell = f"{raw:>13.1f}%"
            elif key == "gotcha_rate":
                cell = f"{raw:>13.1f}%"
            elif key == "gotcha_addressed_frac":
                cell = f"{raw:>14}"
            elif key in ("cost_usd", "med_cost"):
                cell = f"${raw:>13.3f}"
            elif key in ("time_ms", "med_time"):
                cell = f"{raw:>12.0f}ms"
            elif key == "validator_calls":
                cell = f"{'—':>14}" if raw is None else f"{raw:>14.1f}"
            elif key in ("validated_runs", "invalid_runs", "wrote_file_runs", "autofix_runs"):
                cell = f"{raw:>14}"
            else:
                cell = f"{raw:>14.1f}"

            combined = cell + delta
            print(f" {combined:>{col_width}}", end="")
        print()

    print("-" * (25 + (col_width + 1) * len(conditions)))

    # Total runs
    print(f"{'Total runs':<25}", end="")
    for cond in conditions:
        print(f" {computed[cond]['total_runs']:>{col_width}}", end="")
    print()

    # Total cost
    print(f"{'Total cost ($)':<25}", end="")
    for cond in conditions:
        v = computed[cond]["total_cost"]
        delta = ""
        if has_both and cond == "plugin" and "mcp" in computed:
            delta = _delta_str(v, computed["mcp"]["total_cost"], "pct", True)
        cell = f"${v:>13.2f}" + delta
        print(f" {cell:>{col_width}}", end="")
    print()

    # Total actual provider cost (present when runs carry cost_usd_actual,
    # i.e. the backend wrapper exported EVAL_COST_MODEL for repricing)
    if any(computed[c]["total_cost_actual"] is not None for c in conditions):
        print(f"{'Total cost actual ($)':<25}", end="")
        for cond in conditions:
            v = computed[cond]["total_cost_actual"]
            if v is None:
                cell = f"{'—':>14}"
            else:
                delta = ""
                if has_both and cond == "plugin" and "mcp" in computed:
                    mv = computed["mcp"]["total_cost_actual"]
                    if mv is not None:
                        delta = _delta_str(v, mv, "pct", True)
                cell = f"${v:>13.2f}" + delta
            print(f" {cell:>{col_width}}", end="")
        print()

    print(f"\nResults: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
