"""Unit tests for scripts/eval/judge_results.py (LLM judge)."""
from __future__ import annotations

import json
import os

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


class TestIsolation:
    def test_scratch_config_layout(self, tmp_path):
        creds = tmp_path / ".credentials.json"
        creds.write_text('{"claudeAiOauth": {}}')
        cfg = jr.make_scratch_config(creds_source=str(creds))
        try:
            mode = os.stat(cfg).st_mode & 0o777
            assert mode == 0o700
            settings = json.loads(open(os.path.join(cfg, "settings.json")).read())
            assert settings == {}  # clean: no plugins, no hooks
            mcp = json.loads(open(os.path.join(cfg, "empty-mcp.json")).read())
            assert mcp == {"mcpServers": {}}
            link = os.path.join(cfg, ".credentials.json")
            assert os.path.islink(link), "credentials MUST be a symlink, never a copy"
            assert os.path.realpath(link) == os.path.realpath(str(creds))
        finally:
            jr.cleanup_scratch_config(cfg)
        assert not os.path.exists(cfg)

    def test_cleanup_never_follows_symlink(self, tmp_path):
        creds = tmp_path / ".credentials.json"
        creds.write_text("SECRET")
        cfg = jr.make_scratch_config(creds_source=str(creds))
        jr.cleanup_scratch_config(cfg)
        assert creds.read_text() == "SECRET"  # the real file survives cleanup

    def test_build_cmd_isolation_flags(self, tmp_path):
        creds = tmp_path / ".credentials.json"
        creds.write_text("{}")
        cfg = jr.make_scratch_config(creds_source=str(creds))
        try:
            cmd, env = jr.build_cmd("opus", cfg)
            assert cmd[:3] == ["claude", "-p", "--model"]
            assert "opus" in cmd
            assert "--strict-mcp-config" in cmd
            i = cmd.index("--mcp-config")
            assert cmd[i + 1] == os.path.join(cfg, "empty-mcp.json")
            j = cmd.index("--settings")
            assert cmd[j + 1] == os.path.join(cfg, "settings.json")
            assert env["CLAUDE_CONFIG_DIR"] == cfg
        finally:
            jr.cleanup_scratch_config(cfg)


