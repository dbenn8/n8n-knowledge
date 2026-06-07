#!/usr/bin/env python3
"""Statistical analysis for eval results.

Computes paired bootstrap confidence intervals, per-condition aggregates,
and generates the comparison report.

Usage:
    python3 scripts/eval/analyze.py out/eval/<run-dir>
    python3 scripts/eval/analyze.py out/eval/<run-dir> --bootstrap 2000
"""
import json
import glob
import os
import sys
import random
from collections import defaultdict

def load_results(results_dir):
    """Load all meta files from an eval run."""
    data = defaultdict(lambda: defaultdict(list))
    for meta_file in sorted(glob.glob(os.path.join(results_dir, "*", "*.meta.json"))):
        try:
            m = json.load(open(meta_file))
            cond = m.get("condition", "?")
            idx = m.get("prompt_idx", -1)
            data[cond][idx].append(m)
        except:
            pass
    return data


def paired_bootstrap_ci(values_a, values_b, metric, n_bootstrap=2000, ci=0.95):
    """Compute paired bootstrap CI for the difference (A - B).

    values_a, values_b: lists of per-prompt means (one per prompt).
    Returns: (mean_diff, ci_low, ci_high, p_significant)
    """
    n = min(len(values_a), len(values_b))
    if n == 0:
        return 0, 0, 0, False

    diffs = [values_a[i] - values_b[i] for i in range(n)]
    observed_diff = sum(diffs) / n

    boot_diffs = []
    for _ in range(n_bootstrap):
        sample = [diffs[random.randint(0, n - 1)] for _ in range(n)]
        boot_diffs.append(sum(sample) / n)

    boot_diffs.sort()
    alpha = (1 - ci) / 2
    ci_low = boot_diffs[int(alpha * n_bootstrap)]
    ci_high = boot_diffs[int((1 - alpha) * n_bootstrap)]

    significant = (ci_low > 0) or (ci_high < 0)

    return observed_diff, ci_low, ci_high, significant


def analyze(results_dir, n_bootstrap=2000):
    data = load_results(results_dir)
    if not data:
        print("No results found")
        return

    conditions = sorted(data.keys())
    n_prompts = max(max(d.keys()) for d in data.values()) + 1

    print(f"Eval Analysis: {results_dir}")
    print(f"Conditions: {conditions}")
    print(f"Prompts: {n_prompts}")
    print(f"Bootstrap iterations: {n_bootstrap}")
    print()

    # Per-condition aggregates (mean of per-prompt means)
    metrics = ["cost_usd", "time_ms", "num_turns", "output_tokens", "response_chars"]
    labels = {"cost_usd": "Cost ($)", "time_ms": "Time (ms)", "num_turns": "Turns",
              "output_tokens": "Output tokens", "response_chars": "Response (chars)"}

    # Compute per-prompt means for each condition
    prompt_means = {}
    for cond in conditions:
        prompt_means[cond] = {}
        for metric in metrics:
            means = []
            for idx in range(n_prompts):
                runs = data[cond].get(idx, [])
                vals = [r.get(metric, 0) for r in runs if metric in r]
                means.append(sum(vals) / max(len(vals), 1) if vals else 0)
            prompt_means[cond][metric] = means

    # Summary table
    print(f"{'METRIC':<20}", end="")
    for c in conditions:
        print(f" {c + ' (mean)':>15} {c + ' (std)':>12}", end="")
    print()
    print("-" * (20 + 28 * len(conditions)))

    for metric in metrics:
        label = labels.get(metric, metric)
        print(f"{label:<20}", end="")
        for c in conditions:
            vals = prompt_means[c][metric]
            mean = sum(vals) / len(vals)
            std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
            if metric == "cost_usd":
                print(f" ${mean:>14.4f} ${std:>11.4f}", end="")
            elif metric == "time_ms":
                print(f" {mean:>14.0f}ms {std:>10.0f}ms", end="")
            else:
                print(f" {mean:>15.1f} {std:>12.1f}", end="")
        print()

    # Pairwise bootstrap comparisons
    if len(conditions) >= 2:
        print()
        print("=" * 90)
        print("PAIRWISE COMPARISONS (paired bootstrap, 95% CI)")
        print("=" * 90)

        for i, c1 in enumerate(conditions):
            for c2 in conditions[i + 1:]:
                print(f"\n--- {c1} vs {c2} ---")
                print(f"{'Metric':<20} {'Diff ('+c1+'-'+c2+')':>20} {'95% CI':>25} {'Sig?':>6}")
                print("-" * 75)

                for metric in metrics:
                    label = labels.get(metric, metric)
                    vals_1 = prompt_means[c1][metric]
                    vals_2 = prompt_means[c2][metric]

                    diff, ci_low, ci_high, sig = paired_bootstrap_ci(
                        vals_1, vals_2, metric, n_bootstrap
                    )

                    sig_str = "YES *" if sig else "no"

                    if metric == "cost_usd":
                        print(f"{label:<20} ${diff:>19.4f} [${ci_low:.4f}, ${ci_high:.4f}] {sig_str:>6}")
                    elif metric == "time_ms":
                        print(f"{label:<20} {diff:>18.0f}ms [{ci_low:.0f}ms, {ci_high:.0f}ms] {sig_str:>6}")
                    else:
                        print(f"{label:<20} {diff:>20.2f} [{ci_low:.2f}, {ci_high:.2f}] {sig_str:>6}")

    # Per-prompt detail (means across runs)
    print()
    print("=" * 90)
    print("PER-PROMPT MEANS")
    print("=" * 90)
    header = f"{'#':<4}"
    for c in conditions:
        header += f" {c+'-cost':>10} {c+'-ms':>8} {c+'-trn':>6}"
    print(header)
    print("-" * len(header))

    for idx in range(n_prompts):
        line = f"{idx+1:<4}"
        for c in conditions:
            runs = data[c].get(idx, [])
            cost = sum(r.get("cost_usd", 0) for r in runs) / max(len(runs), 1)
            time = sum(r.get("time_ms", 0) for r in runs) / max(len(runs), 1)
            turns = sum(r.get("num_turns", 0) for r in runs) / max(len(runs), 1)
            line += f" ${cost:>9.3f} {time:>7.0f}ms {turns:>5.1f}"
        print(line)

    # Total cost
    print()
    print(f"{'Total cost':<20}", end="")
    for c in conditions:
        total = sum(r.get("cost_usd", 0) for runs in data[c].values() for r in runs)
        n_runs = sum(len(runs) for runs in data[c].values())
        print(f" ${total:>10.2f} ({n_runs} runs)", end="")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results-dir> [--bootstrap N]")
        sys.exit(1)

    results_dir = sys.argv[1]
    n_bootstrap = 2000
    if "--bootstrap" in sys.argv:
        idx = sys.argv.index("--bootstrap")
        n_bootstrap = int(sys.argv[idx + 1])

    random.seed(42)
    analyze(results_dir, n_bootstrap)
