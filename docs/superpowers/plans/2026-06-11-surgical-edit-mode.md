# Surgical-Edit Mode for the Validation Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-gated "surgical" repair mode to the workflow-validation loop: on INVALID feedback the model patches the failing nodes via structured `python3` edits (Bash) instead of regenerating the whole file, using a `!!DRAFT!!` first-line marker whose removal (via Edit) triggers re-validation — then smoke-test surgical vs rewrite on small/medium/complex prompts.

**Architecture:** The PostToolUse hook (`hooks/validate-workflow.sh`) gains two behaviors: (1) files beginning with the literal line `!!DRAFT!!` are skipped silently — no validation, no budget charge — so mid-draft Edits are free; (2) when `CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical`, the INVALID feedback replaces the "batch into one re-write" guidance with a surgical recipe (one Bash python3 script applying all fixes + writing the marker, then one tiny Edit deleting the marker to trigger validation). Default stays `rewrite` — zero behavior change unless opted in. The eval harness passes the style through via a new env var so we can A/B the two modes as plugin variants.

**Tech Stack:** bash hooks, python3 heredocs (existing pattern), bash test files under `tests/` (existing `test-*.sh` auto-discovery via `tests/run-all.sh`).

**Why (context for an engineer with zero history):** Today's INVALID feedback tells the model to "Batch ALL fixes below into one complete re-write — each file write spends one validation." On hard prompts this made single eval runs balloon (prompt-085: 2,695s, 172k output tokens — every repair round regenerates an 8–13k-token file). Cloud validator latency was measured at 0.37–0.69s/call, so validation itself is not the cost — full-file regeneration is. A surgical patch is ~100 output tokens per fix. The marker exists because Bash edits are invisible to the PostToolUse hook (it only fires on Write/Edit tool events): the script leaves `!!DRAFT!!` as line 1, and the model's marker-deleting Edit is the one hook-visible action that fires validation on the final state.

