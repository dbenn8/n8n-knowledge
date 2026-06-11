"""Unit tests for source-fact provenance rendering in synthesized observations.

SCENE (diagnosed live 2026-06-11): Hindsight observations are syntheses
consolidated from many source facts. The recall API returns full source-fact
data ONLY when the request body includes ``"include": {"source_facts": {}}``.
Once the gotcha/structured channels also request that flag (and auto-recall.sh
preserves the merged ``source_facts`` dict across both merge steps), an
observation arriving via the gotcha channel carries its provenance.

Two rendering decisions (Dan, 2026-06-11):
  1. The ``sources="N"`` attribute on the synthesis open-tag must reflect TOTAL
     consolidation strength — the count of ``source_fact_ids`` — not just the
     (≤3) resolved links shown. An observation built from 24 sources should
     LOOK stronger than one built from 2, even when only 3 links resolve.
  2. When ``source_fact_ids`` exist but NONE resolve to a URL in this response
     (e.g. the links were not requested/returned), the body must say how many
     source facts back the observation and point at manual recall — NOT the
     legacy "unavailable" line, which implies zero provenance.

The legacy "unavailable" message survives ONLY when there are genuinely zero
``source_fact_ids``.
"""

from __future__ import annotations

from format_results import render_result


# Minimal cfg covering the keys render_result reads. Truncation disabled so we
# test sources/provenance rendering in isolation.
_CFG = {
    "max_text_length_high": -1,
    "max_text_length_medium": -1,
    "max_text_length_low": -1,
}


def _obs(num_ids, text="machine-distilled synthesis of the issue"):
    """Observation result with ``num_ids`` source_fact_ids and neutral prose."""
    return {
        "type": "observation",
        "text": text,
        "tags": ["source:discourse"],
        "metadata": {},
        "source_fact_ids": [f"sf-{i}" for i in range(num_ids)],
    }


def _resolvable_pairs(n):
    """n resolvable (url, fact) pairs as render_result expects from sf_pairs."""
    return [
        (
            f"https://community.n8n.io/t/thread/{i}",
            {"tags": ["source:discourse"], "metadata": {"views": "500"}},
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# sources="N" reflects TOTAL consolidation strength
# ---------------------------------------------------------------------------

def test_24_ids_3_resolvable_shows_total_24_and_3_links():
    r = _obs(24)
    sf_pairs = _resolvable_pairs(3)
    block = render_result(1, r, "HIGH", True, sf_pairs, _CFG)
    # Open-tag advertises the full consolidation strength.
    assert 'sources="24"' in block
    # But still only the first 3 resolved links are listed.
    assert "https://community.n8n.io/t/thread/0" in block
    assert "https://community.n8n.io/t/thread/1" in block
    assert "https://community.n8n.io/t/thread/2" in block
    # The "links not resolved" fallback must NOT appear when links resolved.
    assert "links not resolved" not in block


def test_sources_count_uses_resolved_when_ids_fewer():
    # Defensive: if source_fact_ids is somehow shorter than resolved pairs, the
    # larger (resolved) count wins so we never under-report visible links.
    r = _obs(2)
    sf_pairs = _resolvable_pairs(3)
    block = render_result(1, r, "HIGH", True, sf_pairs, _CFG)
    assert 'sources="3"' in block


# ---------------------------------------------------------------------------
# ids present but NONE resolve -> new "links not resolved" message with count
# ---------------------------------------------------------------------------

def test_24_ids_0_resolvable_shows_count_and_manual_recall_hint():
    r = _obs(24)
    block = render_result(1, r, "HIGH", True, [], _CFG)
    # Open-tag still reflects the 24-source strength.
    assert 'sources="24"' in block
    # Body explains the count and points at manual recall.
    assert "24 source facts" in block
    assert "links not resolved in this response" in block
    assert "manual recall" in block
    # The legacy zero-provenance message must NOT be used here.
    assert "use manual recall to find the original" not in block


# ---------------------------------------------------------------------------
# zero ids -> legacy "unavailable" message preserved
# ---------------------------------------------------------------------------

def test_zero_ids_uses_legacy_unavailable_message():
    r = _obs(0)
    block = render_result(1, r, "HIGH", True, [], _CFG)
    assert 'sources="0"' in block
    assert "sources: unavailable — use manual recall to find the original" in block
    # The new count-based message must NOT appear when there are no ids.
    assert "links not resolved in this response" not in block


# ---------------------------------------------------------------------------
# raw (non-observation) facts unchanged
# ---------------------------------------------------------------------------

def test_raw_fact_rendering_unchanged():
    r = {
        "type": "memory",
        "text": "Merge node loses rows in Combine mode",
        "tags": ["source:discourse"],
        "metadata": {"url": "https://community.n8n.io/t/merge/999"},
    }
    block = render_result(1, r, "HIGH", False, [], _CFG)
    assert 'kind="post"' in block
    # No synthesis sources attribute on raw facts.
    assert "sources=" not in block
    assert "https://community.n8n.io/t/merge/999" in block
