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


# ---------------------------------------------------------------------------
# Fixture: build a fake result tree
# ---------------------------------------------------------------------------

WORKFLOW = {"nodes": [{"name": "Slack", "type": "n8n-nodes-base.slack", "parameters": {}}], "connections": {}}


def make_result_tree(tmp_path, condition="cond-a", fileidx=0, run="run01",
                     validated=True, candidate=False, written=False,
                     validation=True, valid=True):
    """Create <tmp>/<condition>/prompt-NNN-runMM.* artifacts; return result dir."""
    cond = tmp_path / condition
    cond.mkdir(parents=True, exist_ok=True)
    stem = f"prompt-{fileidx:03d}-{run}"
    (cond / f"{stem}.meta.json").write_text(json.dumps(
        {"condition": condition, "prompt_idx": fileidx, "run": 1}))
    if validation:
        (cond / f"{stem}.validation.json").write_text(json.dumps(
            {"valid": valid, "error_count": 0 if valid else 3, "warning_count": 1,
             "enrichment_mode": "plugin",
             "validated_file": "/Users/secret/abs/path.json"}))
    if validated:
        (cond / f"{stem}.validated.workflow.json").write_text(json.dumps(WORKFLOW))
    if candidate:
        (cond / f"{stem}.candidate.workflow.json").write_text(json.dumps(WORKFLOW))
    if written:
        wdir = cond / f"{stem}.workflow"
        wdir.mkdir(exist_ok=True)
        (wdir / "my-flow.json").write_text(json.dumps(WORKFLOW))
    return tmp_path