**Execution constraints:**
- Work happens in a worktree: `git worktree add .claude/worktrees/surgical-edits -b surgical-edits` from n8n-knowledge master (use superpowers:using-git-worktrees). Do NOT merge to master in this effort — Dan decides after the smoke results (this change alters the plugin eval condition; master must stay comparable to today's numbers until he calls it).
- Dispatch implementation to `tdd-implementer` agents (they have Bash and run their own tests; guard hook allowlists pytest/python3/node/npm test/bash tests/ and safe git). Orchestrator reviews diffs and runs the final full-suite gate.
- Task 6 (smoke runs) is ORCHESTRATOR-ONLY and gated on Dan's explicit approval of the full settings block. NO eval launches without it.
- No AI attribution in commit messages.

**Current-code facts the implementer needs:**
- Hook: `hooks/validate-workflow.sh` (255 lines). Option gate at line 9 (`CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION`), file-path filter at lines 23–26, budget/cap logic at lines 28–67, JSON sanity-copy at lines 72–85, VALID feedback heredoc at lines 110–152, INVALID feedback heredoc at lines 194–251. The INVALID budget line (lines 220–221) is the "complete re-write" wording being branched.
- Options arrive as env vars `CLAUDE_PLUGIN_OPTION_<UPPERCASEDNAME>`; the hook reads them directly (no python config layer involved for this hook).
- Option schema lives in `.claude-plugin/plugin.json` under `userConfig` (string options follow the `validatorMode` pattern at line 32).
- Eval harness passthrough block: `scripts/eval/run-eval-v2.sh` lines 369–375 (`plugin_env+=(...)` array).
- Test conventions: `tests/test-*.sh`, auto-discovered by `tests/run-all.sh`. `tests/test-validator-budget-line.sh` is the model: PASS/FAIL counters, `assert_contains`, and a SKIP gate when no local n8n-mcp validator exists (`ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db`). Tests that don't need a validator must NOT use the SKIP gate.
- An existing test pins the current INVALID wording — if `test-validator-budget-line.sh` asserts the literal "complete re-write" string, that assertion stays valid because default mode is unchanged.

---

## File Structure

- Modify: `hooks/validate-workflow.sh` — marker-skip check + INVALID wording branch (only file with behavior changes)
- Modify: `.claude-plugin/plugin.json` — new `workflowEditStyle` option
- Modify: `scripts/eval/run-eval-v2.sh` — env passthrough (1 line)
- Modify: `CHANGELOG.md` — 0.3.9-dev entry
- Create: `tests/test-surgical-edit-mode.sh` — all new behavior tests

---

### Task 1: Draft-marker skip in the validation hook

**Files:**
- Test: `tests/test-surgical-edit-mode.sh` (create)
- Modify: `hooks/validate-workflow.sh` (insert after line 26, before the `CAP=` line)

- [ ] **Step 1: Write the failing tests** — create `tests/test-surgical-edit-mode.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Surgical-edit mode tests.
# Part 1 (no validator needed): a workflow file whose first line is the literal
# !!DRAFT!! marker is a work-in-progress draft — the hook must exit silently
# with NO output and NO budget charge, in BOTH edit styles.
# Part 2 (needs local n8n-mcp validator, SKIPs otherwise): the INVALID feedback
# wording branches on CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/validate-workflow.sh"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected '$expected', got '$actual')"; FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -Fq "$needle"; then
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"; FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -Fq "$needle"; then
    echo "  FAIL: $desc (must NOT contain '$needle')"; FAIL=$((FAIL + 1))
  else
    echo "  PASS: $desc"; PASS=$((PASS + 1))
  fi
}

echo "=== surgical edit mode tests ==="

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

hook_input() {
  # $1 = session id, $2 = file path
  python3 - "$1" "$2" "$WORK_DIR" << 'PYEOF'
import json, sys
print(json.dumps({
    "session_id": sys.argv[1],
    "tool_name": "Write",
    "cwd": sys.argv[3],
    "tool_input": {"file_path": sys.argv[2]},
}))
PYEOF
}

# --- Part 1: marker skip (validator-free) ---

DRAFT_FILE="$WORK_DIR/draft.workflow.json"
printf '!!DRAFT!!\n{"nodes": [], "connections": {}}\n' > "$DRAFT_FILE"

SID="surgical-marker-test-$$"
STATE_FILE="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation/$SID.json"
rm -f "$STATE_FILE"

OUT=$(hook_input "$SID" "$DRAFT_FILE" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
assert_eq "draft-marker file produces no hook output" "" "$OUT"
assert_eq "draft-marker file does not charge the budget" "absent" "$([ -f "$STATE_FILE" ] && echo present || echo absent)"

OUT=$(hook_input "$SID" "$DRAFT_FILE" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
  CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK")
assert_eq "draft-marker skip also applies in surgical mode" "" "$OUT"

# A normal (markerless) file must still flow past the marker check. We prove it
# by confirming the budget state file IS created for a markerless invalid-JSON
# file (the hook charges budget before the JSON sanity copy).
PLAIN_FILE="$WORK_DIR/plain.workflow.json"
printf '{"nodes": [], "connections": {}}\n' > "$PLAIN_FILE"
SID2="surgical-plain-test-$$"
STATE_FILE2="${TMPDIR:-/tmp}/n8n-knowledge-workflow-validation/$SID2.json"
rm -f "$STATE_FILE2"
hook_input "$SID2" "$PLAIN_FILE" | CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK" > /dev/null || true
assert_eq "markerless file still reaches budget accounting" "present" "$([ -f "$STATE_FILE2" ] && echo present || echo absent)"
rm -f "$STATE_FILE" "$STATE_FILE2"

# --- Part 2: feedback wording branch (needs local validator) ---

if ! ls "$HOME"/.npm/_npx/*/node_modules/n8n-mcp/data/nodes.db >/dev/null 2>&1; then
  echo "  SKIP: no local n8n-mcp install — wording-branch tests not exercised"
else
  INVALID_FILE="$WORK_DIR/invalid.workflow.json"
  cat > "$INVALID_FILE" << 'JSONEOF'
{
  "nodes": [
    {
      "id": "1",
      "name": "Slack",
      "type": "n8n-nodes-base.slack",
      "typeVersion": 2.2,
      "position": [0, 0],
      "parameters": {"resource": "message", "operation": "bogusOperation"}
    }
  ],
  "connections": {}
}
JSONEOF

  SURGICAL_OUT=$(hook_input "surgical-wording-$$" "$INVALID_FILE" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true \
    CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash "$HOOK")
  assert_contains "surgical mode names the marker" "!!DRAFT!!" "$SURGICAL_OUT"
  assert_contains "surgical mode instructs Bash python3 edits" "python3" "$SURGICAL_OUT"
  assert_contains "surgical mode says do not rewrite" "do NOT rewrite" "$SURGICAL_OUT"
  assert_contains "surgical mode says marker removal must use Edit" "Edit tool" "$SURGICAL_OUT"
  assert_not_contains "surgical mode drops the re-write guidance" "one complete re-write" "$SURGICAL_OUT"

  REWRITE_OUT=$(hook_input "rewrite-wording-$$" "$INVALID_FILE" | \
    CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true bash "$HOOK")
  assert_contains "default mode keeps re-write guidance" "one complete re-write" "$REWRITE_OUT"
  assert_not_contains "default mode has no marker instructions" "!!DRAFT!!" "$REWRITE_OUT"
fi

echo ""
echo "surgical-edit-mode: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2: Run the test to verify Part 1 fails**

Run: `bash tests/test-surgical-edit-mode.sh`
Expected: FAIL on "draft-marker file produces no hook output" — current hook validates the draft (or errors on the non-JSON marker line and exits silently AFTER charging budget, so "does not charge the budget" FAILs). Part 2 also fails (no marker/wording yet) or SKIPs without a local validator. At least one Part-1 FAIL is required before implementing.

- [ ] **Step 3: Implement the marker skip** in `hooks/validate-workflow.sh`. Insert between the file-extension `case` block (ends line 26, `esac`) and the `CAP=` line (line 28):

```bash
# Surgical-edit draft marker: a file whose first line is the literal !!DRAFT!!
# is a work-in-progress draft (the model is mid-surgical-edit via Bash). Skip
# silently — no validation, no budget charge. The model deletes the marker via
# the Edit tool when done; THAT Edit triggers validation of the final state.
DRAFT_MARKER='!!DRAFT!!'
case "$(head -c 9 "$FILE_PATH" 2>/dev/null)" in
  "$DRAFT_MARKER") exit 0 ;;
