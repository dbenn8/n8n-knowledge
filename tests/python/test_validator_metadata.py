"""Unit tests for hooks/lib/validator_metadata.py.

Covers cloud health-URL derivation, health payload fetch/parse, local validator
info assembly, the validator descriptor builder for local/cloud/unavailable
modes, and the mixed-mode equivalence logic in compare_validator_descriptors.

Content-hash internals (_nodes_content_sha256 / _hash_sql_value) are owned by a
separate fixture test and are intentionally NOT exercised here.
"""

from __future__ import annotations

import json

import pytest

import validator_metadata as vm


# ---------------------------------------------------------------------------
# build_cloud_health_url
# ---------------------------------------------------------------------------

def test_health_url_for_validate_workflow_endpoint():
    url = "https://api.example.test/public/validate-workflow"
    assert (
        vm.build_cloud_health_url(url)
        == "https://api.example.test/public/validator-health"
    )


def test_health_url_for_non_validate_endpoint_uses_plain_health():
    url = "https://api.example.test/something/else"
    assert vm.build_cloud_health_url(url) == "https://api.example.test/health"


def test_health_url_preserves_scheme_and_netloc_with_port():
    url = "http://localhost:8080/public/validate-workflow"
    assert (
        vm.build_cloud_health_url(url)
        == "http://localhost:8080/public/validator-health"
    )


# ---------------------------------------------------------------------------
# fetch_cloud_health
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload.encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_cloud_health_parses_object(monkeypatch):
    payload = {"status": "ok", "validator_mode": "cloud", "validator_info": {"x": 1}}
    monkeypatch.setattr(
        vm.urllib.request, "urlopen", lambda *a, **k: _FakeResp(json.dumps(payload))
    )
    out = vm.fetch_cloud_health("https://api.example.test/public/validate-workflow")
    assert out == payload


def test_fetch_cloud_health_rejects_non_object(monkeypatch):
    monkeypatch.setattr(
        vm.urllib.request, "urlopen", lambda *a, **k: _FakeResp(json.dumps([1, 2, 3]))
    )
    with pytest.raises(ValueError):
        vm.fetch_cloud_health("https://api.example.test/public/validate-workflow")


# ---------------------------------------------------------------------------
# build_local_validator_info
# ---------------------------------------------------------------------------

def test_local_validator_info_none_when_no_root():
    assert vm.build_local_validator_info(None) is None
    assert vm.build_local_validator_info("") is None


def test_local_validator_info_reads_package_version(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"version": "2.3.4"}))
    info = vm.build_local_validator_info(str(tmp_path))
    assert info is not None
    assert info["validator_engine"] == "n8n-mcp"
    assert info["configured_n8n_mcp_version"] == "2.3.4"
    assert info["installed_n8n_mcp_version"] == "2.3.4"
    # No nodes.db on disk -> hashes are None.
    assert info["nodes_db_sha256"] is None
    assert info["nodes_content_sha256"] is None


def test_local_validator_info_missing_package_json(tmp_path):
    info = vm.build_local_validator_info(str(tmp_path))
    assert info is not None
    assert info["configured_n8n_mcp_version"] is None


# ---------------------------------------------------------------------------
# build_validator_descriptor
# ---------------------------------------------------------------------------

def test_descriptor_local_ok_when_root_present(monkeypatch):
    monkeypatch.setattr(
        vm, "build_local_validator_info", lambda root: {"installed_n8n_mcp_version": "1.0.0"}
    )
    target = {
        "requested_mode": "local",
        "effective_mode": "local",
        "reason": "ok",
        "local_root": "/fake/root",
        "cloud_url": None,
    }
    desc = vm.build_validator_descriptor(target)
    assert desc["status"] == "ok"
    assert desc["validator_mode"] == "local"
    assert desc["validator_info"] == {"installed_n8n_mcp_version": "1.0.0"}


def test_descriptor_local_unavailable_when_no_root():
    target = {"effective_mode": "local", "local_root": None}
    desc = vm.build_validator_descriptor(target)
    assert desc["status"] == "unavailable"
    assert desc["validator_mode"] == "local"


def test_descriptor_cloud_with_health_payload():
    target = {"effective_mode": "cloud", "cloud_url": "https://x/public/validate-workflow"}
    health = {"status": "ok", "validator_mode": "cloud", "validator_info": {"v": 1}}
    desc = vm.build_validator_descriptor(target, health_payload=health)
    assert desc["status"] == "ok"
    assert desc["validator_mode"] == "cloud"
    assert desc["validator_info"] == {"v": 1}


