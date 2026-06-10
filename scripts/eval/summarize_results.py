#!/usr/bin/env python3
"""Summarize eval result metadata for a results directory."""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <results_dir>", file=sys.stderr)
        return 2

    results_dir = sys.argv[1]
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

    print(f"{'METRIC':<25}", end="")
    for cond in conditions:
        print(f" {cond:>14}", end="")
    print()
    print("-" * (25 + 15 * len(conditions)))

    metrics = [
        ("Validated rate", "validated_rate"),
        ("Validated runs", "validated_runs"),
        ("Invalid runs", "invalid_runs"),
        ("Avg cost ($)", "cost_usd"),
        ("Avg time (ms)", "time_ms"),
        ("Avg turns", "num_turns"),
        ("Avg total input tok", "total_input_tokens"),
        ("Avg output tokens", "output_tokens"),
        ("Avg response (chars)", "response_chars"),
        ("Error rate", "is_error"),
        ("Wrote-file runs", "wrote_file_runs"),
        ("Autofix runs", "autofix_runs"),
        ("Avg autofix fixes", "autofix_changes"),
    ]

    for label, key in metrics:
        print(f"{label:<25}", end="")
        for cond in conditions:
            all_runs = [
                meta
                for runs in data[cond].values()
                for meta in runs
            ]
            all_vals = [
                meta[key]
                for meta in all_runs
                if key in meta
            ]
            if key == "validated_rate":
                validated = sum(
                    1 for meta in all_runs
                    if meta.get("_validation", {}).get("valid") is True
                )
                val = validated / max(len(all_runs), 1) * 100
                print(f" {val:>13.1f}%", end="")
            elif key == "validated_runs":
                validated = sum(
                    1 for meta in all_runs
                    if meta.get("_validation", {}).get("valid") is True
                )
                print(f" {validated:>14}", end="")
            elif key == "invalid_runs":
                invalid = sum(
                    1 for meta in all_runs
                    if meta.get("_validation", {}).get("valid") is False
                )
                print(f" {invalid:>14}", end="")
            elif key == "is_error":
                val = sum(1 for v in all_vals if v) / max(len(all_vals), 1) * 100
                print(f" {val:>13.1f}%", end="")
            elif key == "wrote_file_runs":
                wrote = sum(
                    1 for meta in all_runs
                    if meta.get("workflow_filename")
                )
                print(f" {wrote:>14}", end="")
            elif key == "autofix_runs":
                fired = sum(
                    1 for meta in all_runs
                    if (meta.get("autofix_fires") or 0) > 0
                )
                print(f" {fired:>14}", end="")
            elif key == "cost_usd":
                val = sum(all_vals) / max(len(all_vals), 1)
                print(f" ${val:>13.3f}", end="")
            elif key == "time_ms":
                val = sum(all_vals) / max(len(all_vals), 1)
                print(f" {val:>12.0f}ms", end="")
            else:
                val = sum(all_vals) / max(len(all_vals), 1)
                print(f" {val:>14.1f}", end="")
        print()

    print("-" * (25 + 15 * len(conditions)))
    print(f"{'Total runs':<25}", end="")
    for cond in conditions:
        total = sum(len(runs) for runs in data[cond].values())
        print(f" {total:>14}", end="")
    print()

    print(f"{'Total cost ($)':<25}", end="")
    for cond in conditions:
        total = sum(
            meta["cost_usd"]
            for runs in data[cond].values()
            for meta in runs
            if "cost_usd" in meta
        )
        print(f" ${total:>13.2f}", end="")
    print()

    print(f"\nResults: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
