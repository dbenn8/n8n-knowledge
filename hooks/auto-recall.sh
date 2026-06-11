#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/detect-n8n.sh"
source "$SCRIPT_DIR/lib/recall.sh"
source "$SCRIPT_DIR/lib/structured_recall.sh"

# Build the workflow-validation build instructions (empty unless validation is enabled).
# Kept as a function so it can be injected on BOTH the recall-fired and recall-skipped
# paths — the build protocol must reach the model whenever the user is building a
# workflow, independent of whether semantic recall happened to trigger.
build_validator_guidance() {
  [ "${CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION:-false}" = "true" ] || return 0
  # Generic, real-world plugin behavior: explain the validator feedback loop the model
  # will receive. This does NOT dictate where to save (that is the host's/user's choice,
  # or — in the eval — a neutral directive in the shared system prompt for all conditions).
  printf '%s' "## n8n Workflow Build Instructions
Build the workflow by writing it to a .json file and editing THAT file as you go — the validator runs automatically on each file write and returns targeted feedback. The saved file is the importable deliverable; build it there rather than only describing or pasting it.
- INVALID: make only the targeted edits listed, re-write the file, wait for the next result
- VALID: verify the workflow fully solves the user's request. Tell the user the filename so they can import it directly into n8n."
}

# Check if auto-recall is enabled (default: true)
# Note: Claude Code uppercases all plugin option env var names
ENABLED="${CLAUDE_PLUGIN_OPTION_ENABLEAUTORECALL:-true}"
if [ "$ENABLED" = "false" ]; then
  exit 0
fi

# Read hook input from stdin
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('prompt',''))")
CWD=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('cwd',''))")

# Check if we should recall
RECALL_DECISION=$(should_recall "$PROMPT" "$CWD")
if [ "$RECALL_DECISION" != "yes" ]; then
  # Log skip reason for eval analysis
  DEBUG="${CLAUDE_PLUGIN_OPTION_DEBUGRECALL:-summary}"
  if [ "$DEBUG" != "off" ]; then
    python3 -c "
import sys, datetime
sys.path.insert(0, '$SCRIPT_DIR/lib')
prompt_preview = sys.argv[1][:80].replace('\n', ' ')
with open('/tmp/n8n-knowledge-debug.log', 'a') as f:
    f.write(f'[{datetime.datetime.now().strftime(\"%H:%M:%S\")}] auto-recall SKIP | prompt: {prompt_preview!r}\n')
" "$PROMPT" 2>/dev/null || true
  fi
  # Recall was skipped, but workflow build instructions (and eval-mode output folder)
  # must still reach the model if validation is enabled — they are not recall-dependent.
  SKIP_GUIDANCE=$(build_validator_guidance)
  if [ -n "$SKIP_GUIDANCE" ]; then
    python3 "$SCRIPT_DIR/lib/hook_json.py" emit UserPromptSubmit "$SKIP_GUIDANCE" 2>/dev/null || true
  fi
  exit 0
fi

# Call the recall API
TMPFILE=$(mktemp)
STRUCT_TMPFILE=$(mktemp)
GOTCHA_TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE" "$STRUCT_TMPFILE" "$GOTCHA_TMPFILE"' EXIT
do_recall "$PROMPT" "low" > "$TMPFILE"