class TestRunClaude:
    @staticmethod
    def _proc(rc, out="", err=""):
        class P:
            returncode = rc
            stdout = out
            stderr = err
        return P()

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(jr.time, "sleep", lambda s: None)

    def test_timeout_is_transient_then_succeeds(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise jr.subprocess.TimeoutExpired(cmd, 600)
            return self._proc(0, out='{"ok": true}')
        monkeypatch.setattr(jr.subprocess, "run", fake_run)
        assert jr.run_claude("p", "opus", "/tmp/cfg") == '{"ok": true}'
        assert len(calls) == 2

    def test_timeout_exhausted_raises_runtime_error(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise jr.subprocess.TimeoutExpired(cmd, 600)
        monkeypatch.setattr(jr.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="timed out"):
            jr.run_claude("p", "opus", "/tmp/cfg")

    def test_long_stdout_auth_vocabulary_is_not_auth_error(self, monkeypatch):
        verdicty = ("the workflow handles authentication via OAuth and the user "
                    "is logged in before the Slack node fires. " * 20)
        def fake_run(cmd, **kw):
            return self._proc(1, out=verdicty, err="transient failure")
        monkeypatch.setattr(jr.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as ei:
            jr.run_claude("p", "opus", "/tmp/cfg")
        assert type(ei.value) is RuntimeError  # must NOT be the AuthError subclass

    def test_stderr_401_raises_auth_error(self, monkeypatch):
        def fake_run(cmd, **kw):
            return self._proc(1, out="", err="API error: 401 unauthorized")
        monkeypatch.setattr(jr.subprocess, "run", fake_run)
        with pytest.raises(jr.AuthError):
            jr.run_claude("p", "opus", "/tmp/cfg")

    def test_short_stdout_login_message_raises_auth_error(self, monkeypatch):
        def fake_run(cmd, **kw):
            return self._proc(1, out="Please run /login", err="")
        monkeypatch.setattr(jr.subprocess, "run", fake_run)
        with pytest.raises(jr.AuthError):
            jr.run_claude("p", "opus", "/tmp/cfg")

    def test_rate_limit_retries_then_succeeds(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(1)
            if len(calls) < 3:
                return self._proc(1, err="429 rate limit exceeded")
            return self._proc(0, out="verdict")
        monkeypatch.setattr(jr.subprocess, "run", fake_run)
        assert jr.run_claude("p", "opus", "/tmp/cfg") == "verdict"
        assert len(calls) == 3


def runner_returning(*responses):
    """Fake runner yielding each response in turn; records prompts."""
    calls = []
    it = iter(responses)

    def run(prompt):
        calls.append(prompt)
        return next(it)
    run.calls = calls
    return run


class TestJudgeOne:
    def test_happy_path_stamps_metadata(self):
        run = runner_returning(json.dumps(GOOD))
        v = jr.judge_one(_ji(), run, model="opus")
        assert v["intent_fit"] == "pass"
        assert v["judge_model"] == "opus"
        assert v["workflow_source"] == "validated"
        assert "judged_at" in v

    def test_fail_closed_missing_workflow_no_call(self):
        run = runner_returning()
        v = jr.judge_one(_ji(workflow_text=None, workflow_source="missing"), run)
        assert v["intent_fit"] == "fail"
        assert "no parseable workflow artifact" in v["intent_reasoning"]
        assert v["gotcha_handled"] == "not_applicable"  # no rule on this input
        assert run.calls == []  # no claude call was made

    def test_fail_closed_with_gotcha_rule(self):
        run = runner_returning()
        v = jr.judge_one(_ji(workflow_text=None, workflow_source="missing",
                             gotcha=RULES[0]), run)
        assert v["gotcha_handled"] == "fail"

    def test_parse_retry_then_success(self):
        run = runner_returning("no json here", json.dumps(GOOD))
        v = jr.judge_one(_ji(), run)
        assert v["intent_fit"] == "pass"
        assert len(run.calls) == 2
        assert "parse" in run.calls[1].lower()  # retry prompt mentions the error

    def test_retries_exhausted_raises(self):
        run = runner_returning("junk", "junk", "junk")
        with pytest.raises(jr.VerdictParseError):
            jr.judge_one(_ji(), run)

    def test_checklist_rollup_overrides_judge(self):
        crit = {"prompt_idx": 1, "must": ["a", "b"], "nice": ["c"]}
        verdict = dict(GOOD, intent_fit="pass",
                       criteria=[{"criterion": "a", "met": True},
                                 {"criterion": "b", "met": False},
                                 {"criterion": "c", "met": True}])
        run = runner_returning(json.dumps(verdict))
        v = jr.judge_one(_ji(criteria=crit), run)
        assert v["intent_fit"] == "fail"  # unmet 'must' overrides judge's pass

    def test_checklist_all_must_met_passes(self):
        crit = {"prompt_idx": 1, "must": ["a"], "nice": ["c"]}
        verdict = dict(GOOD, intent_fit="fail",
                       criteria=[{"criterion": "a", "met": True},
                                 {"criterion": "c", "met": False}])
        run = runner_returning(json.dumps(verdict))
        v = jr.judge_one(_ji(criteria=crit), run)
        assert v["intent_fit"] == "pass"  # all must met; nice ignored

    def test_checklist_rollup_tolerates_whitespace_case_drift(self):
        crit = {"prompt_idx": 1, "must": ["routes by tier"], "nice": []}
        verdict = dict(GOOD, intent_fit="fail",
                       criteria=[{"criterion": "  Routes by  tier ", "met": True}])
        run = runner_returning(json.dumps(verdict))
        v = jr.judge_one(_ji(criteria=crit), run)
        assert v["intent_fit"] == "pass"


def make_multi_tree(tmp_path, idxs=(0, 1)):
    for i in idxs:
        make_result_tree(tmp_path, fileidx=i)
    gt_file = tmp_path / "gt.jsonl"
    write_jsonl(gt_file, GT)
    return tmp_path, str(gt_file)


class TestRunPass:
    def test_writes_verdicts_and_skips_cached(self, tmp_path):
        root, gt = make_multi_tree(tmp_path)
        run = runner_returning(*([json.dumps(GOOD)] * 2))
        stats = jr.run_pass(str(root), ["cond-a"], run, ground_truth_path=gt,
                            rules_path=str(tmp_path / "no-rules.jsonl"),
                            criteria_path=str(tmp_path / "no-crit.jsonl"),
                            concurrency=2)
        assert stats["judged"] == 2 and stats["errors"] == 0
        vfile = root / "cond-a" / "prompt-000-run01.judge.json"
        assert json.loads(vfile.read_text())["intent_fit"] == "pass"
        # second pass: everything cached, zero runner calls
        run2 = runner_returning()
        stats2 = jr.run_pass(str(root), ["cond-a"], run2, ground_truth_path=gt,
                             rules_path=str(tmp_path / "no-rules.jsonl"),
                             criteria_path=str(tmp_path / "no-crit.jsonl"))
        assert stats2["skipped"] == 2 and run2.calls == []

    def test_force_rejudges(self, tmp_path):
        root, gt = make_multi_tree(tmp_path, idxs=(0,))
        run = runner_returning(json.dumps(GOOD), json.dumps(GOOD))
        common = dict(ground_truth_path=gt,
                      rules_path=str(tmp_path / "n.jsonl"),
                      criteria_path=str(tmp_path / "n.jsonl"))
        jr.run_pass(str(root), ["cond-a"], run, **common)
        stats = jr.run_pass(str(root), ["cond-a"], run, force=True, **common)
        assert stats["judged"] == 1 and stats["skipped"] == 0

    def test_auth_error_halts_pass_no_partial_verdict(self, tmp_path):
        root, gt = make_multi_tree(tmp_path, idxs=(0, 1, 3))
        make_result_tree(tmp_path, fileidx=3)

        def run(prompt):
            raise jr.AuthError("401 from API")
        stats = jr.run_pass(str(root), ["cond-a"], run, ground_truth_path=gt,
                            rules_path=str(tmp_path / "n.jsonl"),
                            criteria_path=str(tmp_path / "n.jsonl"),
                            concurrency=1)
        assert stats["halted"] is True
        assert not list((root / "cond-a").glob("*.judge.json"))  # nothing partial

    def test_parse_failure_counts_error_writes_nothing(self, tmp_path):
        root, gt = make_multi_tree(tmp_path, idxs=(0,))
        run = runner_returning("junk", "junk", "junk")
        stats = jr.run_pass(str(root), ["cond-a"], run, ground_truth_path=gt,
                            rules_path=str(tmp_path / "n.jsonl"),
                            criteria_path=str(tmp_path / "n.jsonl"))
        assert stats["errors"] == 1 and stats["judged"] == 0
        assert not list((root / "cond-a").glob("*.judge.json"))

    def test_concurrent_pass_judges_everything_once(self, tmp_path):
        idxs = tuple(range(20))
        for i in idxs:
            make_result_tree(tmp_path, fileidx=i)
        gt_file = tmp_path / "gt.jsonl"
        write_jsonl(gt_file, [{"id": f"p{i}", "prompt": f"prompt {i}"} for i in idxs])
        run = runner_returning(*([json.dumps(GOOD)] * 20))
        stats = jr.run_pass(str(tmp_path), ["cond-a"], run,
                            ground_truth_path=str(gt_file),
                            rules_path=str(tmp_path / "n.jsonl"),
                            criteria_path=str(tmp_path / "n.jsonl"),
                            concurrency=8)
        assert stats["judged"] == 20 and stats["errors"] == 0
        verdicts = sorted((tmp_path / "cond-a").glob("*.judge.json"))
        assert len(verdicts) == 20
        assert len(run.calls) == 20  # every item judged exactly once


class TestPreflight:
    def _creds(self, tmp_path, expires_in_s):
        p = tmp_path / "creds.json"
        now_ms = 1_750_000_000_000  # fixed epoch for determinism
        p.write_text(json.dumps(
            {"claudeAiOauth": {"expiresAt": now_ms + expires_in_s * 1000}}))
        return str(p), now_ms / 1000

    def test_token_outlives_pass_ok(self, tmp_path):
        creds, now_s = self._creds(tmp_path, expires_in_s=3600)
        assert jr.preflight_ok(creds, n_calls=32, concurrency=16,
                               assume_yes=False, now_s=now_s) is True

    def test_token_expires_mid_pass_warns(self, tmp_path):
        # 256 calls / 16 workers * 30s = 480s estimated; token dies in 60s
        creds, now_s = self._creds(tmp_path, expires_in_s=60)
        assert jr.preflight_ok(creds, n_calls=256, concurrency=16,
                               assume_yes=False, now_s=now_s,
                               confirm=lambda msg: False) is False
        assert jr.preflight_ok(creds, n_calls=256, concurrency=16,
                               assume_yes=True, now_s=now_s) is True

    def test_missing_creds_file_warns_but_does_not_crash(self, tmp_path):
        assert jr.preflight_ok(str(tmp_path / "nope.json"), n_calls=4,
                               concurrency=4, assume_yes=True, now_s=0) is True


class TestAggregate:
    def _write_verdict(self, root, cond, stem, intent="pass", gotcha="not_applicable"):
        v = dict(GOOD, intent_fit=intent, gotcha_handled=gotcha)
        (root / cond / f"{stem}.judge.json").write_text(json.dumps(v))

    def test_aggregation_math(self, tmp_path):
        root, _ = make_multi_tree(tmp_path, idxs=(0, 1))
        make_result_tree(tmp_path, fileidx=3)
        self._write_verdict(root, "cond-a", "prompt-000-run01", "pass", "not_applicable")
        self._write_verdict(root, "cond-a", "prompt-001-run01", "fail", "pass")
        self._write_verdict(root, "cond-a", "prompt-003-run01", "pass", "fail")
        summary = jr.aggregate(str(root), ["cond-a"])
        c = summary["conditions"]["cond-a"]
        assert c["judged"] == 3
        assert c["intent_fit_pct"] == pytest.approx(66.7, abs=0.1)
        # gotcha denominator excludes not_applicable: 1 pass / 2 applicable
        assert c["gotcha_handled_pct"] == pytest.approx(50.0, abs=0.1)
        assert c["unjudged"] == 0
        assert len(c["fails"]) == 2  # one intent fail + one gotcha fail
        assert os.path.exists(os.path.join(str(root), "judge-summary.json"))

    def test_unjudged_counted(self, tmp_path):
        root, _ = make_multi_tree(tmp_path, idxs=(0, 1))
        self._write_verdict(root, "cond-a", "prompt-000-run01")
        summary = jr.aggregate(str(root), ["cond-a"])
        assert summary["conditions"]["cond-a"]["unjudged"] == 1

    def test_format_table_contains_pcts(self, tmp_path):
        root, _ = make_multi_tree(tmp_path, idxs=(0,))
        self._write_verdict(root, "cond-a", "prompt-000-run01")
        table = jr.format_table(jr.aggregate(str(root), ["cond-a"]))
        assert "cond-a" in table and "100.0%" in table


class TestCli:
    def test_dry_run_prints_inputs_no_calls(self, tmp_path, capsys, monkeypatch):
        root, gt = make_multi_tree(tmp_path, idxs=(0,))
        monkeypatch.setattr(jr, "run_claude", lambda *a, **k: pytest.fail("called claude"))
        rc = jr.main([str(root), "--conditions", "cond-a", "--dry-run",
                      "--ground-truth", gt])
        assert rc == 0
        out = capsys.readouterr().out
        assert "post a message to slack" in out

    def test_main_judges_and_prints_table(self, tmp_path, capsys, monkeypatch):
        root, gt = make_multi_tree(tmp_path, idxs=(0,))
        monkeypatch.setattr(jr, "run_claude",
                            lambda prompt, model, cfg: json.dumps(GOOD))
        monkeypatch.setattr(jr, "preflight_ok", lambda *a, **k: True)
        rc = jr.main([str(root), "--conditions", "cond-a", "--ground-truth", gt])
        assert rc == 0
        out = capsys.readouterr().out
        assert "intent" in out.lower() and "cond-a" in out

    def test_main_discovers_condition_subdirs(self, tmp_path, monkeypatch, capsys):
        root, gt = make_multi_tree(tmp_path, idxs=(0,))
        (root / "clean-settings.json").write_text("{}")  # top-level non-dir noise
        monkeypatch.setattr(jr, "run_claude",
                            lambda prompt, model, cfg: json.dumps(GOOD))
        monkeypatch.setattr(jr, "preflight_ok", lambda *a, **k: True)
        rc = jr.main([str(root), "--ground-truth", gt])
        assert rc == 0
        assert "cond-a" in capsys.readouterr().out

    def test_main_halted_returns_nonzero(self, tmp_path, monkeypatch):
        root, gt = make_multi_tree(tmp_path, idxs=(0,))
        def boom(prompt, model, cfg):
            raise jr.AuthError("401")
        monkeypatch.setattr(jr, "run_claude", boom)
        monkeypatch.setattr(jr, "preflight_ok", lambda *a, **k: True)
        assert jr.main([str(root), "--conditions", "cond-a",
                        "--ground-truth", gt]) == 2
