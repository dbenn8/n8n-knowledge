#!/usr/bin/env python3
"""Fail fast when plugin-time validation and scoring use mismatched validators."""

from __future__ import annotations

import json
import os
import sys

from workflow_validation import (
    describe_validator_target,
    resolve_eval_plugin_validator_target,
    resolve_scoring_validator_target,
)
from validator_metadata import compare_validator_descriptors


def main() -> int:
    plugin_validation_enabled = (
        os.environ.get("EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION", "0") == "1"
    )
    allow_mismatch = os.environ.get("EVAL_ALLOW_VALIDATOR_MISMATCH", "0") == "1"

    plugin_target = resolve_eval_plugin_validator_target()
    scoring_target = resolve_scoring_validator_target()
    plugin_descriptor = describe_validator_target(plugin_target)
    scoring_descriptor = describe_validator_target(scoring_target)

    errors: list[str] = []
    mismatches: list[str] = []

    if plugin_validation_enabled:
        if plugin_target.get("effective_mode") is None:
            errors.append(plugin_target.get("reason") or "plugin validator target could not be resolved")
        elif plugin_descriptor.get("status") == "unavailable":
            detail = plugin_descriptor.get("detail")
            errors.append(
                "plugin validator target is unavailable"
                + (f": {detail}" if detail else "")
            )
        elif (
            plugin_target.get("effective_mode") == "cloud"
            and not plugin_descriptor.get("validator_info")
        ):
            errors.append(
                "plugin cloud validator did not return validator_info from its health endpoint"
            )

    if scoring_target.get("effective_mode") is None:
        errors.append(scoring_target.get("reason") or "scoring validator target could not be resolved")
    elif scoring_descriptor.get("status") == "unavailable":
        detail = scoring_descriptor.get("detail")
        errors.append(
            "scoring validator target is unavailable"
            + (f": {detail}" if detail else "")
        )
    elif (
        scoring_target.get("effective_mode") == "cloud"
        and not scoring_descriptor.get("validator_info")
    ):
        errors.append(
            "scoring cloud validator did not return validator_info from its health endpoint"
        )

    if plugin_validation_enabled and not errors:
        mismatches = compare_validator_descriptors(plugin_descriptor, scoring_descriptor)

    summary = {
        "plugin_validation_enabled": plugin_validation_enabled,
        "allow_mismatch": allow_mismatch,
        "plugin_target": plugin_target,
        "plugin_descriptor": plugin_descriptor,
        "scoring_target": scoring_target,
        "scoring_descriptor": scoring_descriptor,
        "mismatches": mismatches,
        "errors": errors,
    }

    print("=== Validator Preflight ===")
    print(json.dumps(summary, indent=2))

    if errors:
        print("\nValidator preflight failed: target resolution/availability error.", file=sys.stderr)
        return 1

    if mismatches and not allow_mismatch:
        print(
            "\nValidator preflight failed: plugin-time validation and scoring would use "
            "different validator versions or node databases.",
            file=sys.stderr,
        )
        print(
            "Set EVAL_ALLOW_VALIDATOR_MISMATCH=1 only if you intentionally want this.",
            file=sys.stderr,
        )
        return 1

    if mismatches:
        print(
            "\nValidator preflight warning: mismatch allowed by "
            "EVAL_ALLOW_VALIDATOR_MISMATCH=1.",
            file=sys.stderr,
        )

    print("\nValidator preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
