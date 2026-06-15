#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected '$expected', got '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== node lookup tests ==="

FIXTURE="$SCRIPT_DIR/fixtures/node-lookup-queries.json"

# Run Python to produce test results, capture to temp file to avoid subshell
TMPOUT=$(mktemp)
trap "rm -f $TMPOUT" EXIT

python3 -c "
import json, sys
sys.path.insert(0, '$LIB_DIR')
from node_lookup import identify_nodes
fixtures = json.load(open('$FIXTURE'))
for f in fixtures:
    result = identify_nodes(f['query'])
    top = result[0][1] if result else None
    exp = f['expect']
    got_base = top.split('.')[-1] if top else None
    exp_base = exp.split('.')[-1] if exp else None
    status = 'PASS' if got_base == exp_base else 'FAIL'
    print(f'{status}|{f[\"query\"][:60]}|{exp or \"None\"}|{top or \"None\"}')
" > "$TMPOUT"

while IFS='|' read -r status query exp got; do
  assert_eq "$query" "$exp" "$got"
done < "$TMPOUT"

# --- Detection-noise regression tests (P0 injection fixes) ---
# These assert on the FULL hit list, not just the top hit, because the
# defects are about (a) spurious nodes appearing at all and (b) ordering.
echo ""
echo "--- detection noise regression ---"

TMPOUT2=$(mktemp)
trap "rm -f $TMPOUT $TMPOUT2" EXIT

python3 > "$TMPOUT2" <<PYEOF
import sys
sys.path.insert(0, '$LIB_DIR')
from node_lookup import identify_nodes

def types(prompt):
    return [nt for _, nt in identify_nodes(prompt)]

cases = []

# Defect A: bare 'n8n' must not match the n8n meta-node.
cases.append(('A_bare_n8n_no_metanode',
    'nodes-base.n8n' not in types('google sheets append row not working with n8n')))

# Defect A: generic build prompt -> slack present, n8n meta-node absent.
t = types('build an n8n workflow that posts to slack')
cases.append(('A_build_prompt_has_slack', 'nodes-base.slack' in t))
cases.append(('A_build_prompt_no_n8n_metanode', 'nodes-base.n8n' not in t))

# Defect B: 'workflow' must not produce a workflowTrigger hit, and must not
# hijack the top slot. 'wait node workflow stuck' -> wait FIRST, no trigger.
t = types('wait node workflow stuck')
cases.append(('B_no_workflowtrigger', 'nodes-base.workflowTrigger' not in t))
cases.append(('B_wait_is_first', bool(t) and t[0] == 'nodes-base.wait'))

# Defect B (combined with A): build prompt also must drop workflowTrigger.
t = types('build an n8n workflow that posts to slack')
cases.append(('B_build_prompt_no_workflowtrigger', 'nodes-base.workflowTrigger' not in t))

# Defect E: explicit multi-word 'workflow trigger' STILL resolves.
cases.append(('E_explicit_workflow_trigger',
    'nodes-base.workflowTrigger' in types('add a workflow trigger node')))

# Defect H: the PLURAL 'workflows' must NOT stem to 'workflow' -> workflowTrigger.
# =====================================================================
# DO NOT DELETE OR WEAKEN THESE CASES. This is a real, fixed bug guard.
# =====================================================================
# 'workflow' is a demoted bare token, but the Pass-2 verb/plural stemmer matched
# the stem in the lookup WITHOUT re-checking the demotion list — so 'workflows'
# (not itself demoted) stemmed to 'workflow' and resolved to workflowTrigger.
# Fix: hooks/lib/node_lookup.py re-checks _DEMOTED_BARE_TOKENS/_COMMON_WORDS on
# each stem (commit dc382c4).
#
# WHY THIS MATTERS ENOUGH TO PIN: this same detector tags GitHub issues/PRs with
# node:X at INGEST time. Thousands of issues mention 'workflows' in passing; with
# this bug, each would be falsely stamped node:workflowTrigger, and gotcha recall
# for that node would fill with irrelevant issues — re-introducing the exact
# cross-node noise the version-aware-bug-surfacing effort exists to remove. A
# false positive here corrupts the production bank and costs a full re-retain to
# undo. If this test ever goes red, the detector regressed (or a vendored copy
# drifted) — FIX THE CODE, do not relax the assertion.
#
# NOTE the asymmetry the second case pins: when a REAL node is present, Pass-1
# matches it and Pass-2 (the stemmer) never runs, so 'workflows' is harmless
# there. The bug only bites generic issues with no other detectable node — i.e.
# exactly the ones that should get NO node tag at all.
cases.append(('H_plural_workflows_no_workflowtrigger',
    'nodes-base.workflowTrigger' not in types('improve editor performance for large workflows')))
