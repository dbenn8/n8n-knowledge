"""Unit tests for forum-handle redaction in synthesized observation prose.

Machine-distilled "observation" results sometimes embed community members'
forum usernames in prose (e.g. "user Chrisyk resolved...",
"Julia_Solias_Huelamo reports...").

DESIGN (Dan, 2026-06-11 — supersedes the regex era): redact by EXACT
REPLACEMENT OF KNOWN AUTHOR NAMES read from each observation's own source
facts' metadata. Three iterations of regex-based redaction kept corrupting
technical prose (Split_In_Batches, Basic_Auth, Mark_as_Read, Loop_Over_Items,
Set_Node became "a community user"), so the regex-guessing machinery was
deleted. An observation's source facts already carry the real author usernames
in ``metadata.username``; the source-facts plumbing delivers those facts for
every observation on every channel via the one merged top-level ``source_facts``
dict. Redaction is now metadata-driven, deterministic (exact, case-insensitive
name replacement), and scoped per-observation. A name that is not a known
source author is NEVER touched, so technical vocabulary can no longer be
corrupted. Observations whose source facts are absent get no redaction —
under-redaction of public names is the accepted failure mode.

These tests cover the metadata-driven helpers
(``collect_source_usernames``, ``redact_known_handles``,
``redact_preserving_urls``) and their wiring into ``render_result`` for the
observation branch only. Raw fact results keep their text untouched (names
there are attribution); source/citation lines and URLs are never modified.
"""

from __future__ import annotations

from format_results import (
    collect_source_usernames,
    redact_known_handles,
    redact_preserving_urls,
    render_result,
)


# ---------------------------------------------------------------------------
# Source-fact fixtures — shapes mirror tests/python/test_source_facts_flow.py:
# a fact is a dict with ``tags`` and ``metadata``; the author's forum username
# lives in ``metadata.username``.
# ---------------------------------------------------------------------------

def _fact(username, url=None, **meta):
    md = {"username": username}
    if url:
        md["url"] = url
    md.update(meta)
    return {"tags": ["source:discourse"], "metadata": md}


# ---------------------------------------------------------------------------
# collect_source_usernames — author name extraction from source facts
# ---------------------------------------------------------------------------

def test_collect_names_from_source_facts():
    r = {"source_fact_ids": ["f1", "f2"]}
    source_facts = {
        "f1": _fact("Chrisyk"),
        "f2": _fact("Julia_Solias_Huelamo"),
    }
    assert collect_source_usernames(r, source_facts) == {
        "Chrisyk",
        "Julia_Solias_Huelamo",
    }


def test_collect_names_skips_short_usernames():
    # 1-2 char names would shred prose on substring-ish word matches, so they
    # are skipped entirely.
    r = {"source_fact_ids": ["f1", "f2", "f3"]}
    source_facts = {
        "f1": _fact("Jo"),       # 2 chars -> skipped
        "f2": _fact("A"),        # 1 char  -> skipped
        "f3": _fact("Chrisyk"),  # kept
    }
    assert collect_source_usernames(r, source_facts) == {"Chrisyk"}


def test_collect_names_no_source_facts():
    assert collect_source_usernames({}, {}) == set()
    assert collect_source_usernames({"source_fact_ids": ["x"]}, {}) == set()


def test_collect_names_missing_username_metadata():
    r = {"source_fact_ids": ["f1"]}
    source_facts = {"f1": {"tags": ["source:discourse"], "metadata": {"url": "u"}}}
    assert collect_source_usernames(r, source_facts) == set()


# ---------------------------------------------------------------------------
# redact_known_handles — exact, case-insensitive, whole-word replacement
# ---------------------------------------------------------------------------

# PIN (must redact): known author, with and without "user " prefix.

def test_redacts_known_author_bare():
    assert (
        redact_known_handles("Chrisyk resolved the duplicate trigger problem",
                             {"Chrisyk"})
        == "a community user resolved the duplicate trigger problem"
    )


def test_redacts_known_author_with_user_prefix():
    # The optional leading "user " is absorbed into the replacement (no dupe).
    assert (
        redact_known_handles("user Chrisyk resolved the issue", {"Chrisyk"})
        == "a community user resolved the issue"
    )


def test_redacts_underscore_author():
    assert (
        redact_known_handles("Julia_Solias_Huelamo reports Merge Node losing lines",
                             {"Julia_Solias_Huelamo"})
        == "a community user reports Merge Node losing lines"
    )


