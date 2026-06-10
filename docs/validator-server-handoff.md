# Validator Server Handoff

This note is for a separate Codex session working in the server/proxy repo.

Goal:
- expose the n8n workflow validator over HTTP from the server side
- let the plugin or eval harness call it remotely
- preserve raw parity with local `n8n-mcp` validation
- keep enrichment and repair orchestration outside the cloud wrapper

## Recommendation

Yes, the design makes sense.

Recommended path:
1. Add a new validator endpoint on the same server/proxy family that already exposes the public recall endpoint.
2. Keep the endpoint narrow and deterministic: accept workflow JSON or full model response text, run extraction + validation, and return raw validator output without cloud-only fixes.
3. Do not copy the current local `npx` cache-path hack into production. Install `n8n-mcp` as a normal dependency in the server container and initialize its validator once at process start.
4. Keep automatic repair orchestration, structured issue enrichment, and candidate/validated artifact handling in the plugin or eval harness.

This keeps the architecture clean:
- cloud is a transport layer over validator execution
- this repo owns enrichment and repair behavior
- local and cloud can stay in parity

## Important correction

We now have direct evidence of parity drift between local and cloud modes on the same invalid workflow.

That means the cloud layer must be treated as a thin wrapper and any cloud-only normalization or auto-repair should be removed or rolled back until parity is restored.

The intended design is:
- cloud returns raw validator results
- local returns raw validator results
- plugin/eval shared code turns those raw results into structured repair guidance

## Current Local Implementation

The local validator has 3 layers.

### 1. Workflow extraction + validation helpers

File:
- [scripts/eval/workflow_validation.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/workflow_validation.py:1)

Responsibilities:
- extract workflow JSON from a model response
- validate the workflow JSON using `n8n-mcp`
- summarize errors into short repair messages
- build a repair prompt from the original user prompt + validator feedback

Key functions:
- `extract_workflow_json(response_text)`
- `validate_with_mcp(workflow_json)`
- `inspect_response_text(response_text, max_errors=8)`
- `inspect_response_file(response_file, max_errors=8)`
- `build_repair_prompt(original_prompt, response_text, inspection)`

