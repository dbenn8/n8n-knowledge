"""Unit tests for hooks/lib/validator_client.py.

These tests pin the CURRENT behaviour of the plugin-side validator client:
result-shape parity between the local and cloud success paths, the inline
error-dict construction in each failure path, mock/timeout/connection handling,
and the env-var mock-response shortcut.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import validator_client as vc


# A minimal raw validator response (the shape validate_local/validate_cloud
# return, before shape_result enriches it).
RAW_VALID = {
    "valid": True,
    "error_count": 0,
    "warning_count": 0,
    "errors": [],
    "warnings": [],
    "statistics": {"totalNodes": 1, "triggerNodes": 1},
    "suggestions": [],
}

RAW_INVALID = {
    "valid": False,
    "error_count": 1,
    "warning_count": 0,
    "errors": [
        {"type": "error", "message": "Required property 'To' cannot be empty", "node": "Slack"}
    ],
    "warnings": [],
    "statistics": {"totalNodes": 2, "triggerNodes": 1},
    "suggestions": [],
}

WORKFLOW = {"name": "wf", "nodes": [], "connections": {}}


# ---------------------------------------------------------------------------
# shape_result
# ---------------------------------------------------------------------------

# The full set of keys shape_result is contracted to emit. If shape_result
# changes, this test forces a deliberate update rather than silent drift.
EXPECTED_SHAPE_KEYS = {
    "valid",
    "has_json",
    "extract_error",
    "error_count",
    "warning_count",
    "node_count",
    "trigger_count",
    "issues",
    "issues_block",
    "repair_messages",
    "feedback_block",
    "errors",
    "warnings",
    "warnings_block",
    "statistics",
    "suggestions",
    "validator_mode",
    "validator_info",
}


def test_shape_result_has_expected_keys():
    shaped = vc.shape_result(RAW_VALID, WORKFLOW, mode="local")
    assert set(shaped.keys()) == EXPECTED_SHAPE_KEYS


def test_shape_result_attaches_warnings_block():
    raw = {
        "valid": True,
        "errors": [],
        "warnings": [
            {"type": "warning", "message": "Deprecated field used", "node": "Slack"}
        ],
        "statistics": {"totalNodes": 1, "triggerNodes": 1},
    }
    shaped = vc.shape_result(raw, WORKFLOW, mode="local")
    assert "Deprecated field used" in shaped["warnings_block"]
    assert "Warnings (non-blocking" in shaped["warnings_block"]


def test_shape_result_warnings_block_empty_when_no_warnings():
    shaped = vc.shape_result(RAW_VALID, WORKFLOW, mode="local")
    assert shaped["warnings_block"] == ""


def test_shape_result_propagates_mode_and_counts():
    shaped = vc.shape_result(RAW_INVALID, WORKFLOW, mode="cloud")
    assert shaped["validator_mode"] == "cloud"
    assert shaped["valid"] is False
    assert shaped["error_count"] == 1
    assert shaped["node_count"] == 2
    assert shaped["trigger_count"] == 1
    assert shaped["has_json"] is True
    assert shaped["extract_error"] is None


def test_shape_result_counts_default_from_lists_when_absent():
    raw = {"valid": False, "errors": [{"message": "boom"}], "warnings": [{}, {}]}
    shaped = vc.shape_result(raw, WORKFLOW, mode="mock")
    assert shaped["error_count"] == 1
    assert shaped["warning_count"] == 2
    # No statistics -> node/trigger counts fall back to zero.
    assert shaped["node_count"] == 0
    assert shaped["trigger_count"] == 0


# ---------------------------------------------------------------------------
# Local <-> cloud success-shape parity
# ---------------------------------------------------------------------------

def _run_mode(monkeypatch, mode, raw):
    """Drive validate_workflow through a single resolved mode with a stub raw."""
    resolved = {
        "effective_mode": mode,
        "local_root": "/fake/root",
        "cloud_url": "https://example.test/public/validate-workflow",
        "reason": "stubbed",
    }
    monkeypatch.setattr(vc, "resolve_validator_target", lambda _pd: resolved)
    monkeypatch.setattr(vc, "validate_local", lambda wf, root: dict(raw))
    monkeypatch.setattr(vc, "validate_cloud", lambda wf, url: dict(raw))
    monkeypatch.delenv("N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE", raising=False)
    return vc.validate_workflow(WORKFLOW, "/proj")


def test_local_and_cloud_success_shapes_are_identical(monkeypatch):
    local_res = _run_mode(monkeypatch, "local", RAW_VALID)
    cloud_res = _run_mode(monkeypatch, "cloud", RAW_VALID)
    # Same keys, regardless of which transport produced the raw result.
    assert set(local_res.keys()) == set(cloud_res.keys())
    # Only validator_mode should differ between the two.
    differing = {k for k in local_res if local_res[k] != cloud_res[k]}
    assert differing == {"validator_mode"}
    assert local_res["validator_mode"] == "local"
    assert cloud_res["validator_mode"] == "cloud"


def test_local_and_cloud_error_shapes_are_identical(monkeypatch):
    local_res = _run_mode(monkeypatch, "local", RAW_INVALID)
    cloud_res = _run_mode(monkeypatch, "cloud", RAW_INVALID)
    assert set(local_res.keys()) == set(cloud_res.keys())
    differing = {k for k in local_res if local_res[k] != cloud_res[k]}
    assert differing == {"validator_mode"}


# ---------------------------------------------------------------------------
# Mock-response env-var path
# ---------------------------------------------------------------------------

def test_mock_response_env_var_short_circuits(monkeypatch):
    monkeypatch.setenv("N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE", json.dumps(RAW_VALID))

    def _boom(*_a, **_k):
        raise AssertionError("resolve_validator_target should not be called in mock mode")

    monkeypatch.setattr(vc, "resolve_validator_target", _boom)
    res = vc.validate_workflow(WORKFLOW, "/proj")
    assert res["validator_mode"] == "mock"
    assert res["valid"] is True
    assert set(res.keys()) == EXPECTED_SHAPE_KEYS


# ---------------------------------------------------------------------------
# validator_not_configured fallback
# ---------------------------------------------------------------------------

def test_unconfigured_fallback_shape(monkeypatch):
    resolved = {
        "effective_mode": None,
        "reason": "validator_mode=default found neither local validator nor validator_cloud_url",
    }
    monkeypatch.setattr(vc, "resolve_validator_target", lambda _pd: resolved)
    monkeypatch.delenv("N8N_KNOWLEDGE_VALIDATOR_MOCK_RESPONSE", raising=False)
    res = vc.validate_workflow(WORKFLOW, "/proj")
    assert res["valid"] is False
    assert res["extract_error"] == "validator_not_configured"
    assert res["validator_mode"] is None
    assert res["errors"][0]["type"] == "validator_not_configured"
    assert res["errors"][0]["message"] == resolved["reason"]


# ---------------------------------------------------------------------------
# validate_local error-dict construction (bridge failure)
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_validate_local_bridge_failure_error_dict(monkeypatch):
    monkeypatch.setattr(
        vc.subprocess, "run", lambda *a, **k: _Proc(1, stderr="exploded in node")
    )
    res = vc.validate_local(WORKFLOW, "/fake/root")
    assert res["valid"] is False
    assert res["error_count"] == 1
    assert res["errors"][0]["type"] == "validator_bridge_error"
    assert "exploded in node" in res["errors"][0]["message"]
    assert res["errors"][0]["node"] is None


def test_validate_local_bridge_failure_default_message(monkeypatch):
    monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: _Proc(2, stderr=""))
    res = vc.validate_local(WORKFLOW, "/fake/root")
    assert res["errors"][0]["message"] == "local validator bridge failed"


def test_validate_local_success_sets_validator_info(monkeypatch):
    monkeypatch.setattr(
        vc.subprocess, "run", lambda *a, **k: _Proc(0, stdout=json.dumps(RAW_VALID))
    )
    monkeypatch.setattr(
        vc, "build_local_validator_info", lambda root: {"validator_engine": "n8n-mcp"}
    )
    res = vc.validate_local(WORKFLOW, "/fake/root")
    assert res["validator_info"] == {"validator_engine": "n8n-mcp"}


def test_validate_local_success_keeps_existing_validator_info(monkeypatch):
    raw = dict(RAW_VALID, validator_info={"validator_engine": "preset"})
    monkeypatch.setattr(
        vc.subprocess, "run", lambda *a, **k: _Proc(0, stdout=json.dumps(raw))
    )
    res = vc.validate_local(WORKFLOW, "/fake/root")
    # setdefault must not clobber a validator_info already present.
    assert res["validator_info"] == {"validator_engine": "preset"}


# ---------------------------------------------------------------------------
# validate_cloud error-dict construction (HTTP error / connection error)
# ---------------------------------------------------------------------------

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        self._body = body.encode("utf-8")
        super().__init__("http://x", code, "err", {}, None)

    def read(self):  # type: ignore[override]
        return self._body


def test_validate_cloud_http_error_dict(monkeypatch):
    def _raise(*_a, **_k):
        raise _FakeHTTPError(503, "service down")

    monkeypatch.setattr(vc.urllib.request, "urlopen", _raise)
    res = vc.validate_cloud(WORKFLOW, "https://example.test/public/validate-workflow")
    assert res["valid"] is False
    assert res["error_count"] == 1
    assert res["errors"][0]["type"] == "validator_http_error"
    assert "HTTP 503" in res["errors"][0]["message"]
    assert "service down" in res["errors"][0]["message"]


def test_validate_cloud_connection_error_dict(monkeypatch):
    def _raise(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(vc.urllib.request, "urlopen", _raise)
    res = vc.validate_cloud(WORKFLOW, "https://example.test/public/validate-workflow")
    assert res["valid"] is False
    assert res["errors"][0]["type"] == "validator_request_error"
    assert "connection refused" in res["errors"][0]["message"]
    assert res["errors"][0]["node"] is None


def test_validate_cloud_timeout_error_dict(monkeypatch):
    def _raise(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(vc.urllib.request, "urlopen", _raise)
    res = vc.validate_cloud(WORKFLOW, "https://example.test/public/validate-workflow")
    assert res["errors"][0]["type"] == "validator_request_error"
    assert "timed out" in res["errors"][0]["message"]


# ---------------------------------------------------------------------------
# All three inline error dicts share the same skeleton (dedupe target)
# ---------------------------------------------------------------------------

ERROR_SKELETON_KEYS = {
    "valid",
    "error_count",
    "warning_count",
    "errors",
    "warnings",
    "statistics",
    "suggestions",
}


def test_inline_error_dicts_share_skeleton(monkeypatch):
    """All three failure paths (bridge / http / request) build the same dict
    skeleton. This guards the upcoming dedupe refactor."""
    monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: _Proc(1, stderr="x"))
    bridge = vc.validate_local(WORKFLOW, "/fake/root")

    monkeypatch.setattr(
        vc.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_FakeHTTPError(500, "y"))
    )
    http = vc.validate_cloud(WORKFLOW, "https://example.test/public/validate-workflow")

    monkeypatch.setattr(
        vc.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("z")),
    )
    request = vc.validate_cloud(WORKFLOW, "https://example.test/public/validate-workflow")

    for res in (bridge, http, request):
        assert set(res.keys()) == ERROR_SKELETON_KEYS
        assert res["valid"] is False
        assert res["error_count"] == 1
        assert res["warning_count"] == 0
        assert res["warnings"] == []
        assert res["statistics"] == {}
        assert res["suggestions"] == []
        assert len(res["errors"]) == 1
        err = res["errors"][0]
        assert set(err.keys()) == {"type", "message", "node"}
        assert err["node"] is None
