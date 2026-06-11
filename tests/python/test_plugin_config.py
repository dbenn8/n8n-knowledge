"""Unit tests for hooks/lib/plugin_config.py.

Covers value coercion edge cases, config option precedence (defaults vs
frontmatter vs CLAUDE_PLUGIN_OPTION_* env vars), local install discovery, and
validator-target resolution across default/local/cloud modes.
"""

from __future__ import annotations

import os

import pytest

import plugin_config as pc


VALIDATOR_ENV_VARS = [
    "CLAUDE_PLUGIN_OPTION_VALIDATORMODE",
    "CLAUDE_PLUGIN_OPTION_VALIDATORCLOUDURL",
    "CLAUDE_PLUGIN_OPTION_VALIDATORLOCALPATH",
]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in VALIDATOR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# _coerce_value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "Yes"])
def test_coerce_bool_truthy(raw):
    assert pc._coerce_value(raw, default=False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "anything"])
def test_coerce_bool_falsy(raw):
    assert pc._coerce_value(raw, default=False) is False


def test_coerce_int():
    assert pc._coerce_value("42", default=0) == 42
    assert pc._coerce_value("-7", default=0) == -7


def test_coerce_int_invalid_raises():
    with pytest.raises(ValueError):
        pc._coerce_value("notanint", default=0)


def test_coerce_float():
    assert pc._coerce_value("3.5", default=0.0) == 3.5


def test_coerce_string_passthrough():
    assert pc._coerce_value("local", default="default") == "local"


def test_coerce_bool_takes_priority_over_int():
    # bool is a subclass of int; the bool branch must win.
    assert pc._coerce_value("yes", default=True) is True
    assert pc._coerce_value("no", default=True) is False


# ---------------------------------------------------------------------------
# load_config — defaults & env precedence
# ---------------------------------------------------------------------------

def test_load_config_returns_defaults_for_empty_dir(tmp_path):
    # Point at an empty project dir with no .claude/n8n-knowledge.local.md.
    # NOTE: load_config(None) reads a CWD-relative .claude path, so passing an
    # explicit empty dir is required for a CWD-independent defaults check.
    cfg = pc.load_config(str(tmp_path))
    assert cfg["validator_mode"] == "default"
    assert cfg["max_results"] == 5
    assert cfg["docs_base"] == 80
    # Must be a copy, not the shared DEFAULTS object.
    assert cfg is not pc.DEFAULTS