cases.append(('H_plural_workflows_with_real_node',
    'nodes-base.merge' in types('the merge node loses rows across large workflows')
    and 'nodes-base.workflowTrigger' not in types('the merge node loses rows across large workflows')))

# --- Tag-format contract: community_tag()/service_to_tag() are the SINGLE
# canonical node_type -> community tag mapping shared by recall (bash
# _node_to_community_tag routes through it) AND the ingest side. If these drift
# from what do_gotcha_recall queries, recall silently misses ingest's tags.
import node_lookup as _nl
cases.append(('tag_openai_base', _nl.community_tag('nodes-base.openAi') == 'openai'))
cases.append(('tag_openai_langchain', _nl.community_tag('@n8n/n8n-nodes-langchain.openAi') == 'openai'))
cases.append(('tag_http_request', _nl.community_tag('nodes-base.httpRequest') == 'http-request'))
cases.append(('tag_merge', _nl.community_tag('nodes-base.merge') == 'merge'))
cases.append(('tag_supabase', _nl.community_tag('nodes-base.supabase') == 'supabase'))
# do_gotcha_recall strips Trigger/Tool BEFORE the tag map; community_tag mirrors that.
cases.append(('tag_strips_trigger', _nl.community_tag('nodes-base.scheduleTrigger') == 'schedule'))
cases.append(('tag_strips_tool', _nl.community_tag('nodes-base.gmailTool') == 'gmail'))
cases.append(('svc_to_tag_camel', _nl.service_to_tag('httpRequest') == 'http-request'))
cases.append(('svc_to_tag_openai_map', _nl.service_to_tag('openAi') == 'openai'))
# NOTE: the JS-error noise "X is not a function" (which would tag the deprecated
# Function node) is stripped on the INGEST side (sync-github.py:detect_node_tags),
# not here — the canonical detector still supports the Function node for real
# prompts. See n8n-hindsight test_github_node_tagging.py.

# Defect J: rare community nodes with English-word names must not match a stray
# word. 'running' must NOT stem to the Runn node; 'top-level' must NOT hit the
# Level node. The real subject node in each title still resolves.
cases.append(('J_running_no_runn_node',
    'n8n-nodes-runn-dotsandarrows.runn' not in types('Merge Append not running if Merge choose branch')))
cases.append(('J_running_keeps_merge',
    'nodes-base.merge' in types('Merge Append not running if Merge choose branch')))
cases.append(('J_toplevel_no_level_node',
    '@levelrmm/n8n-nodes-level.level' not in types('HTTP Request retries when response body has a top-level error field')))

# Defect K: the Pass-1 verb/plural suffix is gated to names >= 5 chars, so short
# node names don't over-match English ("boxing" must not hit a 3-char 'box'
# node), while real verb forms of longer names still resolve.
cases.append(('K_short_name_no_suffix_overmatch',
    'nodes-base.box' not in types('boxing match results from the api')))
cases.append(('K_long_name_suffix_still_works',
    'nodes-base.merge' in types('the workflow merges two streams')))

# Defect C: event-phrasing trigger word 'added' upgrades to the trigger node.
cases.append(('C_added_yields_sheets_trigger',
    'nodes-base.googleSheetsTrigger' in types('when a google sheets row is added')))
cases.append(('C_created_yields_sheets_trigger',
    'nodes-base.googleSheetsTrigger' in types('when a new google sheets row is created')))

