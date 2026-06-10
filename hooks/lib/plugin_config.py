#!/usr/bin/env python3
"""Shared per-project config loader for the n8n-knowledge plugin.

Validator routing settings in this module are for plugin-side validation calls
only. They are not intended for the eval harness conditions (`plugin`, `mcp`,
`bare`) or for local post-hoc validation scripts, which should continue to use
their own explicit local validator paths.
"""

from __future__ import annotations

import glob
import os
from typing import Any

DEFAULTS = {
    "high_threshold": 70,
    "medium_threshold": 50,
    "docs_base": 80,
    "github_base": 49,
    "community_base": 40,
    "solved_bonus": 25,
    "clear_signal_bonus": 25,
    "author_member_bonus": 5,
    "high_engagement_threshold": 10,
    "high_engagement_bonus": 20,
    "medium_engagement_threshold": 3,
    "medium_engagement_bonus": 10,
    "high_views_threshold": 500,
    "views_bonus": 5,
    "max_results": 5,
    "max_low_results": 1,
    "max_text_length_high": -1,
    "max_text_length_medium": 800,
    "max_text_length_low": 300,
    # Plugin-side validator routing config only.
    "validator_mode": "default",  # default | local | cloud
    "validator_cloud_url": "",
    "validator_local_path": "",
}

ENV_OVERRIDES = {
    "validator_mode": "CLAUDE_PLUGIN_OPTION_VALIDATORMODE",
    "validator_cloud_url": "CLAUDE_PLUGIN_OPTION_VALIDATORCLOUDURL",
    "validator_local_path": "CLAUDE_PLUGIN_OPTION_VALIDATORLOCALPATH",
}


def _coerce_value(raw: str, default: Any) -> Any:
    """Coerce frontmatter values to the same type as their defaults."""
    if isinstance(default, bool):
        return raw.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_config(project_dir: str | None):
    """Load plugin config from .claude/n8n-knowledge.local.md if it exists."""
    config = dict(DEFAULTS)

    if not project_dir:
        project_dir = ""

    config_path = os.path.join(project_dir, ".claude", "n8n-knowledge.local.md")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                content = f.read()

            in_frontmatter = False
            for line in content.splitlines():
                if line.strip() == "---":
                    if in_frontmatter:
                        break
                    in_frontmatter = True
                    continue

                if in_frontmatter and ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key not in DEFAULTS:
                        continue
                    try:
                        config[key] = _coerce_value(val, DEFAULTS[key])
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    for key, env_name in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce_value(raw, DEFAULTS[key])
        except (ValueError, TypeError):
            continue

    return config


def find_n8n_mcp_install_root(config: dict[str, Any] | None = None) -> str | None:
    """Return the resolved local n8n-mcp install root, if present."""
    cfg = config or {}
    custom_path = (cfg.get("validator_local_path") or "").strip()
    if custom_path and os.path.exists(custom_path):
        return custom_path

    candidates = glob.glob(
        os.path.expanduser("~/.npm/_npx/*/node_modules/n8n-mcp")
    )
    if candidates:
        return max(candidates, key=os.path.getmtime)
    return None


def resolve_validator_target(project_dir: str | None, mode_override: str | None = None):
    """Resolve plugin-side validator routing only.

    This resolver is for future plugin validation calls. It should not be used
    by the eval harness conditions (`plugin`, `mcp`, `bare`) or by the local
    post-hoc validation scripts under `scripts/eval/`.
    """
    config = load_config(project_dir)
    requested_mode = (mode_override or config.get("validator_mode") or "default").strip().lower()
    if requested_mode not in {"default", "local", "cloud"}:
        requested_mode = "default"

    local_root = find_n8n_mcp_install_root(config)
    local_available = bool(local_root)
    cloud_url = (config.get("validator_cloud_url") or "").strip()

    effective_mode = None
    reason = ""
    if requested_mode == "local":
        if local_available:
            effective_mode = "local"
            reason = "validator_mode=local and local validator is installed"
        else:
            reason = "validator_mode=local but no local validator install was found"
    elif requested_mode == "cloud":
        if cloud_url:
            effective_mode = "cloud"
            reason = "validator_mode=cloud and validator_cloud_url is configured"
        else:
            reason = "validator_mode=cloud but validator_cloud_url is not configured"
    else:
        if local_available:
            effective_mode = "local"
            reason = "validator_mode=default prefers local when installed"
        elif cloud_url:
            effective_mode = "cloud"
            reason = "validator_mode=default fell back to cloud because local is unavailable"
        else:
            reason = "validator_mode=default found neither local validator nor validator_cloud_url"

    return {
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "reason": reason,
        "local_available": local_available,
        "local_root": local_root,
        "local_nodes_db_path": os.path.join(local_root, "data", "nodes.db") if local_root else None,
        "cloud_url": cloud_url or None,
        "config": config,
    }
