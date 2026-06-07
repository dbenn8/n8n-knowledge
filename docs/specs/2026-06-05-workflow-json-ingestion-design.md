# n8n Workflow-JSON Ingestion — Design Spec (#85)

**Status:** DESIGN — pending Dan's approval of the metadata/label schema (§5).
**Date:** 2026-06-05
**Goal:** Let the n8n-knowledge plugin help the harness *design and build* custom n8n workflows by recalling **digestible, node-level building blocks** (real, in-the-wild node configs + how they wire together), not by regurgitating whole pre-vetted workflows.

**Architecture (one line):** Ingest each source workflow as **sibling node-level documents grouped by a `wf:<slug>` tag**, under a second, non-destructive retain strategy (`workflow_json`, `verbatim` mode). Recall returns small focused node/topology units; the full importable JSON is stored once and surfaced only on demand.

---

## 1. Why this shape (decisions that led here)

- **Build-vs-reuse pivot (2026-06-05).** `czlonkowski/n8n-mcp` (verified: 21.5k★, ~84k npm dl/wk, rebuilt per n8n release, offline `validate_workflow` + `get_template` returning importable JSON) already owns the **build/validate** lane. Rebuilding that = a shoddier, drifting wheel.
- **Our differentiated lane = design/judgment:** real configured node examples + how they wire + the gotchas/state layer (e.g. "use HTTP node not native Slack because #issue") — knowledge a schema can never hold. Node-level chunks serve this *better* than whole workflows and are *more* differentiated from the MCP, not less.
- **Deliverable is guideline knowledge, not a copy-pastable file.** The harness builds something *custom*; it composes building blocks. Injecting a whole 11–18 KB workflow to be "copy-pastable" is wasteful and usually partly irrelevant.

## 2. Proven by experiment (throwaway bank, 2026-06-05)

- `verbatim` extraction mode preserves content **byte-faithfully** (recalled JSON `== ` source) while the LLM only extracts entities/metadata (cheap: 5 workflows → 1,583 output tokens).
- Whole-workflow recall: **5/5 queries returned the right workflow #1**, including pure-intent matches.
- **Node-level recall:** focused ~500-char units, right node type on top for design queries; a per-workflow **topology** unit surfaces for wiring queries.
- **API constraint discovered:** Hindsight **rejects duplicate `document_id` within one batch** ("must be unique to avoid race conditions"). → A single document with many node-units is *not* achievable via the batch API. **Therefore: sibling node-documents grouped by tag** (functionally equivalent: node-level recall, gather-by-tag, no whole-workflow auto-injection).

## 3. Data model — sibling-docs-by-tag

Three document kinds per source workflow. **Each is its own Hindsight document** (unique `document_id`), bound together by the `wf:<slug>` tag.

| Kind | `document_id` | Content | In auto-recall? |
|---|---|---|---|
| **Node** (one per node) | `wf-<slug>-node-<nodeNameSlug>` | node config JSON + local wiring | ✅ yes (capped) |
| **Topology** (one per wf) | `wf-<slug>-topo` | node-type list + all connection edges | ✅ yes |
| **Source** (one per wf) | `wf-<slug>-source` | full importable workflow JSON (verbatim, stored once) | ❌ suppressed; on-demand only |

`document_id` keys on **node *name* slug** (n8n requires unique node names), not index — so cron diffs stay stable when nodes are reordered.

## 4. Retain strategy config (`workflow_json`)

Added **additively** to the production n8n bank's `retain_strategies` (per-item `strategy` overrides the default *for that item only* — default untouched, verified in source at `memory_engine.py:2909`). Each retain item passes `strategy: "workflow_json"`.

```jsonc
retain_strategies.workflow_json = {
  "retain_extraction_mode": "verbatim",   // preserve content exactly; LLM only tags entities
  "retain_chunk_size": 25000,             // chars; each node/topo/source = one unit
  "retain_mission": "<node-aware mission: extract node type, purpose, wiring (receives/sends via main|ai_tool|ai_languageModel|ai_memory|ai_retriever), and integrations/services used>"
  // NOTE: enable_observations canNOT be scoped per-strategy — see §9.1 (verified). Consolidation is bank-level.
}
```

