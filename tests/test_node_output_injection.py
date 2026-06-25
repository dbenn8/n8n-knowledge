"""build_cheatsheet must inject output ORDERING for multi-output nodes (the
splitInBatches/If/Filter done-loop reversal class), and must stay backward-compatible
with nodes.db builds that lack the output_names/outputs columns."""
import importlib.util
import os
import sqlite3
import tempfile

import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.abspath(os.path.join(_HERE, "..", "hooks", "lib", "nodes_db_inject.py"))
sys.path.insert(0, os.path.dirname(_PATH))  # let nodes_db_inject import sibling plugin_config
_spec = importlib.util.spec_from_file_location("nodes_db_inject", _PATH)
ndi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ndi)


def _make_db(with_output_cols=True):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    base = "node_type TEXT, display_name TEXT, version TEXT, operations TEXT, properties_schema TEXT"
    cols = base + (", output_names TEXT, outputs TEXT" if with_output_cols else "")
    c.execute(f"CREATE TABLE nodes ({cols})")
    return c, path


def test_multi_output_node_emits_ordered_outputs():
    c, path = _make_db()
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
              ("nodes-base.splitInBatches", "Split In Batches", "3", None, None,
               '["done","loop"]', '["done","loop"]'))
    c.commit()
    out = ndi.build_cheatsheet(["nodes-base.splitInBatches"], db_path=path)
    assert 'output index 0 = "done"' in out
    assert 'output index 1 = "loop"' in out


def test_if_node_true_false_ordering():
    c, path = _make_db()
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
              ("nodes-base.if", "If", "2", None, None, '["true","false"]', None))
    c.commit()
    out = ndi.build_cheatsheet(["nodes-base.if"], db_path=path)
    assert 'output index 0 = "true"' in out
    assert 'output index 1 = "false"' in out


def test_single_output_node_has_no_output_section():
    c, path = _make_db()
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
              ("nodes-base.set", "Edit Fields", "3", None, None, '["main"]', '["main"]'))
    c.commit()
    out = ndi.build_cheatsheet(["nodes-base.set"], db_path=path)
    assert "output index" not in (out or "")


def test_backward_compatible_when_output_columns_missing():
    c, path = _make_db(with_output_cols=False)
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?)",
              ("nodes-base.slack", "Slack", "2.4", None, None))
    c.commit()
    out = ndi.build_cheatsheet(["nodes-base.slack"], db_path=path)  # must not raise
    assert "Slack" in out
    assert "output index" not in out


def test_workflow_node_types_normalizes():
    wf = {"nodes": [
        {"type": "n8n-nodes-base.splitInBatches"},
        {"type": "@n8n/n8n-nodes-langchain.agent"},
        {"type": "n8n-nodes-base.set"},
    ]}
    assert ndi.workflow_node_types(wf) == [
        "nodes-base.splitInBatches", "nodes-langchain.agent", "nodes-base.set"]


def test_multi_output_note_for_model_added_node():
    c, path = _make_db()
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
              ("nodes-base.splitInBatches", "Split In Batches", "3", None, None, '["done","loop"]', None))
    c.commit()
    # node present in the WORKFLOW even though it was never in the prompt
    wf = {"nodes": [{"type": "n8n-nodes-base.splitInBatches", "name": "Loop"}]}
    note = ndi.multi_output_note(wf, db_path=path)
    assert "Split In Batches" in note
    assert 'index 0="done"' in note and 'index 1="loop"' in note


def test_multi_output_note_none_when_only_single_output_nodes():
    c, path = _make_db()
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
              ("nodes-base.set", "Edit Fields", "3", None, None, '["main"]', None))
    c.commit()
    assert ndi.multi_output_note({"nodes": [{"type": "n8n-nodes-base.set"}]}, db_path=path) is None


def _sib_db():
    c, path = _make_db()
    c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
              ("nodes-base.splitInBatches", "Split In Batches", "3", None, None, '["done","loop"]', None))
    c.commit()
    return path


def test_detect_reversed_loop_flags_loop_body_on_done():
    path = _sib_db()
    wf = {"nodes": [{"type": "n8n-nodes-base.splitInBatches", "name": "Loop"},
                    {"type": "n8n-nodes-base.set", "name": "Process"}],
          "connections": {
              "Loop": {"main": [[{"node": "Process"}], []]},   # output 0 (done) -> Process
              "Process": {"main": [[{"node": "Loop"}]]}}}       # Process loops back -> reversed
    issues = ndi.detect_reversed_loop(wf, db_path=path)
    assert len(issues) == 1 and issues[0]["node"] == "Loop"
    assert issues[0]["wrong_output"] == "done" and issues[0]["loop_output"] == "loop"


def test_detect_reversed_loop_ok_when_loop_body_on_loop():
    path = _sib_db()
    wf = {"nodes": [{"type": "n8n-nodes-base.splitInBatches", "name": "Loop"},
                    {"type": "n8n-nodes-base.set", "name": "Process"},
                    {"type": "n8n-nodes-base.set", "name": "Final"}],
          "connections": {
              "Loop": {"main": [[{"node": "Final"}], [{"node": "Process"}]]},  # done->Final, loop->Process
              "Process": {"main": [[{"node": "Loop"}]]}}}                       # loop body returns -> correct
    assert ndi.detect_reversed_loop(wf, db_path=path) == []


def test_detect_reversed_loop_ignores_non_looping_use():
    path = _sib_db()
    wf = {"nodes": [{"type": "n8n-nodes-base.splitInBatches", "name": "Loop"},
                    {"type": "n8n-nodes-base.set", "name": "A"}],
          "connections": {"Loop": {"main": [[{"node": "A"}], []]}}}  # nothing loops back
    assert ndi.detect_reversed_loop(wf, db_path=path) == []