# Negative controls: the demotion must not nuke legitimate detections.
cases.append(('neg_schedule_still_works',
    'nodes-base.scheduleTrigger' in types('trigger the workflow on a schedule every 5 minutes')))
cases.append(('neg_manual_trigger_still_works',
    'nodes-base.manualTrigger' in types('execute a workflow manually using the Manual Trigger')))
cases.append(('neg_slack_still_works',
    'nodes-base.slack' in types('configure the Slack node to post a message')))

# Defect C guard: ubiquitous adjective 'new' must NOT upgrade an action node
# to its trigger variant ('create a new issue in Jira' is an action).
cases.append(('C_new_does_not_overtrigger',
    'nodes-base.jiraTrigger' not in types('create a new issue in jira after processing')))

# --- Defect F: trigger intent must be LOCAL, not prompt-global ---
# Live regression prompt. 'added' is local to google sheets ('a google
# sheets row is added'), so sheets upgrades to its trigger variant. The
# event phrase is fenced off by the comma, so slack/postgres/http_request --
# which are ACTION targets later in the prompt ('post to slack', 'log to
# postgres') -- must stay their action nodes and NOT flip to trigger.
t = types('build an n8n workflow: when a google sheets row is added, '
          'check it with an IF node, post to slack, log to postgres, '
          'and call an http request webhook')
cases.append(('F_sheets_upgrades_local',
    'nodes-base.googleSheetsTrigger' in t))
cases.append(('F_slack_stays_action',
    'nodes-base.slack' in t and 'nodes-base.slackTrigger' not in t))
cases.append(('F_postgres_stays_action',
    'nodes-base.postgres' in t and 'nodes-base.postgresTrigger' not in t))
cases.append(('F_http_request_present',
    'nodes-base.httpRequest' in t))

# Comma-fenced clause: 'added' belongs to sheets; the slack message that
# follows the comma is an action, not a trigger.
t = types('when a new row is added to google sheets, send a slack message')
cases.append(('F_clause_sheets_trigger',
    'nodes-base.googleSheetsTrigger' in t))
cases.append(('F_clause_slack_action',
    'nodes-base.slack' in t and 'nodes-base.slackTrigger' not in t))

# Keep-green guard: the event phrase ALONE still upgrades sheets locally.
cases.append(('F_sheets_alone_trigger',
    'nodes-base.googleSheetsTrigger' in types('when a google sheets row is added')))

# Defect G: verb forms of node names must resolve to the node.
# "merges" -> merge, "splits" -> split. The fuzzy fallback must also
# respect _DEMOTED_BARE_TOKENS so "workflow" doesn't sneak through.
cases.append(('G_merges_finds_merge',
    'nodes-base.merge' in types('merges data from two different API sources')))
cases.append(('G_merges_no_workflow',
    'nodes-base.workflowTrigger' not in types('merges data from two different API sources')))
cases.append(('G_splits_no_false_positive',
    len(types('splits items into batches')) == 0))

# Defect G: verb forms must be detected ALONGSIDE other nodes (not just solo).
# This is the actual eval failure: 'webhook ... merges' detected only webhook.
t = types('receive webhook data then merges them together by product ID')
cases.append(('G_verb_with_other_nodes_merge',
    'nodes-base.merge' in t))
cases.append(('G_verb_with_other_nodes_webhook',
    'nodes-base.webhook' in t))

# Defect H: model-name aliases resolve to OpenAI node.
cases.append(('H_gpt4o_finds_openai',
    'nodes-base.openAi' in types('check blog posts with GPT-4o before publishing')))
cases.append(('H_chatgpt_finds_openai',
    'nodes-base.openAi' in types('use ChatGPT to summarize the article')))
cases.append(('H_dalle_finds_openai',
    'nodes-base.openAi' in types('generate an image with DALL-E 3')))
cases.append(('H_gpt4_finds_openai',
    'nodes-base.openAi' in types('send the text to GPT-4 for analysis')))

# Defect I: rare-node broad-keyword/fuzzy false positives must NOT tag, but the
# real nodes (and explicit '<word> node' references) must still resolve.
#   exact-key common words (demoted): if / search / inbox
#   fuzzy-source common words: host->ghost, post->posta, attachment->attachmentAV
cases.append(('I_bare_if_no_fp',
    'nodes-base.if' not in types('Do not pull jobs if the worker is unhealthy')))