## 5. ★ Metadata & Label (tag) schema — APPROVAL CENTERPIECE

**Tags = filterable, low-cardinality facets** (recall filtering + formatter routing). **Metadata = returned with the unit** (render + trace; not filtered).

### 5a. Tags

**Common to every workflow unit (node, topo, source):**
- `type:workflow-node` | `type:workflow-topology` | `type:workflow-source` — unit kind (formatter routes on this)
- `source:n8n-docs-workflows` — provenance / trust tier (future: `source:n8n-template`, `source:community-workflow`, `source:curated`)
- `wf:<slug>` — the **gather key** (groups all units of one workflow)

**Workflow-level facets — stamped on node + topo + source units so the workflow is discoverable by any of its units:**
- `trigger:<triggerType>` — e.g. `trigger:scheduleTrigger`, `trigger:chatTrigger`, `trigger:webhook`, `trigger:manualTrigger`, `trigger:executeWorkflowTrigger`
- `complexity:simple|medium|advanced` — by node count (simple ≤7, medium 8–20, advanced >20)
- `usecase:<tag>` (0..n) — derived/curated: `usecase:rag`, `usecase:chatbot`, `usecase:scraping`, `usecase:notification`, `usecase:etl`, `usecase:api-integration`
- `integration:<service>` (0..n) — derived from node types/creds: `integration:slack`, `integration:openai`, `integration:pinecone`, `integration:googledrive`, `integration:http`

**Node-unit only:**
- `node:<fullNodeType>` — e.g. `node:n8n-nodes-base.httpRequest`, `node:@n8n/n8n-nodes-langchain.agent` (enables exact "show me an X node")
- `nodeclass:trigger|ai|core|transform|integration` — coarse derived category

**Topology-unit only:**
- `conntype:<type>` (0..n) — each connection type present: `conntype:main`, `conntype:ai_tool`, `conntype:ai_languageModel`, `conntype:ai_memory`, `conntype:ai_retriever`, …

### 5b. Metadata

**Node unit:** `workflow` (display name), `wf_slug`, `node_name`, `node_type`, `url` (raw GitHub URL of source file), `source_document` (`wf-<slug>-source`), `n8n_version` (if known), `ingested_at` (ISO).
**Topology unit:** `workflow`, `wf_slug`, `url`, `source_document`, `node_count`, `ingested_at`.
**Source unit:** `workflow`, `wf_slug`, `url`, `node_count`, `node_types` (comma list), `trigger`, `complexity`, `ingested_at`. (The full importable JSON lives in **content**, stored once — never duplicated across units.)

## 6. Content shape per unit

- **Node:** `Node "<name>" (type <type>) in workflow "<wf>".` / `Receives from: <src (via type), …>` / `Sends to: <tgt (via type), …>` / `Config JSON:` / `<node object>`
- **Topology:** `Topology of "<wf>" (<N> nodes). Node types: <list>.` / `Connections:` / `<src> --<conntype>--> <tgt>` ×edges
- **Source:** `<wf> — full importable workflow JSON:` / `<raw JSON>`

## 7. Recall & formatter behavior (plugin `format_results.py`)

- Render `type:workflow-node` and `type:workflow-topology` in normal recall, **capped** (e.g. top 4–6 node units), grouped by `wf:<slug>`.
- **Suppress** `type:workflow-source` from auto/background recall. Surface it **only** on explicit "give me the importable JSON / full workflow" intent, or when gathering a specific `wf:<slug>`.
- Node render: name + type + wiring summary; config available inline; trace via `metadata.url` / `source_document`.

## 8. Sources & scope

- **Phase 1:** the **28 official `docs/_workflows/**.json`** (public, curated, free; `sync-docs.py` currently SKIPs `_workflows`).
- **Explicitly NOT** the full n8n.io/workflows library (~thousands) — that is czlonkowski/n8n-mcp's lane; re-ingesting = the shoddier wheel.
- **Optional later:** a hand-curated dozen canonical community patterns (annotated with gotchas) under `source:curated`.

## 8b. Node-spec corpus — the build-layer's spec source (reuse, don't build)

