#!/usr/bin/env python3
"""Replace a response payload's workflow JSON block with a canonical workflow file."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def replace_workflow_block(text: str, workflow: dict) -> str:
    canonical_block = "```json\n" + json.dumps(workflow, indent=2) + "\n```"
    pattern = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
    for match in pattern.finditer(text or ""):
        candidate = match.group(1).strip()
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict) and ("nodes" in obj or "connections" in obj):
            return (text or "")[: match.start()] + canonical_block + (text or "")[match.end() :]
    stripped = (text or "").rstrip()
    if stripped:
        return stripped + "\n\n" + canonical_block + "\n"
    return canonical_block + "\n"


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <response-json-file> <workflow-json-file>", file=sys.stderr)
        return 2

    response_path = Path(sys.argv[1])
    workflow_path = Path(sys.argv[2])
    payload = json.loads(response_path.read_text())
    workflow = json.loads(workflow_path.read_text())
    payload["result"] = replace_workflow_block(payload.get("result", ""), workflow)
    payload["validated_workflow_source"] = "deterministic_auto_fix"
    response_path.write_text(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
