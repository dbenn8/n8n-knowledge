"""Regression test for server._common_idxs.

Bug (2026-06-25): a partially-covered condition (a 66/128 bare run, just over the old
ceil(0.5*universe)=64 bar) joined the common-set intersection and shrank it 128 -> 66,
silently recomputing every COMPLETE condition's published numbers over the smaller subset
(Claude plugin works 80 -> 74, pitfall 39 -> 27) even though no plugin run changed.

Fix: only (near-)complete cells (>= 90% of the best-covered cell) define the common set.
An incomplete cell is still reported over its own coverage (qualified by its `cover` count),
but can no longer shrink the basis for the complete cells.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))
import server  # noqa: E402


def _cell(label, n):
    return (label, {i: {"inst": 1} for i in range(n)})


def test_incomplete_cell_does_not_shrink_common():
    cells = [_cell("plugin", 128), _cell("mcp", 128), _cell("bare", 66)]
    assert len(server._common_idxs(cells)) == 128


def test_near_complete_cells_still_define_common():
    # A condition missing a couple prompts (126/128) still participates and constrains
    # the common set to what the complete cells share with it (the 126 they all cover).
    cells = [_cell("plugin", 128), _cell("mcp", 126)]
    assert len(server._common_idxs(cells)) == 126


def test_all_partial_fallback():
    # When every cell is partial (e.g. a gap-fill-only scope), fall back to intersecting
    # them all rather than returning nothing.
    cells = [_cell("a", 66), _cell("b", 66)]
    assert len(server._common_idxs(cells)) == 66


def test_empty():
    assert server._common_idxs([]) == []
    assert server._common_idxs([("x", {})]) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
