#!/usr/bin/env python3
"""Shared validator metadata helpers for plugin and eval paths."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
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


def _hash_sql_value(digest: "hashlib._Hash", value: Any) -> None:
    # Explicit type-tagged serialization: repr() is unicodedata-version
    # dependent (e.g. Python 3.10 escapes U+1FAAA, 3.11+ prints it literally),
    # so it hashes differently across interpreter versions on identical data.
    if value is None:
        digest.update(b"\x00N")
    elif isinstance(value, bool):
        digest.update(b"\x00O1" if value else b"\x00O0")
    elif isinstance(value, int):
        digest.update(b"\x00I" + str(value).encode("ascii"))
    elif isinstance(value, float):
        digest.update(b"\x00F" + repr(value).encode("ascii"))
    elif isinstance(value, bytes):
        digest.update(b"\x00B" + value)
    else:
        digest.update(b"\x00S" + str(value).encode("utf-8"))


def _nodes_content_sha256(path: Path) -> str | None:
    """Stable content hash of the nodes table.

    The physical nodes.db file mutates during normal n8n-mcp use (SQLite change
    counter, freed pages, FTS internals) without the node data changing, so a
    whole-file hash produces false mismatches between a fresh install and a
    used one. Hashing the ordered rows of the nodes table compares the data
    that actually drives validation. Must stay byte-identical with
    n8n-hindsight ops-proxy/workflow_validator.py:_nodes_content_sha256.
    """
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        digest = hashlib.sha256()
        for row in db.execute("SELECT * FROM nodes ORDER BY node_type"):
            for value in row:
                _hash_sql_value(digest, value)
            digest.update(b"\x00R")
        return digest.hexdigest()
    except sqlite3.Error:
        return None
    finally:
        db.close()


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
        "nodes_content_sha256": (
            _nodes_content_sha256(nodes_db_path) if nodes_db_path.is_file() else None
        ),
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
    left_info = left.get("validator_info") or {}
    right_info = right.get("validator_info") or {}
    for key in [
        "configured_n8n_mcp_version",
        "installed_n8n_mcp_version",
    ]:
        if left_info.get(key) != right_info.get(key):
            diffs.append(f"{key} differs: {left_info.get(key)!r} != {right_info.get(key)!r}")

    # Node database comparison: prefer the logical content hash (stable across
    # SQLite runtime bookkeeping) when both sides expose it; fall back to the
    # physical file hash for older validators that only report nodes_db_sha256.
    if left_info.get("nodes_content_sha256") and right_info.get("nodes_content_sha256"):
        db_key = "nodes_content_sha256"
    else:
        db_key = "nodes_db_sha256"
    if left_info.get(db_key) != right_info.get(db_key):
        diffs.append(f"{db_key} differs: {left_info.get(db_key)!r} != {right_info.get(db_key)!r}")

    # Mixed modes (e.g. cloud plugin + local scoring) are allowed when the two
    # engines are verifiably equivalent: versions and node data all match and
    # are actually present. Flag the mode difference only when equivalence is
    # unproven — any diff above, or missing metadata on either side.
    if left.get("effective_mode") != right.get("effective_mode"):
        verified_equivalent = (
            not diffs
            and bool(left_info.get("installed_n8n_mcp_version"))
            and bool(left_info.get(db_key))
        )
        if not verified_equivalent:
            diffs.insert(
                0,
                f"effective_mode differs: {left.get('effective_mode')!r} != {right.get('effective_mode')!r}",
            )
    return diffs


def copy_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(descriptor)
