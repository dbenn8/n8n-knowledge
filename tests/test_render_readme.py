import importlib.util, json, os
_SE = os.path.join(os.path.dirname(__file__), "..", "scripts", "eval")

def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SE, name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

PRESETS = [{
    "id": "canonical", "label": "Canonical",
    "summary": {"cells": [
        {"backend": "claude", "condition": "plugin gate-ON (ship default)", "cover": "128/128",
         "gotcha_pct": 39, "valid_pct": 94, "correct_pct": 93, "works_pct": 80, "avg_cost": 0.75, "turns": 9.8},
        {"backend": "claude", "condition": "mcp", "cover": "128/128",
         "gotcha_pct": 32, "valid_pct": 72, "correct_pct": 70, "works_pct": 59, "avg_cost": 1.26, "turns": 19.4},
        {"backend": "deepseek", "condition": "plugin gate-ON (ship default)", "cover": "128/128",
         "gotcha_pct": 46, "valid_pct": 92, "correct_pct": 75, "works_pct": 67, "avg_cost": 0.068, "turns": 27.5},
        {"backend": "deepseek", "condition": "mcp", "cover": "128/128",
         "gotcha_pct": 36, "valid_pct": 79, "correct_pct": 70, "works_pct": 62, "avg_cost": 0.093, "turns": 38.1},
    ]},
}]


def test_render_eval_tables_has_both_models_and_values():
    rr = _load("render_readme")
    md = rr.render_eval_tables(PRESETS)
    assert "**Claude Sonnet 4.6**" in md
    assert "**DeepSeek v4 Flash**" in md
    # provenance suffixes preserved on re-render
    assert "a full 128-prompt run on the current shipped plugin" in md
    assert "latest available per prompt" in md
    assert "| **plugin (gate-ON, ship default)** | **94%** | **93%** | **80%** | **39%** | **$0.75** | **9.8** |" in md
    assert "| n8n-mcp | 72% | 70% | 59% | 32% | $1.26 | 19.4 |" in md
    assert "Pro" not in md  # DeepSeek is Flash, never Pro


def test_splice_replaces_between_markers_idempotently():
    rr = _load("render_readme")
    doc = "a\n<!-- AUTOGEN:t START -->\nOLD\n<!-- AUTOGEN:t END -->\nb"
    out = rr.splice(doc, "t", "NEW")
    assert "OLD" not in out and "NEW" in out
    assert out.startswith("a\n") and out.endswith("\nb")
    assert rr.splice(out, "t", "NEW") == out  # idempotent

def test_splice_raises_without_markers():
    import pytest
    rr = _load("render_readme")
    with pytest.raises(ValueError):
        rr.splice("no markers here", "t", "x")


def test_main_renders_tables_into_readme(tmp_path):
    rr = _load("render_readme")
    sp = tmp_path / "published_stats.json"
    sp.write_text(json.dumps({"presets": PRESETS}))
    readme = tmp_path / "README.md"
    readme.write_text("intro\n<!-- AUTOGEN:eval-tables START -->\nOLD\n<!-- AUTOGEN:eval-tables END -->\noutro\n")
    rr.main(["--stats", str(sp), "--readme", str(readme)])
    out = readme.read_text()
    assert "OLD" not in out
    assert "**Claude Sonnet 4.6**" in out and "**DeepSeek v4 Flash**" in out
    assert out.startswith("intro\n") and out.rstrip().endswith("outro")
