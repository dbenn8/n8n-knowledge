<!-- created: 2026-06-05T15:40:00 -->
# n8n-knowledge "Build Layer" — Technical Pipeline (Task #85)

**Created: 2026-06-05 15:40**

**Overarching goal:** make the n8n-knowledge plugin able to hand the harness (Claude) everything it needs to *build* an n8n workflow — node-by-node — as zero-friction injected context. Not "copy-paste a whole vetted workflow," but "here's how to configure each node, how nodes wire together, what real examples look like, and what bites you." Three knowledge types feed this: **node specs** (how to build any node), **workflow examples** (how nodes combine to reach a goal), **gotchas/state** (what's wise / what's broken).

This document explains the four technical pieces that produce and serve that context.

---

## 1. End-to-end information flow

**What this shows:** every source of truth, how each is transformed into Hindsight memory units, and how those units reach the harness at build time.

```mermaid
flowchart TB
    subgraph Sources["SOURCES (truth)"]
        NPM["npm node packages<br/>n8n-nodes-base, @n8n/nodes-langchain"]
        CODEX["GitHub codex files<br/>*.node.json (categories, doc URLs)"]
        WF["Example workflows<br/>docs/_workflows + curated"]
        CORPUS["Existing corpus<br/>docs, community, GitHub issues"]
    end

    subgraph Ingest["INGESTION / TRANSFORM (sync scripts, cron)"]
        INTRO["Node-spec introspection<br/>(Node script) -> resolved property spec per node"]
        SPLIT["Workflow splitter<br/>-> per-node units + topology + source"]
        ENRICH["Tag/label deriver<br/>node:, trigger:, integration:, usecase:, complexity:"]
    end

    subgraph Bank["HINDSIGHT n8n BANK (units)"]
        UNODE["node-spec units<br/>strategy: node_spec"]
        UWF["workflow node + topology units<br/>strategy: workflow_json"]
        USRC["workflow source units<br/>(full JSON, suppressed)"]
        UGOT["gotcha / state units<br/>(existing)"]
        OBS["consolidation -> observations<br/>outcome:/goal: labels"]
    end

    subgraph Serve["RECALL -> HARNESS (build time)"]
        HOOK["plugin recall<br/>(auto backstop + manual)"]
        FMT["formatter<br/>cap, route, suppress source"]
        LLM["Harness (Claude)<br/>composes the workflow"]
    end

    NPM --> INTRO --> UNODE
    CODEX --> ENRICH
    WF --> SPLIT --> UWF
    SPLIT --> USRC
    CORPUS --> UGOT
    ENRICH --> UWF
    ENRICH --> UNODE
    UWF --> OBS

    UNODE --> HOOK
    UWF --> HOOK
    UGOT --> HOOK
    OBS --> HOOK
    USRC -. on-demand only .-> HOOK
    HOOK --> FMT --> LLM
```

**Key insight:** four different sources, but a single destination — small, queryable units in the n8n bank that the formatter assembles into just-in-time build context. The full workflow JSON (`USRC`) is stored but deliberately kept *out* of normal recall (dotted line) until explicitly asked for.

**Why it matters:** this is what lets the plugin answer "help me build X" with zero install — the harness pulls node specs + real examples + gotchas frictionlessly, instead of the user standing up an MCP.

---

## 2. The node-spec step — why introspection, not file parsing

**What this shows:** why we cannot just `gh`-fetch node source from GitHub to get property specs, and what we do instead.

```mermaid
flowchart TB
    Q{"Need: resolved property spec for a node type"}

    subgraph GH["Path A: parse GitHub .node.ts files"]
        SIMPLE["Simple node (NoOp)<br/>static literal description"]
        VER["Popular node (HttpRequest)<br/>VersionedNodeType wrapper"]
        IMP["imports V1 / V2 / V3<br/>properties composed across files"]
        FAIL["Cannot resolve from files alone<br/>= reimplement n8n's loader (fragile)"]
    end

    subgraph NPMP["Path B: npm-package introspection (CHOSEN)"]
        INSTALL["npm i n8n-nodes-base @n8n/nodes-langchain<br/>(version pinned)"]
        INST["instantiate each node class"]
        DESC["read resolved .description<br/>= full, version-aware property spec"]
    end

    Q --> SIMPLE
    Q --> VER --> IMP --> FAIL
    Q --> INSTALL --> INST --> DESC
    SIMPLE -. only the easy 10% .-> DESC
    CODEX2["GitHub *.node.json codex<br/>(static metadata)"] --> TAGS["enrich tags:<br/>category, doc URL, aliases"]
    DESC --> UNIT["one node-spec unit per node type<br/>(verbatim) + tags"]
    TAGS --> UNIT
```

**Key insight:** a node's *resolved* spec only exists after the code runs. Simple nodes are static literals, but the nodes people actually use (HTTP Request, Set, Slack, Google*) are `VersionedNodeType` wrappers that compose properties across imported V1/V2/V3 files — verified against n8n source. So we **execute** the published packages and read each node's `.description`. GitHub is still used, but only for the *static* codex metadata that enriches tags.

