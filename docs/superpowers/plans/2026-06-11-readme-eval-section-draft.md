# README eval-section draft (ON HOLD per Dan, 2026-06-11)

New eval runs from another session must be factored in before this ships.
Drop in the new numbers and re-review with Dan.

---

## Eval results (honest comparison)

Benchmarked against the community **n8n-mcp** server on a 128-prompt
workflow-generation benchmark. The validated-workflow metric is "does the
generated workflow pass n8n-mcp's full validation engine."

**Methodology note (June 11, 2026):** earlier published numbers came from a
harness that we later found had two validity bugs — user-scope plugins leaked
context into the comparison conditions, and the plugin's own recall hook
failed silently under load. Both were found, fixed, and verified with
transcript audits; the table below is from post-fix runs (DeepSeek Pro,
no timeout, single run per prompt — treat ±1 run as noise).

| Run (clean harness)        | Plugin (validated) | n8n-mcp | Plugin real cost vs MCP |
|----------------------------|--------------------|---------|-------------------------|
| Group C (40 complex builds)| 80.0%              | 82.5%   | −31%                    |
| Groups A+B (88 prompts)*   | 78.4%              | 75.0%   | −33%                    |

\* A+B ran before the final harness fixes landed; its validity numbers carry
that caveat and will be refreshed.

**Read this honestly:** on raw validation pass rate the plugin and the MCP
server are **statistically tied** (±1 run). The plugin is not a
validation-quality silver bullet. Where it differs:

- **Cost / tokens:** ~31–33% cheaper end-to-end, ~65% fewer input tokens —
  context is injected instead of fetched through a tool-call loop.
- **Turns:** roughly half the tool round-trips.
- **Gotcha awareness:** with multi-node gotcha recall (v0.3.8), injected
  known-bug context measurably changes designs — e.g. avoiding the Merge
  node's positional-combine row-loss mode. The MCP condition earns its gotcha
  coverage differently (live docs fetching), at higher token cost.

An earlier, differently-scored run is committed at
[`docs/eval-findings-run1.md`](docs/eval-findings-run1.md) — the numbers there are older and
not directly comparable to the v2 figures above.
