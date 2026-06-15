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

python3 -c "
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
# 'workflow' is demoted, but the Pass-2 verb/plural stemmer matched the stem
# without re-checking the demotion list. This matters doubly for ingest-side
# node-tagging: thousands of issues mention 'workflows' in passing and would be
# falsely stamped node:workflowTrigger.
cases.append(('H_plural_workflows_no_workflowtrigger',
    'nodes-base.workflowTrigger' not in types('improve editor performance for large workflows')))
cases.append(('H_plural_workflows_with_real_node',
    'nodes-base.merge' in types('the merge node loses rows across large workflows')
    and 'nodes-base.workflowTrigger' not in types('the merge node loses rows across large workflows')))

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

for name, ok in cases:
    print(f'{name}|{\"PASS\" if ok else \"FAIL\"}')
" > "$TMPOUT2"

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
