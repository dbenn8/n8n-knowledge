#!/usr/bin/env python3
"""Smoke-test the cloud validator endpoint from the plugin repo.

Usage:
  python3 scripts/eval/smoke_test_cloud_validator.py
  python3 scripts/eval/smoke_test_cloud_validator.py --project-dir /path/to/repo
  python3 scripts/eval/smoke_test_cloud_validator.py --mode cloud
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
HOOKS_LIB = os.path.join(REPO_DIR, "hooks", "lib")
if HOOKS_LIB not in sys.path:
    sys.path.insert(0, HOOKS_LIB)

from plugin_config import resolve_validator_target


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text)
        except Exception:
            return exc.code, {"error": text}


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def build_valid_workflow() -> dict:
    return {
        "name": "Validator Smoke Test",
        "nodes": [
            {
                "id": "manual-trigger-1",
                "name": "Manual Trigger",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [260, 300],
                "parameters": {},
            }
        ],
        "connections": {},
    }


def build_valid_two_node_workflow() -> dict:
    return {
        "name": "Validator Smoke Test Valid",
        "nodes": [
            {
                "id": "manual-trigger-1",
                "name": "Manual Trigger",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [260, 300],
                "parameters": {},
            },
            {
                "id": "set-1",
                "name": "Set",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [520, 300],
                "parameters": {
                    "assignments": {
                        "assignments": [
                            {
                                "id": "field-1",
                                "name": "status",
                                "value": "ok",
                                "type": "string",
                            }
                        ]
                    }
                },
            },
        ],
        "connections": {
            "Manual Trigger": {
                "main": [[{"node": "Set", "type": "main", "index": 0}]]
            }
        },
    }


def build_invalid_workflow() -> dict:
    return {
        "name": "Validator Smoke Test Invalid",
        "nodes": [
            {
                "id": "manual-trigger-1",
                "name": "",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 999,
                "position": [260, 300],
                "parameters": {},
            }
        ],
        "connections": {},
    }


def run_smoke_tests(url: str) -> None:
    valid_single_node_workflow = build_valid_workflow()
    valid_two_node_workflow = build_valid_two_node_workflow()
    invalid_workflow = build_invalid_workflow()

    tests = [
        (
            "raw workflow valid two-node",
            {
                "workflow": valid_two_node_workflow,
            },
        ),
        (
            "response_text fenced JSON valid two-node",
            {
                "response_text": "```json\n" + json.dumps(valid_two_node_workflow, indent=2) + "\n```",
            },
        ),
        (
            "raw workflow single-node invalid",
            {
                "workflow": valid_single_node_workflow,
            },
        ),
        (
            "response_text bare embedded JSON valid two-node",
            {
                "response_text": (
                    "Here is the workflow draft.\n"
                    + json.dumps(valid_two_node_workflow)
                    + "\nUse this as-is."
                ),
            },
        ),
        (
            "response_text no json + repair prompt",
            {
                "response_text": "Please create an n8n workflow that posts to Slack when a webhook fires.",
                "original_prompt": "Build an n8n workflow that posts to Slack when a webhook fires",
                "include_repair_prompt": True,
                "max_errors": 8,
            },
        ),
        (
            "invalid workflow + repair prompt",
            {
                "workflow": invalid_workflow,
                "original_prompt": "Build an n8n workflow that starts with a manual trigger.",
                "include_repair_prompt": True,
                "max_errors": 8,
            },
        ),
    ]

    for label, payload in tests:
        status, data = post_json(url, payload)
        print(f"\n=== {label} ===")
        print(f"HTTP {status}")
        print(json.dumps(data, indent=2)[:4000])

        assert_true(status == 200, f"{label}: expected HTTP 200, got {status}")
        assert_true("valid" in data, f"{label}: missing 'valid'")
        assert_true("has_json" in data, f"{label}: missing 'has_json'")

        if label in {
            "raw workflow valid two-node",
            "response_text fenced JSON valid two-node",
            "response_text bare embedded JSON valid two-node",
        }:
            assert_true(data.get("valid") is True, f"{label}: expected valid=true")
        elif label == "raw workflow single-node invalid":
            assert_true(data.get("valid") is False, f"{label}: expected valid=false")
            assert_true(data.get("has_json") is True, f"{label}: expected has_json=true")
        else:
            assert_true(data.get("valid") is False, f"{label}: expected valid=false")
            assert_true(bool(data.get("repair_messages")), f"{label}: expected repair_messages")
            assert_true(bool(data.get("feedback_block")), f"{label}: expected feedback_block")
            assert_true(bool(data.get("repair_prompt")), f"{label}: expected repair_prompt")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", default=REPO_DIR)
    parser.add_argument("--mode", default="cloud", help="Override validator mode: local, cloud, or default")
    args = parser.parse_args()

    resolved = resolve_validator_target(args.project_dir, args.mode)
    print(json.dumps(resolved, indent=2))

    if resolved.get("effective_mode") != "cloud":
        raise SystemExit(
            "Cloud validator is not selected. Set validator_mode=cloud or configure "
            "validator_cloud_url and use validator_mode=default."
        )

    url = resolved.get("cloud_url")
    if not url:
        raise SystemExit("validator_cloud_url is not configured.")

    run_smoke_tests(url)
    print("\nCloud validator smoke tests passed.")


if __name__ == "__main__":
    main()