**Why it matters:** introspection makes the corpus authoritative (the same resolution n8n itself does), reproducible (version-pinned), instance-independent, and owned end-to-end by us — not dependent on anyone else's extracted database.

---

## 3. The data model — sibling-docs-by-tag

**What this shows:** how one example workflow becomes many small documents bound by a tag, and exactly what recall returns vs. what stays out of context. (Driven by a hard API rule: Hindsight rejects duplicate `document_id` in a batch, so each unit must be its own document.)

```mermaid
flowchart TB
    SRCWF["Source workflow:<br/>'Let your AI call an API' (slug = let_ai_call_api)"]

    subgraph Docs["Sibling documents, bound by tag wf:let_ai_call_api"]
        N1["wf-...-node-agent<br/>node config + wiring"]
        N2["wf-...-node-httpTool<br/>node config + wiring"]
        N3["wf-...-node-chatModel<br/>node config + wiring"]
        TOPO["wf-...-topo<br/>connections / wiring grammar"]
        SRC["wf-...-source<br/>FULL importable JSON"]
    end

    SRCWF --> N1 & N2 & N3 & TOPO & SRC

    subgraph Recall["What a design/build query gets"]
        RET["node units + topology<br/>(capped, focused ~500 chars each)"]
    end

    N1 & N2 & N3 & TOPO --> RET
    SRC -. only on explicit 'give me the JSON' intent .-> RET

    NODESPEC["node-spec units (parallel corpus)<br/>node_spec strategy, grouped by node:type"] --> RET
    RET --> H["Harness build context"]
```

**Key insight:** the workflow *is* the group of node units (joined by the `wf:<slug>` tag), not one big blob. Recall returns the small, relevant node + topology units; the full JSON (`wf-...-source`) sits in the bank but is **suppressed** from normal recall and surfaced only on explicit intent — so the harness gets focused building blocks, never an 18 KB workflow dumped into context uninvited.

**Why it matters:** this is what makes "guideline, not regurgitation" real and token-cheap — and `wf:<slug>` still lets us *gather* the whole thing on demand.

---

## 4. The design → build → validate journey (who plays where)

**What this shows:** the user's path from intent to a working workflow, and which system carries each leg — clarifying that the plugin owns design+build context while validation/deploy is free or optional.

```mermaid
flowchart LR
    U["User: 'build me a flow that does X'"]

    subgraph DESIGN["DESIGN  (plugin's core)"]
        PAT["proven patterns + outcome labels<br/>'a flow that achieves X looks like...'"]
        GOT["gotchas / state<br/>'use HTTP node not native Slack (#issue)'"]
    end

    subgraph BUILD["BUILD  (plugin's new build layer)"]
        SPEC["node specs (introspected)<br/>exact properties to set"]
        EX["real node examples + topology<br/>how they wire"]
        COMP["Harness composes workflow JSON"]
    end

    subgraph VALIDATE["VALIDATE  (free or optional)"]
        IMP["import to n8n -> red-node check (free)"]
        OPT["optional plugin script<br/>vs user's own /types/nodes.json"]
        MCP["czlonkowski MCP / n8n MCP<br/>(if user already runs it)"]
    end

    U --> PAT --> GOT --> SPEC --> EX --> COMP
    COMP --> IMP
    COMP -. optional .-> OPT
    COMP -. optional .-> MCP
    IMP --> DONE["Working workflow"]
```

**Key insight:** the plugin carries the two legs that are actually hard and high-friction today — **design** and **build** context, injected with zero setup. Validation, the MCP's headline feature, is the *cheapest* leg: it's free the moment you import to n8n (red nodes), or an optional script pointed at the user's own instance. Nobody's core pain is "I lack a validator"; the felt pain is setup friction — which the zero-install plugin removes.

**Why it matters:** it locates our durable value (frictionless design+build knowledge, plus the experiential/contributed layer a schema mirror can never hold) and treats validate/deploy as commodity legs we don't need to own.

---

## Confidence & risk (light scorecard)

| Piece | Confidence | Risk | Note |
|---|---|---|---|
| Node-spec introspection | ✓✓✓ HIGH | 2 LOW | n8n's own resolution; proven approach (czlonkowski does same) |
| Sibling-docs-by-tag | ✓✓✓ HIGH | 2 LOW | Empirically proven; forced by duplicate-document_id rule |
| Verbatim node/topology units | ✓✓✓ HIGH | 2 LOW | Byte-faithful recall confirmed in experiment |
| `outcome:` consolidation labels | ✓✓○ MED | 6 HIGH | Needs per-strategy `enable_observations` scoping verified |
| Formatter intent-gating (suppress source, cap nodes) | ✓✓○ MED | 4 MOD | New plugin logic; TDD slice; failure = token bloat, not correctness |
| Full node-spec corpus scope | ✓✓○ MED | 4 MOD | Large but relevance-ranked; long tail surfaces only when relevant |

**Top open item:** verify async consolidation honors per-item `strategy` so `enable_observations:false` (and later, `outcome:` labels) scope correctly — before production on the live n8n bank.