Behavior:
- first tries fenced ```json blocks
- then tries brace-balanced JSON extraction from free text
- counts `has_json=false` when no extractable workflow object exists
- returns repair guidance as bullet-ready strings

### 2. CLI wrapper that emits machine-friendly JSON

File:
- [scripts/eval/validator_feedback.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validator_feedback.py:1)

Responsibilities:
- inspect one eval response file
- emit compact JSON with:
  - `valid`
  - `has_json`
  - `extract_error`
  - `error_count`
  - `warning_count`
  - `node_count`
  - `trigger_count`
  - `repair_messages`
  - `feedback_block`
- optionally write a repair prompt file if given:
  - `--original-prompt-file`
  - `--repair-prompt-file`

This is the closest local equivalent to the HTTP contract we likely want on the server.

### 3. Node-based bridge into n8n-mcp's validator

File:
- [scripts/eval/validate-with-mcp.js](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validate-with-mcp.js:1)

Responsibilities:
- read workflow JSON from stdin
- instantiate `n8n-mcp` validator internals
- validate nodes, connections, and expressions with `profile: "runtime"`
- emit JSON:
  - `valid`
  - `error_count`
  - `warning_count`
  - `errors[]`
  - `warnings[]`
  - `statistics`
  - `suggestions`

Important:
- the current local script resolves `n8n-mcp` from a hard-coded `~/.npm/_npx/...` cache path
- that is fine for local experiments but not acceptable for server/docker production

## Current Validator Output Shape

There are really 2 useful output layers.

### Raw validator output

Current raw `n8n-mcp`-style output:

```json
{
  "valid": false,
  "error_count": 2,
  "warning_count": 1,
  "errors": [
    {
      "type": "schema_error",
      "message": "Required property 'Name' cannot be empty",
      "node": "Salesforce"
    }
  ],
  "warnings": [],
  "statistics": {
    "totalNodes": 4,
    "triggerNodes": 1
  },
  "suggestions": []
}
```

### Higher-level inspection output

Current `validator_feedback.py` output:

```json
{
  "valid": false,
  "has_json": true,
  "extract_error": null,
  "error_count": 2,
  "warning_count": 1,
  "node_count": 4,
  "trigger_count": 1,
  "repair_messages": [
    "Required property 'Name' cannot be empty",
    "Expression format error in node ..."
  ],
  "feedback_block": "- Required property 'Name' cannot be empty\n- Expression format error in node ..."
}
```

Recommendation:
- make the server endpoint return raw validator output first
- optional higher-level inspection is acceptable only if it is derived mechanically and does not alter validity semantics
- any shared enriched shape should be produced from the same client-side code path for both local and cloud modes

## Recommended Server Endpoint

### Best first endpoint

Suggested endpoint:
- `POST /public/validate-workflow`

If you want tighter control:
- `POST /v1/validate-workflow`
- require the same auth pattern the server already uses for non-public routes

If the plugin already talks to a public proxy endpoint comfortably, a public endpoint with a server-side shared secret or project token is also fine.

### Recommended request body

Support both of these input modes:

```json
{
  "response_text": "...full model response...",
  "original_prompt": "...optional original user prompt...",
  "max_errors": 8,
  "include_repair_prompt": true
}
```

or

```json
{
  "workflow": {
    "nodes": [],
    "connections": {}
  },
  "original_prompt": "...optional original user prompt...",
  "max_errors": 8,
  "include_repair_prompt": true
}
```

Recommendation:
- accept either `response_text` or `workflow`
- if both are provided, prefer `workflow`
- if only `response_text` is provided, do extraction server-side

### Recommended response body

```json
{
  "valid": false,
  "has_json": true,
  "extract_error": null,
  "error_count": 2,
  "warning_count": 1,
  "node_count": 4,
  "trigger_count": 1,
  "repair_messages": [
    "Required property 'Name' cannot be empty",
    "Expression format error in node ..."
  ],
  "feedback_block": "- Required property 'Name' cannot be empty\n- Expression format error in node ...",
  "repair_prompt": "Revise the n8n workflow JSON so it passes validator checks....",
  "errors": [
    {
      "type": "schema_error",
      "message": "Required property 'Name' cannot be empty",
      "node": "Salesforce"
    }
  ],
  "warnings": [],
  "statistics": {
    "totalNodes": 4,
    "triggerNodes": 1
  }
}
```

Behavior recommendation:
- `200 OK` for valid and invalid workflows alike
- reserve `4xx/5xx` for malformed request or service failure

## What The Plugin / Harness Actually Needs

Minimum useful fields:
- `valid`
- `has_json`
- `extract_error`
- `repair_messages`
- `feedback_block`

Useful extra fields:
- `error_count`
- `warning_count`
- `node_count`
- `trigger_count`
- raw `errors` and `warnings`
- optional `repair_prompt`

For the eval harness, this is enough.
For future plugin auto-repair, this is also enough.

## Why Enrichment Should Not Be Cloud-Only

Advantages of keeping enrichment in the plugin/eval client:
- local and cloud can share the exact same enrichment logic
- no parity drift from cloud-only response shaping
- no need to patch the installed local MCP package
- easier to keep evals clean and avoid teaching to the test
- server remains simpler and easier to reason about

The server should still own:
- HTTP transport
- validator process lifecycle
- auth, logging, and rate limiting

The plugin/eval client should own:
- structured issues
- repair prompts
- candidate vs validated artifact tracking
- deterministic replacement or patch application

Tradeoff:
- introduces network dependency and latency

Given you already have a public recall proxy in place, this tradeoff is reasonable.

## Strong Recommendation About Implementation Language

Recommended:
- implement the server-side validator path in Node/TypeScript if the server already supports it cleanly

Reason:
- `n8n-mcp` validator internals are already Node-based
- this avoids Python -> subprocess -> Node nesting on the server
- you can initialize validator dependencies once and reuse them across requests

Acceptable alternative:
- keep a thin Python or other-language endpoint that shells out to Node

I would only use the shell-out approach if the server repo makes Node embedding awkward.

## Server-Side Dependencies

The server-side session should expect to need:
- `node`
- `n8n-mcp` installed as a regular dependency
- whatever package manager the server repo uses
- access to the `nodes.db` shipped with `n8n-mcp`

From the current local script:
- `SQLiteStorageService`
- `NodeRepository`
- `WorkflowValidator`
- `EnhancedConfigValidator`

These are imported in:
- [scripts/eval/validate-with-mcp.js](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validate-with-mcp.js:18)

## Important Production Difference From Local

Do not reuse this local logic directly:
- [scripts/eval/validate-with-mcp.js](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validate-with-mcp.js:16)

Why:
- it hard-codes an `npx` cache directory under `~/.npm/_npx/...`
- that path is brittle and machine-specific

Instead:
- install `n8n-mcp`
- import it from normal `node_modules`
- resolve its bundled DB path from the package location

## Recommended Endpoint Internals

At server startup:
1. initialize `SQLiteStorageService`
2. initialize `NodeRepository`
3. initialize `EnhancedConfigValidator`
4. create a reusable `WorkflowValidator`
5. keep it warm as a singleton

At request time:
1. parse request body
2. extract workflow JSON if needed
3. if no extractable workflow exists:
   - return `valid=false`
   - `has_json=false`
   - `extract_error=no_json_found`
   - include repair guidance telling the caller to return one importable workflow JSON block
4. if workflow exists:
   - run validator
   - summarize errors into short deduped repair messages
   - optionally build a repair prompt when `original_prompt` is present and requested

## Repair Message Logic To Preserve

The useful part is not just raw validation.
The useful part is turning raw errors into a compact list the model can act on.

Current summarization behavior lives in:
- [scripts/eval/workflow_validation.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/workflow_validation.py:82)

It:
- normalizes whitespace
- dedupes messages
- caps the list at `max_errors` default `8`

That behavior should carry over.

## Repair Prompt Logic To Preserve

Current prompt builder lives in:
- [scripts/eval/workflow_validation.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/workflow_validation.py:146)

It includes:
- original user request
- validator feedback block
- explicit rules:
  - preserve requested behavior
  - fix schema/typeVersion/expression/required-field issues first
  - return exactly one corrected workflow JSON block
  - no prose before/after JSON
- current workflow draft JSON when extraction succeeded
- otherwise the previous raw response text

This is a good phase-2 contract even if phase 1 only returns `feedback_block`.

## Recommended Phase Split

### Phase 1

Server endpoint returns validation + repair guidance only.

Client behavior:
- model produces workflow
- client calls validator endpoint
- invalid result is surfaced to the caller or logged in evals

### Phase 2

Add automatic repair loop on the client side.

Client behavior:
1. model produces workflow
2. client calls validator endpoint
3. if invalid:
   - build follow-up prompt from `repair_prompt` or `feedback_block`
   - ask model for corrected workflow
   - validate again
   - repeat up to max attempts

That mirrors the local eval harness behavior already implemented here.

## Current Local Repair Loop Reference

The current eval harness repair loop is in:
- [scripts/eval/run-eval-v2.sh](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/run-eval-v2.sh:182)

Useful existing behavior:
- `EVAL_REPAIR_INVALID=1`
- `EVAL_REPAIR_MAX_ATTEMPTS=3`
- writes `.attemptNN.json` sidecars
- writes final `.validation.json`
- aggregates total cost/tokens/turns across attempts

## Example Curl Contract

```bash
curl -sS -X POST "https://your-server.example.com/public/validate-workflow" \
  -H "Content-Type: application/json" \
  -d '{
    "response_text": "Here is the workflow...```json\n{\"nodes\":[],\"connections\":{}}\n```",
    "original_prompt": "Build an n8n workflow that posts to Slack when a webhook fires",
    "max_errors": 8,
    "include_repair_prompt": true
  }'
