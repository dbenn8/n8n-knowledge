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

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