esac
```

- [ ] **Step 4: Run the test — Part 1 must pass**

Run: `bash tests/test-surgical-edit-mode.sh`
Expected: all three Part-1 assertions PASS plus "markerless file still reaches budget accounting" PASS. Part 2 still FAILs (wording not implemented — that's Task 2) or SKIPs.

- [ ] **Step 5: Regression check + commit**

Run: `bash tests/test-validator-budget-line.sh && bash tests/test-workflow-validation.sh`
Expected: PASS (or their documented SKIP without a local validator).

```bash
git add tests/test-surgical-edit-mode.sh hooks/validate-workflow.sh
git commit -m "feat: skip validation and budget charge on !!DRAFT!! marker files"
```

---

### Task 2: Surgical feedback wording branch

**Files:**
- Modify: `hooks/validate-workflow.sh` (INVALID feedback heredoc, lines ~194–251)
- Test: `tests/test-surgical-edit-mode.sh` Part 2 (already written in Task 1)

- [ ] **Step 1: Confirm Part 2 currently fails** (with a local validator present)

Run: `bash tests/test-surgical-edit-mode.sh`
Expected: Part 2 assertions FAIL (surgical wording absent). If the environment has no local validator, Part 2 SKIPs — in that case run the SAME commands against the cloud validator by exporting `CLAUDE_PLUGIN_OPTION_VALIDATORMODE=cloud CLAUDE_PLUGIN_OPTION_VALIDATORCLOUDURL=https://n8nhindsight.applikuapp.com/public/validate-workflow` manually to observe the failure, but leave the committed test gated on the local validator only (matching existing convention).

- [ ] **Step 2: Implement the wording branch.** Two edits to `hooks/validate-workflow.sh`:

(a) Pass the style into the INVALID heredoc env (line 194). Change:

```bash
CTX=$(RESULT_JSON="$RESULT" FILE_PATH="$FILE_PATH" AUTOFIX_JSON="$AUTOFIX_JSON" NODE_SPECS="$NODE_SPEC_BLOCK" CALLS_USED="$CALLS_USED" CAP="$CAP" python3 - 2>/dev/null << 'PYEOF'
```