def test_redacts_case_insensitive():
    # Prose lowercases the handle ("chrisyk"); the known author is "Chrisyk".
    assert (
        redact_known_handles("the fix from chrisyk worked", {"Chrisyk"})
        == "the fix from a community user worked"
    )


def test_redacts_possessive_form():
    # The trailing \b allows a possessive 's to remain after replacement.
    assert (
        redact_known_handles("Chrisyk's workflow broke on update", {"Chrisyk"})
        == "a community user's workflow broke on update"
    )


def test_redacts_multiple_authors_in_one_text():
    out = redact_known_handles(
        "Julia_Solias_Huelamo reported it and Chrisyk confirmed the fix",
        {"Julia_Solias_Huelamo", "Chrisyk"},
    )
    assert "Julia_Solias_Huelamo" not in out
    assert "Chrisyk" not in out
    assert out.count("a community user") == 2


def test_empty_names_leaves_text_unchanged():
    text = "Chrisyk resolved it"
    assert redact_known_handles(text, set()) == text


# PIN (must NEVER change): a name that is NOT a known source author is never
# touched. This is the whole point of the metadata-driven design — only the
# observation's own authors are candidates. We pass a foreign name set so any
# accidental substring/heuristic matching would fire and fail the test.

def test_unknown_name_not_redacted():
    text = "Chrisyk resolved it"
    assert redact_known_handles(text, {"SomeoneElse"}) == text


# ---------------------------------------------------------------------------
# Technical-prose corpus from the regex era — every confirmed corruption case.
#
# These are trivially safe now (none of these tokens is a known source author),
# but we PIN them so a future regex resurrection that re-introduces guessing
# fails loudly. The candidate name set deliberately includes a plausible author
# ("Chrisyk") to prove that having SOME names active still leaves non-author
# technical vocabulary byte-identical.
# ---------------------------------------------------------------------------

_TECH_PROSE = [
    "Use Split_In_Batches to chunk the incoming items",
    "switch from Basic_Auth to OAuth2",
    "fixed by Mark_as_Read handler",
    "per Loop_Over_Items iteration",
    "Set_Node writes the field",
    "user Agent_Tool is configured",  # Agent_Tool is NOT a source author
    "America/New_York reports the correct offset",
    "Use Loop Over Items with Google Sheets and HTTP Request",
    "Merge node in Combine mode silently loses rows when branches mismatch",
    "see https://community.n8n.io/t/some-thread/123 for details",
]


def test_technical_prose_never_corrupted_by_known_handles():
    names = {"Chrisyk"}  # a real author is active, but appears in none of these
    for text in _TECH_PROSE:
        assert redact_known_handles(text, names) == text, f"mangled: {text!r}"


def test_technical_prose_never_corrupted_via_url_wrapper():
    names = {"Chrisyk"}
    for text in _TECH_PROSE:
        assert redact_preserving_urls(text, names) == text, f"mangled: {text!r}"


# ---------------------------------------------------------------------------
# redact_preserving_urls — per-span redaction, URLs byte-identical
# ---------------------------------------------------------------------------

def test_preserving_urls_redacts_around_url():
    text = (
        "Julia_Solias_Huelamo reports the bug at "
        "https://community.n8n.io/t/x/1 and Chrisyk confirmed it"
    )
    out = redact_preserving_urls(text, {"Julia_Solias_Huelamo", "Chrisyk"})
    assert "Julia_Solias_Huelamo" not in out
    # The prose mention of Chrisyk is gone...
    assert "and Chrisyk confirmed" not in out
    assert out.count("a community user") == 2
    # ...and the URL is byte-identical.
    assert "https://community.n8n.io/t/x/1" in out


def test_preserving_urls_keeps_username_in_url_byte_identical():
    # A URL may legitimately contain /u/<username>; the prose mention is
    # replaced but the URL path stays byte-identical.
    text = "Chrisyk posted the fix at https://community.n8n.io/u/Chrisyk/activity"
    out = redact_preserving_urls(text, {"Chrisyk"})
    # Prose mention redacted.
    assert out.startswith("a community user posted the fix at ")
    # URL untouched — still contains /u/Chrisyk.
    assert "https://community.n8n.io/u/Chrisyk/activity" in out


def test_preserving_urls_leading_url():
    text = "https://community.n8n.io/t/x/1 was shared by Victor_Tong"
    out = redact_preserving_urls(text, {"Victor_Tong"})
    assert out.startswith("https://community.n8n.io/t/x/1")
    assert "Victor_Tong" not in out
    assert "a community user" in out


