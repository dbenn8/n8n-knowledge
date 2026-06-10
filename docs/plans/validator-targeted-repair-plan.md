# Validator Repair And Parity Plan

## Goal

Improve validator-driven repair without polluting evals:

1. Keep cloud and local validation behavior aligned at the raw validator layer.
2. Keep enrichment and repair orchestration in plugin/eval client code, not in a cloud-only wrapper.
3. Reduce long, drift-prone full-workflow rewrites by moving toward targeted, deterministic repair.

## Architecture Guardrails

### 1. Raw validator parity comes first

- Cloud validation must not silently normalize, repair, or forgive issues that local `n8n-mcp` would reject.
- The same workflow should produce the same raw validator result in both modes:
  - `valid`
  - `error_count`
  - `warning_count`
  - `errors`
  - `warnings`
  - `statistics`
  - `suggestions`
- Any transport wrapper around cloud validation should behave like a thin HTTP shell around the same validator logic, not a smarter validator.

### 2. Enrichment lives at the plugin boundary

- Structured issue records, repair prompts, candidate/validated artifacts, and deterministic patch application belong in this repo.
- The plugin and eval harness should enrich raw validator output the same way whether the source was:
  - local installed `n8n-mcp`
  - cloud validator endpoint
- This keeps local and cloud behavior aligned without patching the installed MCP package.

### 3. No teaching to the test

- Do not add prompt-specific or node-specific semantic hacks based on eval failures.
- Safe generic fixes are acceptable only when they are validator-derived and broadly correct.
- Example of acceptable scope:
  - add a missing leading `=` for an expression when the validator explicitly reports an expression-format error
- Examples to avoid:
  - hardcoded Slack operation remaps
  - hardcoded IF operator remaps
  - cloud-only auto-corrections that make invalid workflows appear valid

### 4. Plugin-only repair limits

- Eval-specific retry limits and validator-call caps apply only to the `plugin` condition.
- `mcp` and `bare` should keep their natural behavior and must not be hamstrung by plugin repair controls.

## Phase 0: Parity Baseline

Implement and keep:

- `scripts/eval/compare_validator_modes.py`
  - compare local raw validator output vs cloud raw validator output for the same workflow
  - fail loudly when parity breaks
- At least one intentionally invalid sample workflow that catches real parity drift
- A documented rule for the cloud/server side:
  - if cloud differs from local raw validator results, roll back cloud-only normalization before adding anything else

Exit criteria:

- the same sample workflow returns equivalent raw results in local and cloud modes
- cloud no longer reports `valid=true` when local reports `valid=false` for the same workflow

## Phase 1: Shared Enrichment Over Raw Results

Implement in this repo:

- Write `candidate.workflow.json` whenever the harness extracts a workflow from a response.
- Write or refresh `validated.workflow.json` whenever validation succeeds.
- Convert raw validator output into structured `issues` in shared client-side code.
- Build repair prompts from those structured issues with:
  - exact JSON paths where possible
  - current values when available
  - suggested minimal edits
  - explicit guidance to avoid full rewrites unless required

Exit criteria:

- local and cloud runs produce the same enriched repair inputs when their raw validator outputs match
- enrichment logic is not duplicated in a cloud-only path

## Phase 2: Deterministic Candidate Adoption

Implement next:

- If a candidate workflow validates, adopt that exact validated candidate as the final workflow artifact.
- Do not ask the model to regenerate a workflow that has already passed validation.
- Prefer deterministic replacement of the final response payload from the validated artifact over another free-form rewrite.

Exit criteria:

- validated workflows stop looping through unnecessary extra model turns
- final artifacts come from the last validator-valid candidate, not a fresh regeneration

## Phase 3: Targeted Deterministic Repairs

Implement later:

- Apply safe, generic field-level patches before invoking the model again.
- Let the model review a narrow patch plan instead of the whole workflow when a deterministic fix is not enough.
- Keep deterministic fixes validator-derived and general-purpose.

Exit criteria:

- fewer long-tail repair loops
- smaller repair prompts
- no eval-specific semantic hacks

## Server-Side Implication

The cloud/server repo should be aligned to this plan:

- keep the cloud endpoint thin and parity-focused
- remove cloud-only validator normalization that hides errors
- return raw validator output cleanly
- let this repo own enrichment, repair prompting, candidate adoption, and eval-specific orchestration