# Check for node names in the prompt and do structured + gotcha recall if found
# Also capture all detected nodes for DB schema injection
PROMPT_JSON=$(printf '%s' "$PROMPT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
NODE_DETECT=$(python3 -c "
import sys, json
sys.path.insert(0, '$SCRIPT_DIR/lib')
from node_lookup import identify_nodes
prompt = json.loads(sys.stdin.read())
hits = identify_nodes(prompt)
if hits:
    print(hits[0][1])
    print(' '.join(nt for _, nt in hits))
" <<< "$PROMPT_JSON" 2>/dev/null || true)
NODE_TYPE=$(echo "$NODE_DETECT" | sed -n '1p')
NODE_TYPES=$(echo "$NODE_DETECT" | sed -n '2p')

if [ -n "$NODE_TYPE" ]; then
  # Run node-spec and gotcha recalls in parallel
  do_structured_recall "$NODE_TYPE" > "$STRUCT_TMPFILE" 2>/dev/null &
  do_gotcha_recall "$NODE_TYPE" > "$GOTCHA_TMPFILE" 2>/dev/null &
  wait

  # Merge: gotchas FIRST (highest priority), then semantic, then capped node specs
  python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        sem = json.load(f)
    with open(sys.argv[2]) as f:
        struct = json.load(f)
    gotcha_results = []
    try:
        with open(sys.argv[3]) as f:
            gotcha = json.load(f)
        gotcha_results = gotcha.get('results', [])[:5]
    except Exception:
        pass
    struct_results = struct.get('results', [])[:5]
    sem_results = sem.get('results', [])
    # Order: gotchas (bugs/issues) → semantic (docs/community) → node specs (reference)
    sem['results'] = gotcha_results + sem_results + struct_results
    with open(sys.argv[1], 'w') as f:
        json.dump(sem, f)
except Exception:
    pass
" "$TMPFILE" "$STRUCT_TMPFILE" "$GOTCHA_TMPFILE" 2>/dev/null || true
fi

# Inject compact node schema from nodes.db (valid operation enums per resource)
DB_INJECT=""
if [ -n "$NODE_TYPES" ]; then
  # shellcheck disable=SC2086  # word splitting intentional — NODE_TYPES is space-separated
  DB_INJECT=$(python3 "$SCRIPT_DIR/lib/nodes_db_inject.py" $NODE_TYPES 2>/dev/null || true)
fi

# Format and output results (pass CWD for .local.md config lookup)
RESULT=$(format_recall_results "$TMPFILE" "$CWD")

# Cap recall-only context at MAX_CTX (10K) so large recall results never spill to a file.
if [ -z "$DB_INJECT" ] && [ -n "$RESULT" ]; then
  RESULT=$(python3 "$SCRIPT_DIR/lib/hook_json.py" cap <<< "$RESULT" 2>/dev/null || echo "$RESULT")
fi

# DB injection goes FIRST — it contains must-have schema (valid operation enums).
# Recall results come after and can be truncated if needed.
# This ensures the critical build data is always inline, never spills to a skipped file.
if [ -n "$DB_INJECT" ]; then
  # Keep total additionalContext under MAX_CTX (10K) so it stays inline. DB inject is
  # prepended and always preserved — recall is trimmed by the shared cap helper.
  RESULT=$(HOOK_JSON_EXTRA="$DB_INJECT" python3 "$SCRIPT_DIR/lib/hook_json.py" prepend-cap UserPromptSubmit <<< "$RESULT" 2>/dev/null || echo "$RESULT")
fi

# Prepend validator behavioral guidance when workflow validation is enabled.
# This ensures the model understands the validation protocol in any deployment context.
VALIDATOR_GUIDANCE=$(build_validator_guidance)
if [ -n "$VALIDATOR_GUIDANCE" ]; then
  # Prepend guidance (no cap — guidance is short and must always survive intact).
  RESULT=$(HOOK_JSON_EXTRA="$VALIDATOR_GUIDANCE" python3 "$SCRIPT_DIR/lib/hook_json.py" prepend UserPromptSubmit <<< "$RESULT" 2>/dev/null || echo "$RESULT")
fi

# Debug mode: off, summary (default — condensed), full (complete with formatting)
# Runs AFTER DB inject merge so log reflects what Claude actually receives.
# Output written to /tmp/n8n-knowledge-debug.log — tail -f in another terminal to watch
DEBUG="${CLAUDE_PLUGIN_OPTION_DEBUGRECALL:-summary}"
if [ "$DEBUG" != "off" ] && [ -n "$RESULT" ]; then
  echo "$RESULT" | python3 -c "
import json, sys
sys.path.insert(0, '$SCRIPT_DIR/lib')
from debug_formatter import format_debug
data = json.load(sys.stdin)
ctx = data.get('hookSpecificOutput', {}).get('additionalContext', '')
if ctx:
    has_db = '## n8n Node Schema' in ctx
    if has_db:
        # Measure DB inject block: everything before recall preamble (or full ctx if no recall)
        db_end = ctx.find('*** n8n Knowledge Base')
        db_chars = db_end if db_end != -1 else len(ctx)
    else:
        db_chars = 0
    summary_line = f'[ctx={len(ctx)}chars, db_inject={has_db}, db_chars={db_chars}]\n'
    with open('/tmp/n8n-knowledge-debug.log', 'a') as f:
        f.write(summary_line)
        f.write(format_debug(ctx, '$DEBUG', 'auto-recall'))
" 2>/dev/null || true
fi

echo "$RESULT"
