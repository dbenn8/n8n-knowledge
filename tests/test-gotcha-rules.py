#!/usr/bin/env python3
"""Pin the gotcha-rule check semantics in gotcha_scoring.py.

Covers the param-aware tightenings added after the 2026-06-11 Opus review of
the group C run (false-positive/false-negative analysis per rule):
  - require_node_param_regex: a required node only counts when its parameters
    match a regex (e.g. httpRequest must actually target Supabase).
  - avoid_param_patterns: a node is "buggy" only when type AND params match
    (e.g. Merge is unsafe only in positional combine, keyed merge is fine).
  - warned_ok_terms: buggy node present but the response explicitly warns the
    user about the bug -> counts as addressed.
  - explicit_timezone_no_wait param check (rule 119).
  - curated heuristic_terms override for llm_only rules (rules 22, 125).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "eval"))

from gotcha_scoring import check_rule, load_rules

RULES = load_rules(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "eval", "gotcha_rules.jsonl")
)


def wf(*nodes, settings=None):
    return {"nodes": list(nodes), "connections": {}, **({"settings": settings} if settings else {})}


def node(ntype, name="n", **params):
    return {"name": name, "type": ntype, "typeVersion": 1, "parameters": params}


class TestRule110SupabaseSwap(unittest.TestCase):
    """httpRequest must actually target Supabase to count as the workaround."""

    def test_http_request_targeting_supabase_passes(self):
        w = wf(node("n8n-nodes-base.httpRequest", url="https://xyz.supabase.co/rest/v1/survey_responses"))
        addressed, reason, _ = check_rule(w, RULES[110])
        self.assertTrue(addressed, reason)

    def test_unrelated_http_request_does_not_count(self):
        w = wf(node("n8n-nodes-base.httpRequest", url="https://hooks.slack.com/services/T000"))
        addressed, reason, _ = check_rule(w, RULES[110])
        self.assertFalse(addressed, reason)

    def test_native_supabase_node_still_fails(self):
        w = wf(
            node("n8n-nodes-base.supabase", operation="create"),
            node("n8n-nodes-base.httpRequest", url="https://xyz.supabase.co/rest/v1/x"),
        )
        addressed, reason, _ = check_rule(w, RULES[110])
        self.assertFalse(addressed, reason)


class TestRule113SplitInBatches(unittest.TestCase):
    """Code node must show real slicing/iteration; loud warning is an escape hatch."""

    def test_code_with_slicing_passes(self):
        w = wf(node("n8n-nodes-base.code", jsCode="const batch = items.slice(i, i + 50);"))
        addressed, reason, _ = check_rule(w, RULES[113])
        self.assertTrue(addressed, reason)

    def test_incidental_code_node_does_not_count(self):
        w = wf(node("n8n-nodes-base.code", jsCode="return { ...$json, merged: true };"))
        addressed, reason, _ = check_rule(w, RULES[113])
        self.assertFalse(addressed, reason)

    def test_split_in_batches_fails_even_with_warning(self):
        # Rule 113 is strict: the prompt no longer demands Loop Over Items, so a
        # data-losing workflow fails even when the response warns about the bug.
        w = wf(node("n8n-nodes-base.splitInBatches", batchSize=1))
        text = "Warning: Loop Over Items has a known bug where it repeats the first item and causes data loss."
        addressed, reason, _ = check_rule(w, RULES[113], text)
        self.assertFalse(addressed, reason)

    def test_split_in_batches_with_generic_prose_fails(self):
        w = wf(node("n8n-nodes-base.splitInBatches", batchSize=1))
        text = "Each loop processes one row at a time to avoid rate limits."
        addressed, reason, _ = check_rule(w, RULES[113], text)
        self.assertFalse(addressed, reason)


class TestWarnedOkMechanism(unittest.TestCase):
    """The warned_ok_terms escape hatch stays available in the scorer (no rule
    currently uses it); pin its semantics with a synthetic rule."""

    RULE = {
        "prompt_idx": 999, "prompt_id": "synthetic", "check_type": "node_swap",
        "avoid_node_types": ["n8n-nodes-base.splitInBatches"],
        "require_node_types": [],
        "warned_ok_terms": "data loss|first item",
    }

    def test_buggy_node_with_warning_passes(self):
        w = wf(node("n8n-nodes-base.splitInBatches"))
        addressed, reason, _ = check_rule(w, self.RULE, "this node can cause data loss")
        self.assertTrue(addressed, reason)

    def test_buggy_node_without_warning_fails(self):
        w = wf(node("n8n-nodes-base.splitInBatches"))
        addressed, reason, _ = check_rule(w, self.RULE, "wired up the loop for you")
        self.assertFalse(addressed, reason)


class TestRule116NotionFormulaFilter(unittest.TestCase):
    """Notion node sending a formula filter fails even when an IF node exists."""

    def test_unfiltered_fetch_with_local_if_passes(self):
        w = wf(
            node("n8n-nodes-base.notion", operation="getAll", options={}),
            node("n8n-nodes-base.if", conditions={"leftValue": "={{ $json.properties['Priority Score'].formula.number }}"}),
        )
        addressed, reason, _ = check_rule(w, RULES[116])
        self.assertTrue(addressed, reason)

    def test_notion_formula_filter_fails_despite_if_node(self):
        w = wf(
            node(
                "n8n-nodes-base.notion",
                operation="getAll",
                filters={"conditions": [{"key": "Priority Score|formula", "condition": "greater_than"}]},
            ),
            node("n8n-nodes-base.if", conditions={}),
        )
        addressed, reason, _ = check_rule(w, RULES[116])
        self.assertFalse(addressed, reason)


class TestRule122MergeCombine(unittest.TestCase):
    """Only positional/keyless combine merges are unsafe; keyed merge and merge-free flows pass."""

    def test_positional_combine_fails(self):
        w = wf(
            node("n8n-nodes-base.merge", mode="combine", combinationMode="combineByPosition"),
            node("n8n-nodes-base.code", jsCode="return items;"),
        )
        addressed, reason, _ = check_rule(w, RULES[122])
        self.assertFalse(addressed, reason)

    def test_keyed_combine_passes(self):
        w = wf(node("n8n-nodes-base.merge", mode="combine", combinationMode="mergeByKey", propertyName1="email"))
        addressed, reason, _ = check_rule(w, RULES[122])
        self.assertTrue(addressed, reason)

    def test_merge_free_linear_flow_passes_without_code_node(self):
        w = wf(
            node("n8n-nodes-base.googleSheets", operation="update", matchingColumns=["email"]),
        )
        addressed, reason, _ = check_rule(w, RULES[122])
        self.assertTrue(addressed, reason)


class TestRule119TimezoneParamCheck(unittest.TestCase):
    def test_workflow_settings_timezone_no_wait_passes(self):
        w = wf(node("n8n-nodes-base.scheduleTrigger"), settings={"timezone": "America/New_York"})
        addressed, reason, _ = check_rule(w, RULES[119])
        self.assertTrue(addressed, reason)

    def test_trigger_param_timezone_passes(self):
        w = wf(node("n8n-nodes-base.scheduleTrigger", timezone="America/New_York"))
        addressed, reason, _ = check_rule(w, RULES[119])
        self.assertTrue(addressed, reason)

    def test_no_timezone_fails(self):
        w = wf(node("n8n-nodes-base.scheduleTrigger"))
        addressed, reason, _ = check_rule(w, RULES[119])
        self.assertFalse(addressed, reason)

    def test_wait_node_fails_even_with_timezone(self):
        w = wf(
            node("n8n-nodes-base.scheduleTrigger", timezone="America/New_York"),
            node("n8n-nodes-base.wait"),
        )
        addressed, reason, _ = check_rule(w, RULES[119])
        self.assertFalse(addressed, reason)


class TestCuratedHeuristicTerms(unittest.TestCase):
    def test_rule22_operation_label_no_longer_matches(self):
        # 'Get Row(s)' as a mere operation label must NOT count as addressing the bug
        addressed, reason, _ = check_rule({}, RULES[22], "Google Sheets (Get Row(s)) — configure OAuth2 credentials")
        self.assertFalse(addressed, reason)

    def test_rule22_real_warning_matches(self):
        addressed, reason, _ = check_rule({}, RULES[22], "verify your OAuth scope includes spreadsheets.readonly")
        self.assertTrue(addressed, reason)

    def test_rule125_webhook_url_fix_matches(self):
        addressed, reason, _ = check_rule({}, RULES[125], "Set WEBHOOK_URL to the public Cloudflare domain")
        self.assertTrue(addressed, reason)

    def test_rule125_generic_oauth_advice_misses(self):
        addressed, reason, _ = check_rule({}, RULES[125], "Copy the OAuth callback URL into Google Cloud Console")
        self.assertFalse(addressed, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
