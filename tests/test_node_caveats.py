"""node_caveats_note must surface n8n's curated `/// warning` admonitions for the
nodes ACTUALLY USED in a workflow — deprecations, free-plan limits, queue-mode/SSL
caveats, etc. These are real user-facing gotchas the validator can never catch.

Signal = the `/// warning | Title ... ///` admonition block only. Incidental substring
matches ("unimportant", doc-lint "<!-- many -->" comments, a "Warninglist" operation)
must NOT be surfaced."""
import importlib.util
import os
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.abspath(os.path.join(_HERE, "..", "hooks", "lib", "nodes_db_inject.py"))
sys.path.insert(0, os.path.dirname(_PATH))  # let nodes_db_inject import sibling plugin_config
_spec = importlib.util.spec_from_file_location("nodes_db_inject", _PATH)
ndi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ndi)


def _make_db(rows):
    """rows: list of (node_type, display_name, documentation)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE nodes (node_type TEXT, display_name TEXT, documentation TEXT)")
    c.executemany("INSERT INTO nodes VALUES (?,?,?)", rows)
    c.commit()
    c.close()
    return path


# ---- _parse_warning_admonitions -------------------------------------------------

def test_parse_single_admonition_title_and_body():
    doc = ("Some intro.\n\n/// warning | Supported Figma Plans\n"
           "Figma doesn't support webhooks on the free \"Starter\" plan.\n///\n\nMore text.")
    out = ndi._parse_warning_admonitions(doc)
    assert len(out) == 1
    title, body = out[0]
    assert title == "Supported Figma Plans"
    assert "free \"Starter\" plan" in body


def test_parse_handles_pipe_on_next_line():
    # observed nodes.db shape: title pipe is on the line after `/// warning`
    doc = "/// warning\n| SSL\nThis node doesn't support SSL.\n///"
    out = ndi._parse_warning_admonitions(doc)
    assert out == [("SSL", "This node doesn't support SSL.")]


def test_parse_multiple_blocks():
    doc = ("/// warning | First\naaa\n///\n"
           "/// note | ignored\nxxx\n///\n"
           "/// warning | Second\nbbb\n///")
    titles = [t for t, _ in ndi._parse_warning_admonitions(doc)]
    assert titles == ["First", "Second"]


def test_parse_returns_empty_without_admonition():
    # "unimportant" / doc-lint comments / operation names are NOT admonitions
    for doc in ["ignore unimportant details",
                '<!-- "Many" triggers warnings -->',
                "Warninglist  Get  Get All",
                None, ""]:
        assert ndi._parse_warning_admonitions(doc) == []


def test_clean_md_strips_links_and_attrs():
    s = ("View the [end of service announcement]"
         "(https://notify-bot.line.me/closing){:target=_blank .external-link} now.")
    assert ndi._clean_md(s) == "View the end of service announcement now."


# ---- node_caveats_note ----------------------------------------------------------

def test_caveats_note_surfaces_used_node_warning():
    path = _make_db([
        ("nodes-base.line", "LINE",
         "/// warning | Deprecated: End of service\nLINE Notify is discontinuing service.\n///"),
        ("nodes-base.set", "Edit Fields", "No warnings here."),
    ])
    wf = {"nodes": [{"type": "n8n-nodes-base.line"}, {"type": "n8n-nodes-base.set"}]}
    note = ndi.node_caveats_note(wf, db_path=path)
    assert "LINE" in note
    assert "Deprecated: End of service" in note
    assert "discontinuing service" in note
    assert "Edit Fields" not in note  # node with no admonition is omitted


def test_caveats_note_none_when_no_used_node_has_warning():
    path = _make_db([("nodes-base.set", "Edit Fields", "Plain docs, no admonition.")])
    assert ndi.node_caveats_note({"nodes": [{"type": "n8n-nodes-base.set"}]}, db_path=path) is None


def test_caveats_note_ignores_unused_nodes():
    path = _make_db([
        ("nodes-base.line", "LINE", "/// warning | Deprecated\ngone\n///"),
        ("nodes-base.set", "Edit Fields", "plain"),
    ])
    # workflow uses only Edit Fields -> the LINE warning must not leak in
    assert ndi.node_caveats_note({"nodes": [{"type": "n8n-nodes-base.set"}]}, db_path=path) is None


def test_caveats_note_truncates_long_body():
    long_body = "x" * 1000
    path = _make_db([("nodes-base.line", "LINE", f"/// warning | T\n{long_body}\n///")])
    note = ndi.node_caveats_note({"nodes": [{"type": "n8n-nodes-base.line"}]}, db_path=path)
    assert "x" * 1000 not in note  # body capped
    assert "…" in note or "..." in note
