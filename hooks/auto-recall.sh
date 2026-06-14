#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/runtime_dirs.sh"
source "$SCRIPT_DIR/lib/detect-n8n.sh"
source "$SCRIPT_DIR/lib/recall.sh"
source "$SCRIPT_DIR/lib/structured_recall.sh"

# Resolve + export NK_RUNTIME_DIR/NK_DEBUG_LOG/NK_STATE_DIR (per-user, 0700).
# The debug log contains prompt text, so it must NOT live in world-readable /tmp.
nk_runtime_init

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
import sys, os, datetime
sys.path.insert(0, '$SCRIPT_DIR/lib')
import runtime_dirs
debug_log = os.environ.get('NK_DEBUG_LOG') or runtime_dirs.debug_log_path()
os.umask(0o077)  # debug log holds prompt text — owner-only (0600), matches nk_debug_log_write
prompt_preview = sys.argv[1][:80].replace('\n', ' ')
with open(debug_log, 'a') as f:
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
GOTCHA_MULTI_DIR=$(mktemp -d)
MM_DIR=$(mktemp -d)
trap 'rm -f "$TMPFILE" "$STRUCT_TMPFILE" "$GOTCHA_TMPFILE"; rm -rf "$GOTCHA_MULTI_DIR" "$MM_DIR"' EXIT
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

MM_CONTENT=""
if [ -n "$NODE_TYPE" ]; then
  # --- Phase 1: Mental models from local cache (instant, <100ms) ---
  # Mental models are curated bug catalogs that change infrequently. Served from
  # disk cache (~/.cache/n8n-knowledge/mental-models/) with version-aware
  # invalidation via manifest (content hashes), falling back to 24h TTL.
  # Nodes covered by a mental model skip the expensive gotcha recall.
  GOTCHA_NODE_CAP="${CLAUDE_PLUGIN_OPTION_GOTCHANODECAP:-3}"
  UNCOVERED_NODES=""
  _gn=0
  for _nt in $NODE_TYPES; do
    [ "$_gn" -ge "$GOTCHA_NODE_CAP" ] && break
    do_mental_model_recall "$_nt" "$PROMPT" > "$MM_DIR/$_gn.txt" 2>/dev/null
    if [ -s "$MM_DIR/$_gn.txt" ]; then
      _mc=$(cat "$MM_DIR/$_gn.txt")
      [ -n "$_mc" ] && MM_CONTENT="${MM_CONTENT}${_mc}
"
    else
      UNCOVERED_NODES="${UNCOVERED_NODES} ${_nt}"
    fi
    _gn=$((_gn + 1))
  done

  # --- Phase 2: Remote calls only for what's still needed ---
  # Structured recall + gotcha (only for uncovered nodes) run in parallel with
  # the semantic recall that was already started above.
  do_structured_recall "$NODE_TYPE" > "$STRUCT_TMPFILE" 2>/dev/null &
  _gn=0
  for _nt in $UNCOVERED_NODES; do
    do_gotcha_recall "$_nt" > "$GOTCHA_MULTI_DIR/$_gn.json" 2>/dev/null &
    _gn=$((_gn + 1))
  done
  wait

  # Combine per-node gotcha results for uncovered nodes only.
  if [ -z "$MM_CONTENT" ] && [ "$_gn" -gt 0 ]; then
    python3 -c "
import glob, json, sys
combined, seen = [], set()
buckets = []
merged_sf = {}
for path in sorted(glob.glob(sys.argv[1] + '/*.json')):
    try:
        with open(path) as f:
            doc = json.load(f)
        buckets.append(doc.get('results', []))
        merged_sf.update(doc.get('source_facts') or {})
    except Exception:
        pass
i = 0
while len(combined) < 5 and any(len(b) > i for b in buckets):
    for b in buckets:
        if i < len(b):
            key = json.dumps(b[i], sort_keys=True)[:300]
            if key not in seen:
                seen.add(key)
                combined.append(b[i])
            if len(combined) >= 5:
                break
    i += 1
json.dump({'results': combined, 'source_facts': merged_sf}, open(sys.argv[2], 'w'))
" "$GOTCHA_MULTI_DIR" "$GOTCHA_TMPFILE" 2>/dev/null || true
  fi

  # Merge remaining recall streams.
  python3 -c "
import json, sys

def load_results(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

sem = load_results(sys.argv[1])
struct_doc = load_results(sys.argv[2])
gotcha_doc = load_results(sys.argv[3])
struct_results = struct_doc.get('results', [])[:5]
gotcha_results = gotcha_doc.get('results', [])[:5]
sem_results = sem.get('results', [])
sem['results'] = gotcha_results + sem_results + struct_results
sem['source_facts'] = {
    **(struct_doc.get('source_facts') or {}),
    **(gotcha_doc.get('source_facts') or {}),
    **(sem.get('source_facts') or {}),
}
with open(sys.argv[1], 'w') as f:
    json.dump(sem, f)
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

# Inject mental model content (curated bug catalog) before recall results.
# Mental models are pre-distilled from the same recall data but with much higher
# signal-to-noise ratio — they replace the noisy semantic gotcha recall.
if [ -n "$MM_CONTENT" ]; then
  MM_HEADER="## Known Issues (from curated knowledge base)
IMPORTANT: Review these known bugs BEFORE choosing your node implementation. Design around confirmed issues — do not suggest the broken path.

${MM_CONTENT}"
  RESULT=$(HOOK_JSON_EXTRA="$MM_HEADER" python3 "$SCRIPT_DIR/lib/hook_json.py" prepend UserPromptSubmit <<< "$RESULT" 2>/dev/null || echo "$RESULT")
fi

# Cap recall-only context at MAX_CTX (10K) so large recall results never spill to a file.
if [ -z "$DB_INJECT" ] && [ -z "$MM_CONTENT" ] && [ -n "$RESULT" ]; then
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
# Output written to $NK_DEBUG_LOG (~/.cache/n8n-knowledge/debug.log) — tail -f in another terminal to watch
DEBUG="${CLAUDE_PLUGIN_OPTION_DEBUGRECALL:-summary}"
if [ "$DEBUG" != "off" ] && [ -n "$RESULT" ]; then
  echo "$RESULT" | python3 -c "
import json, sys, os
sys.path.insert(0, '$SCRIPT_DIR/lib')
import runtime_dirs
from debug_formatter import format_debug
debug_log = os.environ.get('NK_DEBUG_LOG') or runtime_dirs.debug_log_path()
os.umask(0o077)  # debug log holds prompt/context text — owner-only (0600)
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
    with open(debug_log, 'a') as f:
        f.write(summary_line)
        f.write(format_debug(ctx, '$DEBUG', 'auto-recall'))
" 2>/dev/null || true
fi

echo "$RESULT"