def test_env_override_string_option(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VALIDATORMODE", "cloud")
    cfg = pc.load_config(str(tmp_path))
    assert cfg["validator_mode"] == "cloud"


def test_env_override_cloud_url(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VALIDATORCLOUDURL", "https://x/public/validate-workflow")
    cfg = pc.load_config(str(tmp_path))
    assert cfg["validator_cloud_url"] == "https://x/public/validate-workflow"


def test_empty_env_var_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VALIDATORMODE", "")
    cfg = pc.load_config(str(tmp_path))
    # Empty string env var must not override the default.
    assert cfg["validator_mode"] == "default"


def test_frontmatter_parsing(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "n8n-knowledge.local.md").write_text(
        "---\n"
        'validator_mode: "local"\n'
        "max_results: 9\n"
        "unknown_key: ignored\n"
        "---\n"
        "# body text\n"
    )
    cfg = pc.load_config(str(tmp_path))
    assert cfg["validator_mode"] == "local"
    assert cfg["max_results"] == 9
    assert "unknown_key" not in cfg


def test_env_overrides_frontmatter(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "n8n-knowledge.local.md").write_text(
        "---\nvalidator_mode: local\n---\n"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VALIDATORMODE", "cloud")
    cfg = pc.load_config(str(tmp_path))
    # Env var is applied after frontmatter -> env wins.
    assert cfg["validator_mode"] == "cloud"


def test_frontmatter_bad_int_value_is_skipped(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "n8n-knowledge.local.md").write_text(
        "---\nmax_results: not-a-number\n---\n"
    )
    cfg = pc.load_config(str(tmp_path))
    # Coercion fails -> default retained.
    assert cfg["max_results"] == 5


# ---------------------------------------------------------------------------
# find_n8n_mcp_install_root
# ---------------------------------------------------------------------------

def test_find_install_root_uses_custom_path_when_exists(tmp_path):
    cfg = {"validator_local_path": str(tmp_path)}
    assert pc.find_n8n_mcp_install_root(cfg) == str(tmp_path)


def test_find_install_root_ignores_missing_custom_path(monkeypatch):
    monkeypatch.setattr(pc.glob, "glob", lambda pattern: [])
    cfg = {"validator_local_path": "/definitely/not/here"}
    assert pc.find_n8n_mcp_install_root(cfg) is None


def test_find_install_root_picks_newest_npx_candidate(monkeypatch):
    monkeypatch.setattr(pc.glob, "glob", lambda pattern: ["/a/n8n-mcp", "/b/n8n-mcp"])
    monkeypatch.setattr(pc.os.path, "getmtime", lambda p: 1 if p == "/a/n8n-mcp" else 2)
    assert pc.find_n8n_mcp_install_root({}) == "/b/n8n-mcp"


def test_find_install_root_none_when_no_candidates(monkeypatch):
    monkeypatch.setattr(pc.glob, "glob", lambda pattern: [])
    assert pc.find_n8n_mcp_install_root({}) is None


# ---------------------------------------------------------------------------
# resolve_validator_target_config — routing precedence
# ---------------------------------------------------------------------------

def test_resolve_default_prefers_local(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: "/local/root")
    res = pc.resolve_validator_target_config({"validator_mode": "default"})
    assert res["effective_mode"] == "local"
    assert res["local_available"] is True
    assert "prefers local" in res["reason"]


def test_resolve_default_falls_back_to_cloud(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: None)
    res = pc.resolve_validator_target_config(
        {"validator_mode": "default", "validator_cloud_url": "https://x/public/validate-workflow"}
    )
    assert res["effective_mode"] == "cloud"
    assert "fell back to cloud" in res["reason"]


def test_resolve_default_neither_available(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: None)
    res = pc.resolve_validator_target_config({"validator_mode": "default"})
    assert res["effective_mode"] is None
    assert "neither local validator nor validator_cloud_url" in res["reason"]


def test_resolve_explicit_local_requires_install(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: None)
    res = pc.resolve_validator_target_config({"validator_mode": "local"})
    assert res["effective_mode"] is None
    assert "no local validator install" in res["reason"]


def test_resolve_explicit_local_available(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: "/local/root")
    res = pc.resolve_validator_target_config({"validator_mode": "local"})
    assert res["effective_mode"] == "local"
    assert res["local_root"] == "/local/root"
    assert res["local_nodes_db_path"] == os.path.join("/local/root", "data", "nodes.db")


def test_resolve_explicit_cloud_requires_url(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: None)
    res = pc.resolve_validator_target_config({"validator_mode": "cloud"})
    assert res["effective_mode"] is None
    assert "validator_cloud_url is not configured" in res["reason"]


def test_resolve_explicit_cloud_with_url(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: "/ignored/local")
    res = pc.resolve_validator_target_config(
        {"validator_mode": "cloud", "validator_cloud_url": "https://x/public/validate-workflow"}
    )
    assert res["effective_mode"] == "cloud"
    assert res["cloud_url"] == "https://x/public/validate-workflow"


def test_resolve_mode_override_beats_config(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: "/local/root")
    res = pc.resolve_validator_target_config(
        {"validator_mode": "cloud", "validator_cloud_url": "https://x"},
        mode_override="local",
    )
    assert res["requested_mode"] == "local"
    assert res["effective_mode"] == "local"


def test_resolve_unknown_mode_normalizes_to_default(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: "/local/root")
    res = pc.resolve_validator_target_config({"validator_mode": "bogus"})
    assert res["requested_mode"] == "default"
    assert res["effective_mode"] == "local"


def test_resolve_local_nodes_db_path_none_without_root(monkeypatch):
    monkeypatch.setattr(pc, "find_n8n_mcp_install_root", lambda cfg: None)
    res = pc.resolve_validator_target_config({"validator_mode": "default"})
    assert res["local_nodes_db_path"] is None
    assert res["cloud_url"] is None