def test_descriptor_cloud_without_health_is_unavailable():
    target = {"effective_mode": "cloud", "cloud_url": "https://x/public/validate-workflow"}
    desc = vm.build_validator_descriptor(target)
    assert desc["status"] == "unavailable"
    assert desc["validator_mode"] == "cloud"
    assert desc["validator_info"] is None


def test_descriptor_unknown_mode_leaves_defaults():
    target = {"effective_mode": None}
    desc = vm.build_validator_descriptor(target)
    assert desc["status"] is None
    assert desc["validator_mode"] is None
    assert desc["validator_info"] is None


# ---------------------------------------------------------------------------
# compare_validator_descriptors
# ---------------------------------------------------------------------------

def _desc(mode, version, content_hash=None, file_hash=None):
    info = {
        "configured_n8n_mcp_version": version,
        "installed_n8n_mcp_version": version,
    }
    if content_hash is not None:
        info["nodes_content_sha256"] = content_hash
    if file_hash is not None:
        info["nodes_db_sha256"] = file_hash
    return {"effective_mode": mode, "validator_info": info}


def test_compare_identical_same_mode_no_diffs():
    left = _desc("local", "1.0.0", content_hash="abc")
    right = _desc("local", "1.0.0", content_hash="abc")
    assert vm.compare_validator_descriptors(left, right) == []


def test_compare_version_mismatch_reported():
    left = _desc("local", "1.0.0", content_hash="abc")
    right = _desc("local", "1.0.1", content_hash="abc")
    diffs = vm.compare_validator_descriptors(left, right)
    assert any("configured_n8n_mcp_version differs" in d for d in diffs)
    assert any("installed_n8n_mcp_version differs" in d for d in diffs)


def test_compare_prefers_content_hash_when_both_present():
    # File hashes differ but content hashes match -> no db diff reported.
    left = _desc("local", "1.0.0", content_hash="same", file_hash="fileA")
    right = _desc("local", "1.0.0", content_hash="same", file_hash="fileB")
    assert vm.compare_validator_descriptors(left, right) == []


def test_compare_falls_back_to_file_hash_when_content_absent():
    left = _desc("local", "1.0.0", file_hash="fileA")
    right = _desc("local", "1.0.0", file_hash="fileB")
    diffs = vm.compare_validator_descriptors(left, right)
    assert any("nodes_db_sha256 differs" in d for d in diffs)


def test_compare_content_hash_mismatch_reported():
    left = _desc("local", "1.0.0", content_hash="hashA")
    right = _desc("local", "1.0.0", content_hash="hashB")
    diffs = vm.compare_validator_descriptors(left, right)
    assert any("nodes_content_sha256 differs" in d for d in diffs)


def test_compare_mixed_modes_verified_equivalent_no_mode_diff():
    # cloud vs local, but versions + content hash match AND are present
    # -> equivalence verified, mode difference suppressed.
    left = _desc("cloud", "1.0.0", content_hash="abc")
    right = _desc("local", "1.0.0", content_hash="abc")
    assert vm.compare_validator_descriptors(left, right) == []


def test_compare_mixed_modes_flagged_when_metadata_missing():
    # cloud vs local with no version/hash metadata -> cannot verify equivalence
    # -> the mode difference IS flagged, and inserted first.
    left = {"effective_mode": "cloud", "validator_info": {}}
    right = {"effective_mode": "local", "validator_info": {}}
    diffs = vm.compare_validator_descriptors(left, right)
    assert diffs
    assert "effective_mode differs" in diffs[0]


def test_compare_mixed_modes_flagged_when_versions_differ():
    left = _desc("cloud", "1.0.0", content_hash="abc")
    right = _desc("local", "2.0.0", content_hash="abc")
    diffs = vm.compare_validator_descriptors(left, right)
    # version diff present -> equivalence unproven -> mode diff flagged first.
    assert "effective_mode differs" in diffs[0]


def test_copy_descriptor_is_deep():
    original = {"effective_mode": "local", "validator_info": {"nested": [1, 2]}}
    clone = vm.copy_descriptor(original)
    assert clone == original
    clone["validator_info"]["nested"].append(3)
    assert original["validator_info"]["nested"] == [1, 2]