to:

```bash
CTX=$(RESULT_JSON="$RESULT" FILE_PATH="$FILE_PATH" AUTOFIX_JSON="$AUTOFIX_JSON" NODE_SPECS="$NODE_SPEC_BLOCK" CALLS_USED="$CALLS_USED" CAP="$CAP" EDIT_STYLE="${CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE:-rewrite}" python3 - 2>/dev/null << 'PYEOF'
```

(b) Inside that heredoc, replace the single budget line in the `body` list (currently):

```python
    f"Validator budget: {calls_used} of {cap} calls used ({remaining} remaining). "
    "Batch ALL fixes below into one complete re-write — each file write spends one validation.",
```

with:

```python
    f"Validator budget: {calls_used} of {cap} calls used ({remaining} remaining). " + (
        (
            "Fix via SURGICAL EDITS — do NOT rewrite the file; rewrites waste tokens and time. Recipe:\n"
            "  1. Run ONE Bash python3 script that loads the JSON file at the path above, applies "
            "EVERY fix listed below, and writes the file back with the literal first line !!DRAFT!! "
            "immediately followed by the JSON on the next line.\n"
            "  2. Delete the !!DRAFT!! line using the Edit tool "
            "(old_string: '!!DRAFT!!\\n{', new_string: '{'). That Edit triggers re-validation — "
            "it is the ONLY step that spends validation budget. Do NOT remove the marker with Bash; "
            "the validator will not see the file."
        )
        if os.environ.get("EDIT_STYLE", "rewrite") == "surgical"
        else "Batch ALL fixes below into one complete re-write — each file write spends one validation."
    ),
```

The VALID-path heredoc is intentionally untouched (its wording is pinned by `test-validator-budget-line.sh`; the surgical recipe only matters on INVALID rounds).

- [ ] **Step 3: Run the full new test file**

Run: `bash tests/test-surgical-edit-mode.sh`
Expected: ALL assertions PASS (Part 2 requires a local n8n-mcp install; on the dev machine one exists at `~/.npm/_npx/*/node_modules/n8n-mcp/`).

- [ ] **Step 4: Regression run**

Run: `bash tests/test-validator-budget-line.sh && bash tests/test-workflow-validation.sh && bash tests/test-hook-json.sh`
Expected: PASS — default-mode wording is byte-identical to before.

- [ ] **Step 5: Commit**

```bash
git add hooks/validate-workflow.sh
git commit -m "feat: surgical-edit feedback recipe behind workflowEditStyle option"
```

---

### Task 3: plugin.json option + CHANGELOG

**Files:**
- Modify: `.claude-plugin/plugin.json` (userConfig block)
- Modify: `CHANGELOG.md` (top)

- [ ] **Step 1: Add the option** after the `workflowValidationMaxCalls` entry (line 24 of `.claude-plugin/plugin.json`):

```json
    "workflowEditStyle": { "type": "string", "title": "Workflow Edit Style", "description": "How validator feedback asks the model to repair an invalid workflow. 'rewrite' (default): batch all fixes into one full re-write. 'surgical': patch only failing nodes via scripted JSON edits using a !!DRAFT!! marker, then trigger one re-validation — far fewer output tokens on large workflows. Experimental.", "default": "rewrite" },
```

- [ ] **Step 2: Validate the JSON parses**

Run: `python3 -c "import json; json.load(open('.claude-plugin/plugin.json')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Add CHANGELOG entry** at the top of `CHANGELOG.md` (above the `## 0.3.8` heading):

```markdown
## Unreleased

### Surgical-edit repair mode (experimental, off by default)
- New `workflowEditStyle` option (`rewrite` | `surgical`). In surgical mode, INVALID validator feedback instructs the model to patch only the failing nodes via a scripted JSON edit and a `!!DRAFT!!` draft marker, instead of regenerating the whole file — cutting output tokens on large-workflow repair rounds by ~99% per fix.
- The validation hook skips files whose first line is `!!DRAFT!!` (work-in-progress drafts): no validation, no budget charge, in all modes.
```