def write_jsonl(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


GT = [
    {"id": "p0", "prompt": "post a message to slack", "group": "a"},
    {"id": "p1", "prompt": "send an email via gmail", "group": "b"},
]
RULES = [{"prompt_idx": 1, "prompt_id": "p1", "check_type": "llm_only",
          "gotcha": "Gmail node breaks on X", "workaround": "Use HTTP Request with Bearer auth"}]


class TestGathering:
    def test_load_ground_truth_by_line_index(self, tmp_path):
        gt_file = tmp_path / "gt.jsonl"
        write_jsonl(gt_file, GT)
        gt = jr.load_ground_truth(str(gt_file))
        assert gt[0]["prompt"] == "post a message to slack"
        assert gt[1]["id"] == "p1"

    def test_load_by_prompt_idx(self, tmp_path):
        f = tmp_path / "rules.jsonl"
        write_jsonl(f, RULES)
        rules = jr.load_by_prompt_idx(str(f))
        assert rules[1]["gotcha"] == "Gmail node breaks on X"
        assert jr.load_by_prompt_idx(str(tmp_path / "missing.jsonl")) == {}

    def test_workflow_prefers_validated(self, tmp_path):
        root = make_result_tree(tmp_path, validated=True, candidate=True, written=True)
        text, source = jr.find_workflow(str(root / "cond-a"), "prompt-000-run01")
        assert source == "validated" and json.loads(text)["nodes"]

    def test_workflow_falls_back_to_candidate(self, tmp_path):
        root = make_result_tree(tmp_path, validated=False, candidate=True, written=True)
        _, source = jr.find_workflow(str(root / "cond-a"), "prompt-000-run01")
        assert source == "candidate"

    def test_workflow_falls_back_to_written(self, tmp_path):
        root = make_result_tree(tmp_path, validated=False, candidate=False, written=True)
        _, source = jr.find_workflow(str(root / "cond-a"), "prompt-000-run01")
        assert source == "written"

    def test_written_prefers_meta_workflow_filename(self, tmp_path):
        root = make_result_tree(tmp_path, validated=False, written=True)
        cond = root / "cond-a"
        # a lexicographically-earlier decoy that must NOT be picked
        (cond / "prompt-000-run01.workflow" / "aaa-decoy.json").write_text(
            json.dumps({"nodes": [], "connections": {}}))
        meta_path = cond / "prompt-000-run01.meta.json"
        meta = json.loads(meta_path.read_text())
        meta["workflow_filename"] = "my-flow.json"
        meta_path.write_text(json.dumps(meta))
        text, source = jr.find_workflow(str(cond), "prompt-000-run01")
        assert source == "written"
        assert json.loads(text)["nodes"], "must pick meta's workflow_filename, not the decoy"

    def test_workflow_missing(self, tmp_path):
        root = make_result_tree(tmp_path, validated=False)
        text, source = jr.find_workflow(str(root / "cond-a"), "prompt-000-run01")
        assert text is None and source == "missing"

    def test_validation_summary_extracts_only_safe_fields(self, tmp_path):
        root = make_result_tree(tmp_path)
        s = jr.load_validation_summary(str(root / "cond-a"), "prompt-000-run01")
        assert s == {"valid": True, "error_count": 0, "warning_count": 1}

    def test_gather_input_full(self, tmp_path):
        root = make_result_tree(tmp_path, fileidx=1)
        gt = {i: e for i, e in enumerate(GT)}
        rules = {1: RULES[0]}
        ji = jr.gather_input(str(root / "cond-a"), 1, "run01", gt, rules, {})
        assert ji.prompt_text == "send an email via gmail"
        assert ji.workflow_source == "validated"
        assert ji.gotcha["workaround"].startswith("Use HTTP Request")
        assert ji.criteria is None

    def test_discover_results(self, tmp_path):
        root = make_result_tree(tmp_path, fileidx=0)
        make_result_tree(tmp_path, fileidx=3)
        found = jr.discover_results(str(root / "cond-a"))
        assert found == [(0, "prompt-000-run01"), (3, "prompt-003-run01")]


def _ji(**kw):
    base = dict(fileidx=1, stem="prompt-001-run01",
                prompt_text="send an email via gmail",
                workflow_text=json.dumps(WORKFLOW), workflow_source="validated",
                validation={"valid": True, "error_count": 0, "warning_count": 1},
                gotcha=None, criteria=None)
    base.update(kw)
    return jr.JudgeInput(**base)


class TestBuildPrompt:
    def test_contains_request_and_workflow(self):
        p = jr.build_prompt(_ji())
        assert "send an email via gmail" in p
        assert "n8n-nodes-base.slack" in p

    def test_blinded_no_provenance(self):
        p = jr.build_prompt(_ji())
        low = p.lower()
        for leak in ("plugin", "n8n-mcp", "mcp", "deepseek", "sonnet",
                     "condition", "/users/", "enrichment"):
            assert leak not in low, f"provenance leak: {leak}"

    def test_validity_does_not_imply_intent_line(self):
        assert "does not imply" in jr.build_prompt(_ji()).lower()

    def test_gotcha_section_when_rule_present(self):
        p = jr.build_prompt(_ji(gotcha=RULES[0]))
        assert "Gmail node breaks on X" in p
        assert "Use HTTP Request with Bearer auth" in p

    def test_no_gotcha_section_without_rule(self):
        p = jr.build_prompt(_ji())
        assert "not_applicable" in p  # instructed default
        assert "Known gotcha" not in p

    def test_checklist_mode_lists_criteria(self):
        crit = {"prompt_idx": 1, "must": ["uses gmail or http", "sends to recipient"],
                "nice": ["retries on failure"]}
        p = jr.build_prompt(_ji(criteria=crit))
        assert "uses gmail or http" in p and "retries on failure" in p
        assert '"criteria"' in p  # response schema mentions criteria array

    def test_json_only_instruction(self):
        assert "single JSON object" in jr.build_prompt(_ji())

    def test_checklist_states_must_rollup_rule(self):
        crit = {"prompt_idx": 1, "must": ["uses gmail or http"], "nice": []}
        p = jr.build_prompt(_ji(criteria=crit))
        low = p.lower()
        assert "every [must] criterion" in low
        assert "[nice]" in p  # the rule must mention nice never affects intent_fit

    def test_checklist_criterion_text_excludes_tag(self):
        crit = {"prompt_idx": 1, "must": ["uses gmail or http"], "nice": ["retries"]}
        p = jr.build_prompt(_ji(criteria=crit))
        assert "without the [must]/[nice] tag" in p

    def test_checklist_empty_nice_renders_musts_only(self):
        crit = {"prompt_idx": 1, "must": ["uses gmail or http"], "nice": []}
        p = jr.build_prompt(_ji(criteria=crit))
        assert "- [must] uses gmail or http" in p
        assert "- [nice]" not in p

    def test_gotcha_schema_line_flips_to_pass_fail(self):
        p = jr.build_prompt(_ji(gotcha=RULES[0]))
        assert '"gotcha_handled": "pass" or "fail"' in p
        assert "not_applicable" not in p
