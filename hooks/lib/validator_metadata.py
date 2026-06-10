#!/usr/bin/env python3
"""Shared validator metadata helpers for plugin and eval paths."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def build_local_validator_info(local_root: str | None) -> dict[str, Any] | None:
    if not local_root:
        return None

    root = Path(local_root)
    package = _read_json_file(root / "package.json") or {}
    nodes_db_path = root / "data" / "nodes.db"
    version = package.get("version")

    return {
        "validator_engine": "n8n-mcp",
        "configured_n8n_mcp_version": version,
        "installed_n8n_mcp_version": version,
        "nodes_db_sha256": _sha256_file(nodes_db_path) if nodes_db_path.is_file() else None,
    }


def build_cloud_health_url(validate_url: str) -> str:
    parsed = urllib.parse.urlsplit(validate_url)
    health_path = "/public/validator-health"
    if not parsed.path.endswith("/public/validate-workflow"):
        health_path = "/health"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))


def fetch_cloud_health(validate_url: str, timeout_seconds: int = 10) -> dict[str, Any]:
    req = urllib.request.Request(build_cloud_health_url(validate_url), method="GET")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("validator health response was not a JSON object")
    return payload


def build_validator_descriptor(
    target: dict[str, Any],
    *,
    health_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    descriptor = {
        "requested_mode": target.get("requested_mode"),
        "effective_mode": target.get("effective_mode"),
        "reason": target.get("reason"),
        "cloud_url": target.get("cloud_url"),
        "local_root": target.get("local_root"),
        "validator_info": None,
        "status": None,
        "validator_mode": None,
    }

    if target.get("effective_mode") == "local":
        descriptor["status"] = "ok" if target.get("local_root") else "unavailable"
        descriptor["validator_mode"] = "local"
        descriptor["validator_info"] = build_local_validator_info(target.get("local_root"))
        return descriptor

    if target.get("effective_mode") == "cloud" and health_payload is not None:
        descriptor["status"] = health_payload.get("status")
        descriptor["validator_mode"] = health_payload.get("validator_mode")
        descriptor["validator_info"] = health_payload.get("validator_info")
        return descriptor

    if target.get("effective_mode") == "cloud":
        descriptor["status"] = "unavailable"
        descriptor["validator_mode"] = "cloud"

    return descriptor


def compare_validator_descriptors(
    left: dict[str, Any],
    right: dict[str, Any],
) -> list[str]:
    diffs: list[str] = []
    if left.get("effective_mode") != right.get("effective_mode"):
        diffs.append(
            f"effective_mode differs: {left.get('effective_mode')!r} != {right.get('effective_mode')!r}"
        )

    left_info = left.get("validator_info") or {}
    right_info = right.get("validator_info") or {}
    for key in [
        "configured_n8n_mcp_version",
        "installed_n8n_mcp_version",
        "nodes_db_sha256",
    ]:
        if left_info.get(key) != right_info.get(key):
            diffs.append(f"{key} differs: {left_info.get(key)!r} != {right_info.get(key)!r}")
    return diffs


def copy_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(descriptor)
