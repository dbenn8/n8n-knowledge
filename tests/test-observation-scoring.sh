#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"
PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then echo "  PASS: $desc"; PASS=$((PASS+1));
  else echo "  FAIL: $desc (expected '$expected', got '$actual')"; FAIL=$((FAIL+1)); fi
}
assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -q "$needle"; then echo "  PASS: $desc"; PASS=$((PASS+1));
  else echo "  FAIL: $desc (expected to contain '$needle')"; FAIL=$((FAIL+1)); fi
}
assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -q "$needle"; then echo "  FAIL: $desc (should NOT contain '$needle')"; FAIL=$((FAIL+1));
  else echo "  PASS: $desc"; PASS=$((PASS+1)); fi
}

echo "=== observation scoring tests ==="

# --- Task 1: source-aware scoring ---
strong_obs_level=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
cfg = fr.DEFAULTS
obs = {'type':'observation','text':'synth','tags':['type:community-post','source:discourse'],'metadata':{}}
strong = {'tags':['type:community-post','source:discourse','outcome:solved'],
          'metadata':{'like_count':'13','views':'3062','has_accepted_answer':'True'}}
level,_,_ = fr.score_result(obs, cfg, eng=strong)
print(level)
")
assert_eq "observation with strong source scores HIGH" "HIGH" "$strong_obs_level"

weak_obs_level=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
cfg = fr.DEFAULTS
obs = {'type':'observation','text':'synth','tags':['type:community-post','source:discourse'],'metadata':{}}
weak = {'tags':['source:discourse'],'metadata':{'views':'12'}}
level,_,_ = fr.score_result(obs, cfg, eng=weak)
print(level)
")
assert_eq "observation with weak source scores LOW" "LOW" "$weak_obs_level"

desc=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
print(fr.engagement_descriptor({'like_count':'13','views':'3062','has_accepted_answer':'True'},
                               ['source:discourse','outcome:solved']))
")
assert_contains "engagement_descriptor shows solved" "solved" "$desc"
assert_contains "engagement_descriptor shows likes" "13 likes" "$desc"
assert_contains "engagement_descriptor shows views" "3062 views" "$desc"

src=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
print(fr.detect_source(['type:github-issue','source:github']))
")
assert_eq "detect_source github" "github" "$src"

# --- Task 2: render_result ---
synth_block=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
r = {'type':'observation','text':'Gzip on the MCP endpoint breaks Claude Desktop; disable it.'}
sf_pairs = [('https://community.n8n.io/t/a/1', {'tags':['source:discourse','outcome:solved'],
             'metadata':{'url':'https://community.n8n.io/t/a/1','like_count':'13','views':'3062','has_accepted_answer':'True'}}),
            ('https://community.n8n.io/t/b/2', {'tags':['source:discourse'],'metadata':{'url':'https://community.n8n.io/t/b/2'}})]
print(fr.render_result(1, r, 'HIGH', True, sf_pairs, fr.DEFAULTS))
")
assert_contains "synthesis has result open tag" '<result n=\"1\" kind=\"synthesis\" confidence=\"HIGH\" sources=\"2\">' "$synth_block"
assert_contains "synthesis has close tag" "</result>" "$synth_block"
assert_contains "synthesis includes full text" "disable it." "$synth_block"
assert_contains "synthesis shows primary source engagement" "3062 views" "$synth_block"
assert_contains "synthesis lists extra source" "also: https://community.n8n.io/t/b/2" "$synth_block"
assert_contains "synthesis has verify note" "machine-distilled" "$synth_block"
assert_contains "synthesis note nudges fetch" "fetch a source URL" "$synth_block"

post_block=$(python3 -c "
import sys; sys.path.insert(0,'$LIB_DIR')
import format_results as fr
r = {'type':'world','text':'User cannot connect Claude Desktop to n8n Cloud MCP.',
     'tags':['type:community-post','source:discourse','outcome:solved'],
     'metadata':{'url':'https://community.n8n.io/t/x/9','like_count':'6','views':'563','has_accepted_answer':'True'}}
print(fr.render_result(2, r, 'HIGH', False, [], fr.DEFAULTS))
")
assert_contains "post has result open tag" '<result n=\"2\" kind=\"post\" confidence=\"HIGH\" source=\"community\">' "$post_block"
assert_contains "post includes Source line" "Source: https://community.n8n.io/t/x/9" "$post_block"
assert_contains "post shows views" "563 views" "$post_block"
assert_not_contains "post has no synthesis note" "machine-distilled" "$post_block"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
