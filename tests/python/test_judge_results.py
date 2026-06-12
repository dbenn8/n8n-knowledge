"""Unit tests for scripts/eval/judge_results.py (LLM judge)."""
from __future__ import annotations

import json

import pytest

import judge_results as jr


GOOD = {
    "intent_fit": "pass",
    "intent_reasoning": "Slack node n8n-nodes-base.slack posts to #general",
    "gotcha_handled": "not_applicable",
    "gotcha_reasoning": "no known gotcha for this prompt",
    "confidence": "high",
}


class TestParseVerdict:
    def test_clean_json(self):
        assert jr.parse_verdict(json.dumps(GOOD))["intent_fit"] == "pass"

    def test_fenced_json(self):
        text = "```json\n" + json.dumps(GOOD) + "\n```"
        assert jr.parse_verdict(text)["intent_fit"] == "pass"

    def test_prose_wrapped_json(self):
        text = "Here is my verdict:\n" + json.dumps(GOOD) + "\nHope that helps!"
        assert jr.parse_verdict(text)["confidence"] == "high"

    def test_malformed_raises(self):
        with pytest.raises(jr.VerdictParseError):
            jr.parse_verdict("I cannot produce JSON today.")

    def test_truncated_json_raises(self):
        with pytest.raises(jr.VerdictParseError):
            jr.parse_verdict('{"intent_fit": "pass", "intent_re')


class TestValidateVerdict:
    def test_good_binary(self):
        assert jr.validate_verdict(GOOD, checklist_mode=False) == []

    def test_missing_key(self):
        bad = {k: v for k, v in GOOD.items() if k != "gotcha_handled"}
        assert any("gotcha_handled" in e for e in jr.validate_verdict(bad, checklist_mode=False))

    def test_bad_enum(self):
        bad = dict(GOOD, intent_fit="maybe")
        assert any("intent_fit" in e for e in jr.validate_verdict(bad, checklist_mode=False))

    def test_checklist_requires_criteria(self):
        assert any("criteria" in e for e in jr.validate_verdict(GOOD, checklist_mode=True))

    def test_checklist_criteria_shape(self):
        v = dict(GOOD, criteria=[{"criterion": "routes by tier", "met": True}])
        assert jr.validate_verdict(v, checklist_mode=True) == []
        v_bad = dict(GOOD, criteria=[{"criterion": "routes by tier"}])
        assert any("met" in e for e in jr.validate_verdict(v_bad, checklist_mode=True))

    def test_checklist_non_dict_criterion_reports_not_raises(self):
        v = dict(GOOD, criteria=["not a dict"])
        errors = jr.validate_verdict(v, checklist_mode=True)
        assert any("criteria[0]" in e for e in errors)

    def test_checklist_empty_list_rejected(self):
        v = dict(GOOD, criteria=[])
        assert any("criteria" in e for e in jr.validate_verdict(v, checklist_mode=True))
