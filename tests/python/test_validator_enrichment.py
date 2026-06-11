"""Unit tests for warning rendering and deterministic spec-slot ordering.

Covers:
  * validator_enrichment.build_warnings_block — non-blocking warning block with
    per-node attribution, dedup, a 5-item cap, and an overflow marker.
  * nodes_db_inject.order_error_node_types — deterministic, error-count-weighted
    ordering of error node types (ties broken by first appearance), plus the
    no-error-node fallback returning an empty list.
  * nodes_db_inject.build_cheatsheet — the omission marker appended when the node
    cap drops schemas.
"""

from __future__ import annotations

import nodes_db_inject as ndi
from validator_enrichment import build_warnings_block


# ---------------------------------------------------------------------------
# build_warnings_block
# ---------------------------------------------------------------------------

def test_no_warnings_returns_empty_string():
    assert build_warnings_block({"warnings": []}) == ""
    assert build_warnings_block({}) == ""


def test_warnings_block_has_header_and_bullets_with_attribution():
    validation = {
        "warnings": [
            {"type": "warning", "message": "Field X is deprecated", "node": "Slack"},
            {"type": "warning", "message": "Consider setting Y", "node": "HTTP Request"},
        ]
    }
    block = build_warnings_block(validation)
    assert "Warnings (non-blocking — review before finalizing):" in block
    assert "Field X is deprecated" in block
    assert "Consider setting Y" in block
    # Per-node attribution
    assert "Slack" in block
    assert "HTTP Request" in block


def test_warnings_block_dedupes_identical_message_and_node():
    validation = {
        "warnings": [
            {"type": "warning", "message": "Same warning", "node": "Slack"},
            {"type": "warning", "message": "Same warning", "node": "Slack"},
        ]
    }
    block = build_warnings_block(validation)
    assert block.count("Same warning") == 1


def test_warnings_block_keeps_same_message_for_distinct_nodes():
    validation = {
        "warnings": [
            {"type": "warning", "message": "Same warning", "node": "A"},
            {"type": "warning", "message": "Same warning", "node": "B"},
        ]
    }
    block = build_warnings_block(validation)
    assert block.count("Same warning") == 2


def test_warnings_block_caps_at_five_with_overflow_marker():
    validation = {
        "warnings": [
            {"type": "warning", "message": f"warning number {i}", "node": f"N{i}"}
            for i in range(8)
        ]
    }
    block = build_warnings_block(validation, max_warnings=5)
    # Only 5 bullet messages rendered
    rendered = sum(1 for i in range(8) if f"warning number {i}" in block)
    assert rendered == 5
    assert "(+3 more warnings)" in block


def test_warnings_block_no_overflow_marker_at_or_under_cap():
    validation = {
        "warnings": [
            {"type": "warning", "message": f"w{i}", "node": f"N{i}"} for i in range(5)
        ]
    }
    block = build_warnings_block(validation, max_warnings=5)
    assert "more warnings" not in block


# ---------------------------------------------------------------------------
# order_error_node_types — deterministic, error-count weighted
# ---------------------------------------------------------------------------

def _six_node_workflow():
    return {
        "nodes": [
            {"name": "Slack", "type": "n8n-nodes-base.slack"},
            {"name": "Notion", "type": "n8n-nodes-base.notion"},
            {"name": "HTTP", "type": "n8n-nodes-base.httpRequest"},
            {"name": "Set", "type": "n8n-nodes-base.set"},
            {"name": "IF", "type": "n8n-nodes-base.if"},
            {"name": "Sheets", "type": "n8n-nodes-base.googleSheets"},
        ]
    }


def _result_with_counts():
    # Notion has 3 errors, Slack 2, Sheets 1, HTTP 1.
    return {
        "issues": [
            {"node": "Notion", "message": "e1"},
            {"node": "Notion", "message": "e2"},
            {"node": "Notion", "message": "e3"},
            {"node": "Slack", "message": "e4"},
            {"node": "Slack", "message": "e5"},
            {"node": "Sheets", "message": "e6"},
            {"node": "HTTP", "message": "e7"},
        ]
    }


def test_ordering_is_error_count_weighted():
    wf = _six_node_workflow()
    res = _result_with_counts()
    ordered = ndi.order_error_node_types(wf, res)
    # Notion (3) first, then Slack (2), then HTTP and Sheets (1 each, tie broken
    # by first appearance in nodes array: HTTP before Sheets).
    assert ordered == [
        "n8n-nodes-base.notion",
        "n8n-nodes-base.slack",
        "n8n-nodes-base.httpRequest",
        "n8n-nodes-base.googleSheets",
    ]


def test_ordering_is_deterministic_across_runs():
    wf = _six_node_workflow()
    res = _result_with_counts()
    first = ndi.order_error_node_types(wf, res)
    second = ndi.order_error_node_types(wf, res)
    assert first == second


def test_ordering_no_error_nodes_returns_empty():
    # P2#9 fix: when no error node can be identified, return nothing (skip
    # spec injection) rather than ALL workflow nodes.
    wf = _six_node_workflow()
    res = {"issues": []}
    assert ndi.order_error_node_types(wf, res) == []


def test_ordering_catches_node_type_in_message():
    wf = _six_node_workflow()
    res = {"issues": [{"node": None, "message": "problem in n8n-nodes-base.notion config"}]}
    ordered = ndi.order_error_node_types(wf, res)
    assert ordered == ["n8n-nodes-base.notion"]


# ---------------------------------------------------------------------------
# build_cheatsheet — omission marker
# ---------------------------------------------------------------------------

def test_cheatsheet_appends_omission_marker_when_capped(tmp_path):
    import sqlite3

    db_path = tmp_path / "nodes.db"
    db = sqlite3.connect(str(db_path))
    db.execute(
        "CREATE TABLE nodes (node_type TEXT, display_name TEXT, version INTEGER, "
        "operations TEXT, properties_schema TEXT)"
    )
    ops = '[{"resource":"r","operation":"get"}]'
    for i in range(7):
        db.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?)",
            (f"nodes-base.node{i}", f"Node {i}", 1, ops, "[]"),
        )
    db.commit()
    db.close()

    node_types = [f"nodes-base.node{i}" for i in range(7)]
    sheet = ndi.build_cheatsheet(node_types, db_path=str(db_path))
    assert sheet is not None
    # 7 requested, _MAX_NODES caps at 5 -> 2 omitted.
    assert "(+2 more node schemas omitted" in sheet


def test_cheatsheet_no_marker_when_all_fit(tmp_path):
    import sqlite3

    db_path = tmp_path / "nodes.db"
    db = sqlite3.connect(str(db_path))
    db.execute(
        "CREATE TABLE nodes (node_type TEXT, display_name TEXT, version INTEGER, "
        "operations TEXT, properties_schema TEXT)"
    )
    ops = '[{"resource":"r","operation":"get"}]'
    for i in range(3):
        db.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?)",
            (f"nodes-base.node{i}", f"Node {i}", 1, ops, "[]"),
        )
    db.commit()
    db.close()

    node_types = [f"nodes-base.node{i}" for i in range(3)]
    sheet = ndi.build_cheatsheet(node_types, db_path=str(db_path))
    assert sheet is not None
    assert "node schemas omitted" not in sheet
