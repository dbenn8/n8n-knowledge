#!/usr/bin/env python3
"""Replace a plugin eval response's final workflow JSON with a validated scratch file."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _load_validator(project_dir: str):
    hooks_lib = os.path.join(project_dir, "hooks", "lib")
    if hooks_lib not in sys.path:
        sys.path.insert(0, hooks_lib)
    from validator_client import validate_workflow  # type: ignore

    return validate_workflow


def _replace_workflow_block(text: str, workflow: dict) -> tuple[str, bool]:
    canonical_block = "```json\n" + json.dumps(workflow, indent=2) + "\n```"
    pattern = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

    for match in pattern.finditer(text or ""):
        candidate = match.group(1).strip()
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
            replaced = (text or "")[: match.start()] + canonical_block + (text or "")[match.end() :]
            return replaced, True

    stripped = (text or "").rstrip()
    if stripped:
        return stripped + "\n\n" + canonical_block + "\n", False
    return canonical_block + "\n", False


def main() -> int:
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <response-json-file> <scratch-workflow-json> <project-dir>",
            file=sys.stderr,
        )
        return 2

    response_path = Path(sys.argv[1])
    scratch_path = Path(sys.argv[2])
    project_dir = sys.argv[3]

    if not response_path.exists() or not scratch_path.exists():
        print(json.dumps({"adopted": False, "reason": "missing_file"}))
        return 0

    payload = json.loads(response_path.read_text())
    workflow = json.loads(scratch_path.read_text())

    validate_workflow = _load_validator(project_dir)
    validation = validate_workflow(workflow, project_dir)
    if not validation.get("valid"):
        print(
            json.dumps(
                {
                    "adopted": False,
                    "reason": "scratch_not_valid",
                    "validator_mode": validation.get("validator_mode"),
                    "repair_messages": validation.get("repair_messages", []),
                }
            )
        )
        return 0

    new_result, replaced_existing = _replace_workflow_block(payload.get("result", ""), workflow)
    payload["result"] = new_result
    payload["validated_workflow_source"] = "scratch_file"
    payload["validated_workflow_mode"] = validation.get("validator_mode")

    response_path.write_text(json.dumps(payload))
    response_path.with_suffix(".candidate.workflow.json").write_text(json.dumps(workflow, indent=2))
    response_path.with_suffix(".validated.workflow.json").write_text(json.dumps(workflow, indent=2))

    print(
        json.dumps(
            {
                "adopted": True,
                "replaced_existing_block": replaced_existing,
                "validator_mode": validation.get("validator_mode"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