- [ ] **Step 4: Commit**

```bash
git add .claude-plugin/plugin.json CHANGELOG.md
git commit -m "feat: workflowEditStyle plugin option (rewrite|surgical)"
```

---

### Task 4: Eval harness passthrough

**Files:**
- Modify: `scripts/eval/run-eval-v2.sh` (after line 375, inside the same `plugin_env` block)

- [ ] **Step 1: Add the passthrough line** directly after the `EVAL_PLUGIN_VALIDATOR_LOCAL_PATH` line (375):

```bash
        [ -n "${EVAL_PLUGIN_WORKFLOW_EDIT_STYLE:-}" ] && plugin_env+=("CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=${EVAL_PLUGIN_WORKFLOW_EDIT_STYLE}")
```

- [ ] **Step 2: Syntax check**

Run: `bash -n scripts/eval/run-eval-v2.sh`
Expected: no output (clean parse).

- [ ] **Step 3: Verify the wiring end-to-end without launching a run**

Run: `grep -n "WORKFLOWEDITSTYLE" scripts/eval/run-eval-v2.sh hooks/validate-workflow.sh .claude-plugin/plugin.json`
Expected: one hit in each of the three files, names matching exactly (`CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE` in harness + hook; `workflowEditStyle` in plugin.json).

- [ ] **Step 4: Commit**

```bash
git add scripts/eval/run-eval-v2.sh
git commit -m "feat: EVAL_PLUGIN_WORKFLOW_EDIT_STYLE passthrough for eval harness"
```

---

### Task 5: Full-suite gate (orchestrator)

- [ ] **Step 1: Run the entire plugin suite**

Run: `bash tests/run-all.sh`
Expected: `ALL TESTS PASSED` (validator-gated files may SKIP per their own notices; zero FAILs).

- [ ] **Step 2: Run python tests if present**

Run: `cd tests/python 2>/dev/null && python3 -m pytest -q || echo "no python suite here"`
Expected: green or explicit absence.

- [ ] **Step 3: Manual end-to-end sanity of the surgical flow (no model needed)** — simulate the model's recipe by hand from the worktree root:

```bash
WORK=$(mktemp -d)
cat > "$WORK/wf.workflow.json" << 'EOF'
{"nodes": [{"id": "1", "name": "Slack", "type": "n8n-nodes-base.slack", "typeVersion": 2.2, "position": [0,0], "parameters": {"resource": "message", "operation": "bogusOperation"}}], "connections": {}}
EOF
# step 1 of the recipe: scripted fix + marker
python3 - "$WORK/wf.workflow.json" << 'EOF'
import json, sys
p = sys.argv[1]
wf = json.load(open(p))
wf["nodes"][0]["parameters"]["operation"] = "post"
with open(p, "w") as f:
    f.write("!!DRAFT!!\n")
    json.dump(wf, f, indent=2)
EOF
# hook must skip the draft silently
printf '{"session_id":"e2e-%s","tool_name":"Edit","cwd":"%s","tool_input":{"file_path":"%s/wf.workflow.json"}}' $$ "$WORK" "$WORK" | \
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash hooks/validate-workflow.sh
echo "draft skip output above should be EMPTY"
# step 2 of the recipe: remove marker (simulating the Edit), then the hook validates
python3 - "$WORK/wf.workflow.json" << 'EOF'
import sys
p = sys.argv[1]
text = open(p).read()
open(p, "w").write(text.removeprefix("!!DRAFT!!\n"))
EOF
printf '{"session_id":"e2e-%s","tool_name":"Edit","cwd":"%s","tool_input":{"file_path":"%s/wf.workflow.json"}}' $$ "$WORK" "$WORK" | \
  CLAUDE_PLUGIN_OPTION_ENABLEWORKFLOWVALIDATION=true CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE=surgical bash hooks/validate-workflow.sh
echo "markerless validation output above should be NON-empty validator feedback"
rm -rf "$WORK"
```

Expected: first hook invocation prints nothing; second prints a `*** n8n Workflow Validator ***` block.

- [ ] **Step 4: Push the branch** (worktree branch only — NOT master)

```bash
git push -u origin surgical-edits
```

---