```

## Suggested Acceptance Tests In The Server Repo

Minimum tests:
1. valid workflow JSON returns `valid=true`
2. invalid workflow JSON returns `valid=false` with actionable `repair_messages`
3. prose-only response returns `has_json=false`
4. fenced JSON extraction works
5. bare embedded JSON extraction works
6. repeated validator messages are deduped
7. `include_repair_prompt=true` returns a repair prompt when invalid
8. large request body is rejected cleanly if over cap

Nice-to-have tests:
1. malformed JSON body returns `400`
2. validator internal failure returns structured `5xx`
3. endpoint can handle concurrent requests without reinitializing validator each time

## Operational Notes

Recommended protections:
- request size cap
- request timeout
- auth or rate limiting if exposed publicly
- structured logs of:
  - request id
  - caller
  - valid/invalid
  - has_json
  - error_count
  - warning_count
  - latency

Recommendation:
- avoid logging full workflow JSON unless debug mode is explicitly enabled

## Known Failure Modes Seen Locally

These are the exact kinds of failures the endpoint should surface well:
- no JSON returned at all
- JSON returned as a string or prose wrapper instead of workflow object
- expression format errors
- invalid `typeVersion`
- missing required properties
- invalid `responseNode` / `onError` schema combinations
- invalid field values like `workspace`

## Key Conclusion

The server-side validator endpoint is the right next move.

The main design tweak I recommend is:
- make the server return validation + repair guidance
- keep the retry loop in the plugin/eval harness first
- only move retry orchestration server-side later if there is a strong reason

That gives you:
- one shared validator
- easy reuse from evals and plugin
- lower coupling
- simpler rollback if anything goes sideways

## Useful Files In This Repo

- [scripts/eval/workflow_validation.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/workflow_validation.py:1)
- [scripts/eval/validator_feedback.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validator_feedback.py:1)
- [scripts/eval/validate-with-mcp.js](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validate-with-mcp.js:1)
- [scripts/eval/validate_workflow.py](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/validate_workflow.py:1)
- [scripts/eval/run-eval-v2.sh](/Users/danielbennett/codeNew/n8n-knowledge/scripts/eval/run-eval-v2.sh:182)