**Goal:** retain every node type's property spec so the harness can build any node with zero install. **Source decision:** do NOT write our own introspector — reuse czlonkowski/n8n-mcp's **MIT** extraction. The npm package ships a prebuilt SQLite at `package/data/nodes.db` (verified): 1,851 nodes with `node_type`, `display_name`, `description`, `category`, `is_trigger`/`is_ai_tool`/`is_webhook`, `is_versioned`/`version`, **`properties_schema`** (the build payload), `operations`, `credentials_required`, `outputs`. Plus `node_versions` (per-version schema + breaking changes + migration hints) and `template_node_configs` (real ranked example configs per node with `use_cases`/`complexity`).

**Two reuse modes:** (a) **consume the bundled `nodes.db`** (extract from the npm tarball — lightest, no build); (b) **run their `db:rebuild`** pinned to an n8n release (operationally independent). Either way we write zero introspection code. Freshness = cron-bump per release + Hindsight dedup. Their MIT `validate` logic is what the optional local-validate plugin script wraps.

**Licensing note (conscious call):** the *extractor* is MIT, but the *node definitions* originate from n8n's packages, which are **fair-code (Sustainable Use License), not MIT.** Retaining node schemas redistributes data derived from SUL-licensed packages. For our use — a KB that helps people *use* n8n — this aligns with n8n's intent, and czlonkowski does exactly this at 21.5k-star scale (precedent). Documented so it's deliberate.

**Strategy:** `node_spec` (own named strategy), `verbatim` extraction. Tags: `type:node-spec`, `source:n8n-node-introspection`, `node:<fullType>`, `nodeclass:`, `integration:`. Metadata: node_type, display_name, category, version, doc_url.

**CRITICAL sizing finding (prototype, 2026-06-05):** node `properties_schema` sizes vary enormously — httpRequest 39 KB, Set 12 KB, **Slack 172 KB.** Whole-node verbatim at `chunk_size` 25000 **splits big multi-resource nodes mid-JSON into incoherent fragments**, and recall's token cap means the full 172 KB never returns anyway. **This is the workflow lesson one level down: a big node is like a whole workflow; its operations are like nodes.** → **Chunk big nodes into per-resource/per-operation sub-units** (Slack `message`/`channel`/`file`…), each tagged `operation:<resource>.<op>`, using the `operations` column + `displayOptions` resource/operation conditionals as boundaries. A "send a Slack message" query then returns just the message-resource properties, not 172 KB. Small nodes (Set, NoOp) stay single units. Sibling-docs-by-tag again: `node:<type>` groups a big node's operation units. *(Threshold for splitting — e.g. >chunk_size — TBD in plan.)*

**Proven (prototype):** nodes.db rows → node_spec units → recall returned the correct node with build-usable property fields for httpRequest / Set / Slack queries. Reuse path validated end-to-end.

## 8c. Real-scale retrieval findings (CRITICAL — June 5, 2026)

Seeded 190 node-spec units (36 diverse nodes, per-op splits for big ones) into the **live n8n bank** (~360k units, 1,117 tags) with production tags (`type:node-spec`, `node:<type>`, etc.). Ran 12 build queries.

**Results ladder:**
| Approach | Hits | Why it fails/works |
|---|---|---|
| Plain semantic (whole bank) | 0/12 | node specs invisible under 268k community posts |
| Cardinality-filtered (broad `any`-match) | 2/12 | community topic-tags (`tag:slack`) collide with our node tags; IDF can't distinguish |
| Scoped to `type:node-spec` only | 4/12 | 190 per-op units are homogeneous; single-unit nodes (httpRequest, code) buried by the Slack/Gmail op-flood |
| **Scoped `type:node-spec` + `node:<type>` [+ `resource:`]** | **8/8** | ✅ |