### Task 6: Smoke test — surgical vs rewrite (ORCHESTRATOR ONLY, Dan-approval gated)

**Design** (present to Dan with the full settings block and get explicit approval BEFORE launching):

- **Prompts (9):** small = indices `0, 3, 6` (single-node group A — Slack post, Notion page, Jira issue); medium = `66, 70, 76` (from today's mid-13 set; 76 was a DeepSeek-mcp failure); complex = `79, 85, 94` (85 is the 2,695s/172k-token rewrite pathology — the headline test case; 94 failed mcp on both models).
- **Arms (2):** identical settings except `EVAL_PLUGIN_WORKFLOW_EDIT_STYLE=surgical` vs unset (rewrite). Condition: `plugin` only — this is plugin-vs-plugin, mcp adds nothing here.
- **Models:** Sonnet first (18 sessions). DeepSeek as a follow-up adherence check if Sonnet looks good (the recipe's Edit-not-Bash marker removal is the instruction-following risk for weaker models).
- **Command shape** (run from the WORKTREE root so the worktree's hooks are exercised):

```bash
EVAL_PROMPT_FILE_IDXS=0,3,6,66,70,76,79,85,94 \
EVAL_PLUGIN_WORKFLOW_EDIT_STYLE=surgical \
EVAL_CONDITIONS_PARALLEL=1 EVAL_ENABLE_PLUGIN_WORKFLOW_VALIDATION=1 \
EVAL_PLUGIN_VALIDATOR_MODE=cloud \
EVAL_PLUGIN_VALIDATOR_CLOUD_URL=https://n8nhindsight.applikuapp.com/public/validate-workflow \
EVAL_SCORING_VALIDATOR_MODE=cloud \
EVAL_PLUGIN_WORKFLOW_VALIDATION_MAX_CALLS=10 EVAL_KEEP_TRANSCRIPTS=1 \
bash scripts/eval/run-eval-v2.sh --conditions plugin --runs 1 \
  --model-timeout-seconds 0 --max-in-flight-runs 16
```

(rewrite arm: same command without `EVAL_PLUGIN_WORKFLOW_EDIT_STYLE`.) Transcripts ON — we need to read whether the model actually followed the recipe.

- **Success criteria:** (1) validated rate not worse than the rewrite arm; (2) output tokens on complex prompts materially lower (target: prompt-085 under half its rewrite-arm output tokens); (3) transcripts show the recipe followed — scripted python edit, marker present mid-round, marker removed via Edit (not Bash); (4) zero budget charges on draft-state edits (validator_calls in meta should not exceed rewrite arm's).
- **Monitoring:** 5-minute cadence with full stats tables in message text, % diffs on every comparative row, median+mean time.

- [ ] Step 1: Present design + verbatim settings to Dan; wait for explicit approval
- [ ] Step 2: Launch rewrite arm, then surgical arm (or both interleaved — confirm with Dan)
- [ ] Step 3: Compare per the success criteria; read transcripts of all complex-prompt surgical runs
- [ ] Step 4: Report verdict + recommendation (adopt for full A+B+C, iterate wording, or drop)

---

## Self-Review Notes

- Spec coverage: config option (Task 3), marker protocol + hook skip (Task 1), feedback branch (Task 2), harness passthrough (Task 4), smoke test (Task 6) — all covered. ✔
- The marker-skip is mode-independent by design (a draft file should never be validated even if the option is off — a model could only produce one when instructed, but skipping is always safe and the test pins it in both modes). ✔
- Type/name consistency: `workflowEditStyle` ↔ `CLAUDE_PLUGIN_OPTION_WORKFLOWEDITSTYLE` ↔ `EVAL_PLUGIN_WORKFLOW_EDIT_STYLE`; marker literal `!!DRAFT!!` (9 bytes — `head -c 9` matches exactly). ✔
- `head -c 9` vs first-line semantics: the case-match requires the first 9 bytes to equal `!!DRAFT!!`; recipe always writes it followed by `\n{`, and an n8n workflow JSON can never begin with `!`. ✔
- Known risk (accepted, smoke test measures it): model removes marker via Bash → validation never fires that round. Mitigated by explicit "Do NOT remove the marker with Bash" wording; transcript review in Task 6 checks adherence.
