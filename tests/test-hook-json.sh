#!/usr/bin/env bash
set -euo pipefail

# Tests for hooks/lib/hook_json.py — the shared hook-JSON helper extracted from
# the inline python blocks in auto-recall.sh and validate-workflow.sh.
# These pin the EXACT current semantics of:
#   - the emit-hookSpecificOutput wrapper (validate-workflow lines 59/140/255, auto-recall skip path)
#   - the merge-additionalContext-with-cap behavior (auto-recall recall-cap / db-inject / guidance blocks)
# MAX_CTX is 10000 and the truncation suffix is exactly '\n... (recall truncated to stay inline)'.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"
HJ="$LIB_DIR/hook_json.py"

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

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -Fq "$needle"; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== hook_json.py helper tests ==="

# --- MAX_CTX constant is the single source of truth ---
echo ""
echo "--- constant ---"
MAXCTX=$(python3 -c "import sys; sys.path.insert(0,'$LIB_DIR'); import hook_json; print(hook_json.MAX_CTX)")
assert_eq "MAX_CTX is 10000" "10000" "$MAXCTX"

# --- emit: hookSpecificOutput wrapper ---
echo ""
echo "--- emit wrapper ---"
# UserPromptSubmit wrapper (matches auto-recall skip path)
emit_ups=$(python3 "$HJ" emit UserPromptSubmit "hello guidance")
assert_eq "emit UserPromptSubmit exact json" \
  '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "hello guidance"}}' \
  "$emit_ups"

# PostToolUse wrapper (matches validate-workflow lines 59/140/255 — argv-passed text)
emit_ptu=$(python3 "$HJ" emit PostToolUse "Validator limit reached for this session.")
assert_eq "emit PostToolUse exact json" \
  '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Validator limit reached for this session."}}' \
  "$emit_ptu"

# Text with special chars must be JSON-escaped (em dash, quote, newline)
emit_esc=$(python3 "$HJ" emit PostToolUse $'line1\nuser\'s "quote" — dash')
assert_contains "emit escapes newline" '\n' "$emit_esc"
# json.dumps defaults to ensure_ascii -> the em dash is emitted as its \uXXXX escape
# (matches the pre-refactor inline blocks, which also used plain json.dumps).
# Build the expected escape from python so no literal Unicode sits in this test source.
EMDASH_ESC=$(python3 -c "print('\\\\u%04x' % ord('—'))")
assert_contains "emit ascii-escapes em dash" "$EMDASH_ESC" "$emit_esc"
# round-trips back to the original text
rt=$(printf '%s' "$emit_esc" | python3 -c "import json,sys;print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")
assert_eq "emit round-trips PostToolUse text" $'line1\nuser\'s "quote" — dash' "$rt"

# --- cap: recall-only cap (auto-recall lines 130-144) ---
echo ""
echo "--- cap (recall-only) ---"
# Under cap: passes through unchanged, preserves existing hookEventName.
small=$(python3 -c "import json;print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':'short ctx'}}))")
cap_small=$(printf '%s' "$small" | python3 "$HJ" cap)
assert_eq "cap under-limit unchanged" "$small" "$cap_small"

# Over cap: truncates to MAX_CTX + suffix.
over=$(python3 -c "import json;print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':'A'*12000}}))")
cap_over=$(printf '%s' "$over" | python3 "$HJ" cap)
ctxlen=$(printf '%s' "$cap_over" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['hookSpecificOutput']['additionalContext']))")
# 10000 chars + suffix length
SUFFIXLEN=$(python3 -c "print(len('\n... (recall truncated to stay inline)'))")
EXPLEN=$((10000 + SUFFIXLEN))
assert_eq "cap over-limit truncates to MAX_CTX+suffix" "$EXPLEN" "$ctxlen"
assert_contains "cap over-limit adds suffix" "(recall truncated to stay inline)" "$cap_over"

# Empty stdin: no output at all (exit 0).
cap_empty=$(printf '' | python3 "$HJ" cap)
assert_eq "cap empty stdin -> no output" "" "$cap_empty"