**Conclusion: node-spec retrieval is a STRUCTURED LOOKUP, not semantic search.** The tags are the index. You must identify the node (and resource) and issue a scoped query; semantic ranking is only the final tiebreak *within* that scope. This means:
- **`node:<type>` and `resource:<r>` tags are LOAD-BEARING for retrieval**, not just metadata.
- Cardinality/IDF filtering is NOT the node-spec answer (community topic-tag collisions dominate).
- A **safety property**: node specs score ~0 on unscoped semantic recall, so they are invisible to normal plugin auto-recall (don't pollute), yet work 8/8 on structured fetch.

**Two-layer retrieval model:**
- **Design layer** (workflows, gotchas, docs) → **semantic** recall suggests *which node* to use.
- **Build layer** (node specs) → **structured** lookup: once the node is known, fetch `[type:node-spec, node:<type>, resource:<r>]` → property spec.

**NODE IDENTIFICATION — SOLVED (June 6, 2026).** The structured lookup needs `node:<type>`. Dan's hypothesis: n8n users name their services ("Slack" not "a messaging platform") because they already use those systems. **Confirmed empirically:** a dictionary lookup of 3,537 entries (display names + type suffixes + camelCase splits, derived from `nodes.db`) scores **13/13 on realistic community-style queries** ("sync airtable with hubspot," "send telegram message when form submitted," etc.). Node identification is a cached dictionary lookup, not a fuzzy NLP problem.

**Two minor fixes needed:** (a) min-length guard (4+ chars) to avoid false positives ("cal" in "call an API" → calTrigger), (b) action-vs-trigger disambiguation (prefer the action node unless the query contains trigger/listen/watch keywords — "slack" should match `nodes-base.slack` not `slackTrigger`).

**The complete auto-detect pipeline (every piece proven at real scale):**
1. Plugin loads node display-name dict at session start (from cached `nodes.db` or a derived JSON)
2. On each prompt: name-match → `node:<type>` [+ `resource:<r>` if the resource word also matches]
3. If match: **structured recall** `[type:node-spec, node:<type>]`, `tags_match=all` → property spec (8/8 at 360k)
4. In parallel: **unscoped semantic recall** → gotchas, workflows, docs (existing behavior, proven)
5. Merge + format: node spec + gotcha + example composition → injected context

## 9. Open items / verification (before production)

1. **Consolidation scoping — VERIFIED (2026-06-05) in hindsight-api source.** Per-strategy `enable_observations` does **NOT** work: both the post-retain auto-trigger (`memory_engine.py:2838`) and the consolidator (`consolidator.py:253`) call `resolve_full_config(bank_id)` — they re-resolve **bank** config and ignore the per-item strategy. Consolidation is **bank-level, all-or-nothing.** Control knob = **`observation_scopes`** (tag-based): the consolidator accepts scopes and only processes memories whose tags match. Since we WANT observations on for `outcome:`/`goal:` labels anyway, this is a design item, not a blocker: set `observation_scopes` on workflow/node MemoryItems and drive a **scoped consolidation** (cron `POST /consolidate` with the workflow scope) so workflow/node units synthesize cleanly among themselves without cross-contaminating docs/issues. Caveat: the post-retain auto-trigger fires **unscoped** — so to keep scoped consolidation clean, either disable bank-level auto-consolidation and rely on scoped cron passes, or accept mixed auto-consolidation. Decide before production.
2. **Formatter changes** (n8n-knowledge): new workflow-aware render path + intent-gating + capping + source-suppression. Separate TDD slice.
3. **Tag-derivation rules** for `usecase` / `integration` / `nodeclass`: a deterministic mapping table (node type/credential → service/class), optional LLM assist for `usecase`.
4. **Ingestion script:** new `sync-workflows.py` in `n8n-hindsight/scripts/`, nightly cron, **diff by `document_id`** (node-name-keyed), re-ingest changed workflows only.

## 10. Relationship to GitHub code retain (#86)

Same structural lesson applies to source code: **digestible sub-units (function/class) as sibling docs grouped by a `file:`/`module:` tag, each linking back to parent file + raw GitHub URL**, mirroring node→workflow. Likely a **separate `code_units` strategy** (different facets: language, function/class/method, visibility, call/import edges; possibly a different extraction mode — signature+docstring+purpose as the unit + URL for the body, vs workflow's verbatim). Tracked as task #86; do not fold into `workflow_json`.

## 11. Cleanup

Delete throwaway experiment banks `wf-strategy-test`, `wf-node-test` after spec lock.