cases.append(('I_if_node_still_resolves',
    'nodes-base.if' in types('IF node returns the wrong branch')))
cases.append(('I_host_no_ghost',
    'nodes-base.ghost' not in types('Resolve VM proxy results on host side')))
cases.append(('I_ghost_real_still_resolves',
    'nodes-base.ghost' in types('Ghost CMS publish post fails')))
cases.append(('I_search_no_searchapi',
    len(types('Rewrite icon picker with search and emoji set')) == 0))
cases.append(('I_search_node_still_resolves',
    '@searchapi/n8n-nodes-searchapi.searchApi' in types('use the search node to query')))
cases.append(('I_attachment_no_av',
    '@attachmentav/n8n-nodes-attachmentav.attachmentAV' not in
    types('Error adding attachment to Create Draft Outlook')))
cases.append(('I_post_no_posta',
    'n8n-nodes-posta.posta' not in types('post a message to the channel')))
cases.append(('I_posta_real_still_resolves',
    'n8n-nodes-posta.posta' in types('posta API connection error')))
cases.append(('I_inbox_demoted_webhook_kept',
    '@inboxapp/n8n-nodes-inboxapp.inboxApp' not in types('Webhook inbox not receiving')
    and 'nodes-base.webhook' in types('Webhook inbox not receiving')))

# Defect J: GENERAL rules (task #84) — driven by the English-word oracle +
# first-party-vs-third-party scope, NOT a hand list. These corpus FP cases were
# NEVER added to _COMMON_WORDS/_DEMOTED, so passing proves the rules GENERALIZE.
#   R1: fuzzy match whose SOURCE word is a dictionary word -> suppress.
#   R2: exact match to a THIRD-PARTY node whose key is a dictionary word -> demote.
cases.append(('J_extract_no_extruct',  # R1: extract->extruct (fuzzy, dict source)
    len(types('Out of Memory using Extract from File node')) == 0))
cases.append(('J_table_no_teable',     # R1: table->teable
    '@teable' not in str(types('Account for pending CSV uploads in data-table budget'))))
cases.append(('J_context_no_qontext',  # R1: context->qontext
    'qontext' not in str(types('support context caching in Google Vertex model'))))
cases.append(('J_custom_no_customerio',  # R1: custom->customerIo (FIRST-PARTY, still FP)
    'nodes-base.customerIo' not in types('Fix custom node icon path resolution')))
cases.append(('J_consolidate_demoted',  # R2: third-party + dict key -> demote bare
    len(types('Consolidate native tools into action families')) == 0))
cases.append(('J_consolidate_node_resolves',  # R2 preserves explicit '<name> node'
    'consolidate' in str(types('set up the consolidate node'))))
cases.append(('J_reply_no_replyio',    # R2: reply->@replyio (third-party, dict)
    '@replyio' not in str(types('Add in-reply-to and references to reply emails'))))
cases.append(('J_rabbitmq_spaced_kept',  # R1 despaced-guard: real spaced node name
    'nodes-base.rabbitmq' in types('Rabbit MQ triggers do not work in test mode')))
cases.append(('J_typo_slak_still_corrects',  # R1 allows NON-dict typo (eval #46)
    'nodes-base.slack' in types('slak node auth fails')))
cases.append(('J_distinctive_3p_kept',  # non-dict third-party name still resolves bare
    'n8n-nodes-mcp.mcp' in types('MCP server trigger stops working')))
cases.append(('J_langchain_agent_kept',  # first-party langchain dict-word name kept
    'nodes-langchain.agent' in types('AI Agent does not store tool usage in memory')))

for name, ok in cases:
    print(f'{name}|{"PASS" if ok else "FAIL"}')
PYEOF

while IFS='|' read -r name result; do
  [ -z "$name" ] && continue
  if [ "$result" = "PASS" ]; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name"
    FAIL=$((FAIL + 1))
  fi
done < "$TMPOUT2"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