def test_preserving_urls_no_names_returns_unchanged():
    text = "Julia_Solias_Huelamo reports the Merge Node bug"
    assert redact_preserving_urls(text, set()) == text


# ---------------------------------------------------------------------------
# Author-name / node-vocabulary collision (documented behavior).
#
# Names shorter than 3 chars are skipped (see collect_source_usernames). For an
# exact collision where a real author's username equals a node-vocabulary token
# (e.g. metadata.username == "Merge"), redaction WILL fire — it is a real author
# name. This is possible but rare, and scoping the candidate set to the
# observation's OWN authors bounds the blast radius: a "Merge" author only
# affects prose in observations that author actually wrote.
# ---------------------------------------------------------------------------

def test_author_name_colliding_with_node_vocab_is_redacted():
    # Author username happens to be "Merge" — a node name. By design this fires.
    assert (
        redact_known_handles("Merge reported the rows-lost bug", {"Merge"})
        == "a community user reported the rows-lost bug"
    )
    # But "Merge" is NOT redacted in an observation where it is not an author:
    text = "Merge node in Combine mode loses rows"
    assert redact_known_handles(text, {"Chrisyk"}) == text


# ---------------------------------------------------------------------------
# render_result — observation branch wiring
# ---------------------------------------------------------------------------

# Minimal cfg covering the keys render_result reads. max_text_length_high is
# -1 so truncation is disabled and we test redaction in isolation.
_CFG = {
    "max_text_length_high": -1,
    "max_text_length_medium": -1,
    "max_text_length_low": -1,
}


def _obs_result():
    return {
        "type": "observation",
        "text": "user Chrisyk fixed it by reconnecting the Merge node inputs",
        "tags": ["source:discourse"],
        "metadata": {},
        "source_fact_ids": ["f1"],
    }


def _raw_fact_result():
    return {
        "type": "memory",
        "text": "user Chrisyk fixed it by reconnecting the Merge node inputs",
        "tags": ["source:discourse"],
        "metadata": {"url": "https://community.n8n.io/t/merge-loses-rows/999"},
    }


def test_observation_render_redacts_known_author():
    r = _obs_result()
    source_facts = {
        "f1": _fact("Chrisyk", url="https://community.n8n.io/t/merge-loses-rows/999",
                    views="1200", like_count="3"),
    }
    sf_pairs = [("https://community.n8n.io/t/merge-loses-rows/999",
                 source_facts["f1"])]
    block = render_result(1, r, "HIGH", True, sf_pairs, _CFG, source_facts)
    assert "Chrisyk" not in block.split("sources:")[0]  # prose has no Chrisyk
    assert "a community user" in block
    # The source URL must survive untouched.
    assert "https://community.n8n.io/t/merge-loses-rows/999" in block


def test_observation_render_no_source_facts_text_unchanged():
    # No resolvable source facts -> no candidate names -> text unchanged.
    # Under-redaction of public names is the accepted failure mode.
    r = _obs_result()
    block = render_result(1, r, "HIGH", True, [], _CFG, {})
    assert "Chrisyk" in block


def test_observation_render_struct_channel_threads_source_facts():
    # gotcha/struct-channel observations arrive via the same merged top-level
    # source_facts dict; render_result must redact them too. Here the URL is
    # resolved separately (sf_pairs) but the names come from source_facts.
    r = {
        "type": "observation",
        "text": "Julia_Solias_Huelamo reports the Merge node loses rows",
        "tags": ["type:gotcha", "source:discourse"],
        "metadata": {},
        "source_fact_ids": ["g1"],
    }
    source_facts = {"g1": _fact("Julia_Solias_Huelamo")}
    block = render_result(1, r, "HIGH", True, [], _CFG, source_facts)
    assert "Julia_Solias_Huelamo" not in block
    assert "a community user" in block


def test_raw_fact_render_keeps_handle():
    r = _raw_fact_result()
    block = render_result(1, r, "HIGH", False, [], _CFG, {})
    # Raw facts keep the name (attribution) — never redacted even if a matching
    # name set were present.
    assert "Chrisyk" in block


def test_raw_fact_render_never_redacted_even_with_names():
    # obs=False short-circuits redaction entirely; the name is attribution.
    r = _raw_fact_result()
    source_facts = {"f1": _fact("Chrisyk")}
    block = render_result(1, r, "HIGH", False, [], _CFG, source_facts)
    assert "Chrisyk" in block