# Non-JSON stdin: echoes raw back.
cap_raw=$(printf 'not json here' | python3 "$HJ" cap)
assert_eq "cap non-json echoes raw" "not json here" "$cap_raw"

# --- prepend-cap: DB-inject style (auto-recall lines 151-172) ---
echo ""
echo "--- prepend-cap (db-inject) ---"
existing=$(python3 -c "import json;print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':'EXISTING'}}))")
pc=$(printf '%s' "$existing" | HOOK_JSON_EXTRA="EXTRA" python3 "$HJ" prepend-cap UserPromptSubmit)
pc_ctx=$(printf '%s' "$pc" | python3 -c "import json,sys;print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")
assert_eq "prepend-cap joins extra + blank line + existing" $'EXTRA\n\nEXISTING' "$pc_ctx"
pc_evt=$(printf '%s' "$pc" | python3 -c "import json,sys;print(json.load(sys.stdin)['hookSpecificOutput']['hookEventName'])")
assert_eq "prepend-cap forces UserPromptSubmit" "UserPromptSubmit" "$pc_evt"

# Empty stdin -> fresh wrapper with just extra.
pc_empty=$(printf '' | HOOK_JSON_EXTRA="ONLY EXTRA" python3 "$HJ" prepend-cap UserPromptSubmit)
assert_eq "prepend-cap empty stdin -> fresh wrapper" \
  '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ONLY EXTRA"}}' \
  "$pc_empty"

# Over cap: extra+existing truncated to MAX_CTX+suffix.
big_existing=$(python3 -c "import json;print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':'B'*12000}}))")
pc_over=$(printf '%s' "$big_existing" | HOOK_JSON_EXTRA="HEAD" python3 "$HJ" prepend-cap UserPromptSubmit)
pc_over_len=$(printf '%s' "$pc_over" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['hookSpecificOutput']['additionalContext']))")
assert_eq "prepend-cap over-limit truncates to MAX_CTX+suffix" "$EXPLEN" "$pc_over_len"

# --- prepend: validator-guidance style, NO cap (auto-recall lines 179-196) ---
echo ""
echo "--- prepend (guidance, no cap) ---"
gp=$(printf '%s' "$existing" | HOOK_JSON_EXTRA="GUIDE" python3 "$HJ" prepend UserPromptSubmit)
gp_ctx=$(printf '%s' "$gp" | python3 -c "import json,sys;print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")
assert_eq "prepend joins guide + blank line + existing" $'GUIDE\n\nEXISTING' "$gp_ctx"

# prepend with EMPTY existing context: no trailing blank line (separator suppressed).
empty_ctx=$(python3 -c "import json;print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':''}}))")
gp2=$(printf '%s' "$empty_ctx" | HOOK_JSON_EXTRA="GUIDE" python3 "$HJ" prepend UserPromptSubmit)
gp2_ctx=$(printf '%s' "$gp2" | python3 -c "import json,sys;print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")
assert_eq "prepend with empty existing -> no separator" "GUIDE" "$gp2_ctx"

# prepend does NOT cap (12k stays 12k+len(GUIDE)+2).
gp_over=$(printf '%s' "$big_existing" | HOOK_JSON_EXTRA="HEAD" python3 "$HJ" prepend UserPromptSubmit)
gp_over_len=$(printf '%s' "$gp_over" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['hookSpecificOutput']['additionalContext']))")
# HEAD + \n\n + 12000 B's = 4 + 2 + 12000
assert_eq "prepend does NOT truncate" "12006" "$gp_over_len"

# prepend empty stdin -> fresh wrapper with guidance.
gp_empty=$(printf '' | HOOK_JSON_EXTRA="ONLY GUIDE" python3 "$HJ" prepend UserPromptSubmit)
assert_eq "prepend empty stdin -> fresh wrapper" \
  '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ONLY GUIDE"}}' \
  "$gp_empty"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
