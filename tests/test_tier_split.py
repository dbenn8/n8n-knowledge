import importlib.util, os, statistics
import pytest

_SE = os.path.join(os.path.dirname(__file__), "..", "scripts", "eval")
DB = os.path.join(_SE, "..", "..", "out", "eval", "eval.db")
PRO = ("20260624-135803-v2", "20260624-122555-v2")  # confirm with eval_clean
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="eval.db not present")


def _server():
    import sys
    sys.path.insert(0, _SE)
    spec = importlib.util.spec_from_file_location("dash_server", os.path.join(_SE, "dashboard", "server.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_time_median_fn():
    s = _server()
    assert s._median_s([1000, 3000, 2000]) == 2          # ms -> s, median
    assert s._median_s([]) is None


def test_tiers_split_and_reproduce_pro_numbers():
    s = _server()
    out = s.get_summary("v2-fileoutput", "all", "newest", pro_run_ids=PRO)
    cells = out["cells"]
    # three groups present; deepseek carries a tier
    assert {(c["backend"], c["tier"]) for c in cells} >= {
        ("claude", None), ("deepseek", "flash"), ("deepseek", "pro")}
    pro_plugin = next(c for c in cells if c["backend"] == "deepseek" and c["tier"] == "pro"
                      and c["condition"].startswith("plugin gate-ON"))
    assert pro_plugin["valid_pct"] == 98 and pro_plugin["works_pct"] == 68
    assert pro_plugin["avg_cost"] == 0.044
    assert pro_plugin["time_mean_s"] == 357 and pro_plugin["time_median_s"] == 251
    pro_mcp = next(c for c in cells if c["backend"] == "deepseek" and c["tier"] == "pro"
                   and c["condition"] == "mcp")
    assert pro_mcp["works_pct"] == 60 and pro_mcp["gotcha_pct"] == 32
    # flash group still has gate-OFF (Pro doesn't)
    assert any(c["tier"] == "flash" and c["condition"].startswith("plugin gate-OFF") for c in cells)


def test_default_call_unchanged_shape_plus_time():
    s = _server()
    out = s.get_summary("v2-fileoutput", "all", "newest")  # no pro_run_ids
    # backward compatible: single deepseek group (tier None), but time fields now present
    assert any(c["backend"] == "deepseek" and c["tier"] is None for c in out["cells"])
    assert all("time_median_s" in c for c in out["cells"])
