#!/usr/bin/env python3
"""Inspect an eval response, validate it, and optionally emit a repair prompt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from workflow_validation import build_repair_prompt, inspect_response_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--response-file", required=True)
    parser.add_argument("--enrichment-mode", choices=["basic", "plugin"], default="basic")
    parser.add_argument("--original-prompt-file")
    parser.add_argument("--repair-prompt-file")
    parser.add_argument("--candidate-file")
    parser.add_argument("--validated-file")
    parser.add_argument("--workflow-file", help="Fallback workflow JSON file when response text has no JSON (e.g. plugin scratch file copy)")
    parser.add_argument("--max-errors", type=int, default=8)
    args = parser.parse_args()

    inspection = inspect_response_file(
        args.response_file,
        max_errors=args.max_errors,
        enrichment_mode=args.enrichment_mode,
        workflow_file=args.workflow_file,
    )
    output = {
        "valid": inspection["valid"],
        "has_json": inspection["has_json"],
        "extract_error": inspection["extract_error"],
        "error_count": inspection["validation"].get("error_count", 0),
        "warning_count": inspection["validation"].get("warning_count", 0),
        "node_count": inspection["validation"].get("statistics", {}).get("totalNodes", 0),
        "trigger_count": inspection["validation"].get("statistics", {}).get("triggerNodes", 0),
        "enrichment_mode": args.enrichment_mode,
        "issues": inspection.get("issues", []),
        "repair_messages": inspection["repair_messages"],
        "feedback_block": inspection["feedback_block"],
    }

    workflow = inspection.get("workflow")
    if workflow is not None and args.candidate_file:
        Path(args.candidate_file).write_text(json.dumps(workflow, indent=2))
        output["candidate_file"] = args.candidate_file

    if workflow is not None and inspection["valid"] and args.validated_file:
        Path(args.validated_file).write_text(json.dumps(workflow, indent=2))
        output["validated_file"] = args.validated_file

    if (not inspection["valid"]) and args.repair_prompt_file and args.original_prompt_file:
        original_prompt = Path(args.original_prompt_file).read_text()
        repair_prompt = build_repair_prompt(
            original_prompt,
            inspection.get("response_text", ""),
            inspection,
        )
        Path(args.repair_prompt_file).write_text(repair_prompt)
        output["repair_prompt_file"] = args.repair_prompt_file
        output["repair_prompt"] = repair_prompt

    print(json.dumps(output))


if __name__ == "__main__":
    main()
