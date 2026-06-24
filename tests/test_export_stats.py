#!/usr/bin/env python3
"""Tests for scripts/eval/export_stats.py — the publishable stats exporter that
feeds the n8n-knowledge benchmark site. Integration test against the live
out/eval/eval.db (skipped if absent), mirroring test_dashboard_parity.py.

The exporter must reuse the dashboard's get_summary / get_rows / filter_rows /
PRESETS so the published numbers match the dashboard and report.py exactly.
"""
import importlib.util
import json
import os
import sys

import pytest

_SE = os.path.join(os.path.dirname(__file__), "..", "scripts", "eval")
DB = os.path.join(_SE, "..", "..", "out", "eval", "eval.db")

pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="eval.db not present")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, _SE)  # dashboard/server.py loads report.py by path
    spec.loader.exec_module(m)
    return m


def _export():
    return _load("export_stats", os.path.join(_SE, "export_stats.py"))


def test_build_stats_shape_and_published_presets():
    exp = _export()
    server = exp.load_server()
    stats = exp.build_stats(server, generated_at="2026-06-24T00:00:00Z", db_sha256="deadbeef")
    assert stats["generated_at"] == "2026-06-24T00:00:00Z"
    assert stats["db_sha256"] == "deadbeef"
    ids = [p["id"] for p in stats["presets"]]
    assert "canonical" in ids, "canonical preset must be published"
    assert "gotcha" in ids, "gotcha preset must be published"
    # the 'off' debug preset must NOT be published to the public site
    assert "off" not in ids


def test_canonical_has_both_backends_and_funnel_keys():
    exp = _export()
    server = exp.load_server()
    stats = exp.build_stats(server, generated_at="t", db_sha256="h")
    canon = next(p for p in stats["presets"] if p["id"] == "canonical")
    cells = canon["summary"]["cells"]
    assert {c["backend"] for c in cells} == {"claude", "deepseek"}
    required = ("backend", "condition", "cover", "gotcha_pct", "valid_pct",
                "correct_pct", "works_pct", "judged_pct", "avg_cost", "turns", "inst")
    for c in cells:
        for k in required:
            assert k in c, f"cell missing key {k}: {c}"
    # the headline cell exists and works_pct is a sane percentage (or None)
    claude_plugin = [c for c in cells
                     if c["backend"] == "claude" and c["condition"].startswith("plugin gate-ON")]
    assert claude_plugin, "expected a claude plugin gate-ON cell"
    w = claude_plugin[0]["works_pct"]
    assert w is None or (0 <= w <= 100)


def test_gotcha_preset_rows_are_all_bug_prompts():
    exp = _export()
    server = exp.load_server()
    stats = exp.build_stats(server, generated_at="t", db_sha256="h")
    gotcha = next(p for p in stats["presets"] if p["id"] == "gotcha")
    assert gotcha["rows"], "expected published rows for the gotcha preset"
    # filter_rows applied the preset's gotcha_prompt=1 filter
    assert all(str(r.get("gotcha_prompt")) == "1" for r in gotcha["rows"])


def test_stats_is_json_serializable():
    exp = _export()
    server = exp.load_server()
    stats = exp.build_stats(server, generated_at="t", db_sha256="h")
    # must round-trip through JSON (the site bakes this file in)
    s = json.dumps(stats)
    assert json.loads(s)["generated_at"] == "t"
