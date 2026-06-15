# n8n-knowledge — Version-Aware Bug Surfacing Architecture

**Status:** Design (Branch B, validated). Plan: `docs/superpowers/plans/2026-06-13-version-aware-bug-tagging.md`
**Date:** 2026-06-15
**One-line goal:** Surface the *right* node's known bugs to the model by tagging the whole GitHub corpus with `node:X` at ingest, then letting **engagement ranking** + **two-layer progressive disclosure** pick what the model sees — no hand-curated bug lists (no "teaching to the test").

---

## 0. TL;DR — the mental model

There are **two completely separate pipelines**. Confusing them is what makes this feel tangled:

| | **INGEST pipeline** | **RECALL pipeline** |
|---|---|---|
| Runs | Server-side, nightly (n8n-hindsight) | On the user's machine, per prompt (plugin hook) |
| Frequency | Once per issue (dedup) | Every prompt |
| Server load | Batch, one-time | **Zero** extra load (local dict + 1-3 recall calls) |
| Job | Tag issues `node:X`, bake engagement/state/version into the bank | Detect nodes in the prompt, fetch the relevant bugs, rank, inject |

"Progressive disclosure" lives entirely in the **recall** pipeline and has **two layers** (§4).

---

## 1. System Context — where this lives

> **What this shows:** the feature's boundaries and who talks to whom.
> **Key insight:** the plugin only ever calls one cheap public endpoint; all the heavy tagging happens server-side at ingest.

```mermaid
graph TB
    subgraph dev["Developer machine"]
        User[Developer in Claude Code]
        Hook[auto-recall hook<br/>+ node_lookup.py local dict]
    end
    subgraph cloud["n8n-hindsight server"]
        Sync[sync-github.py<br/>nightly ingest]
        ValDb[(validator / n8n-mcp<br/>node database)]
        Bank[(n8n Hindsight bank<br/>430K memories, TEMPR)]
        Recall[/public/recall<br/>no-auth endpoint/]
    end
    subgraph ext["External"]
        GH[GitHub API<br/>issues + PRs + releases]
    end

    GH -->|fetch newest 4500 issues<br/>+ 1500 PRs| Sync
    Sync -->|node-detect title+body| ValDb
    Sync -->|retain with node:X +<br/>engagement + state tags| Bank
    User --> Hook
    Hook -->|node-filtered query| Recall
    Recall --> Bank
    Bank -->|ranked results| Recall
    Recall -->|bugs + specs| Hook
    Hook -->|injected context| User
```

---

## 2. INGEST pipeline — how issues get `node:X` tags

> **What this shows:** the one-time/nightly server-side job that makes bugs findable by node.
> **Key insight:** node detection here is a *separate* server-side detector from the plugin's local dict. It reads the validator/n8n-mcp node DB on the server — users never need a local `nodes.db`.

```mermaid
flowchart LR
    A[GitHub API] -->|newest ~4500 issues<br/>+ ~1500 PRs| B[Filter by freshness cap]
    B --> C{Engagement<br/>floor?<br/>OPEN}
    C -->|above floor| D[Node-detect on title + body<br/>via validator/n8n-mcp DB]
    C -->|below floor| X[Skip / minimal tag]
    D --> E[Attach tags:<br/>node:X · source:github-issues<br/>state · reactions · comments]
    E --> F[(Retain to n8n bank<br/>dedup by document_id)]
    F --> G[Consolidation distills<br/>+ preserves tags]
```

**Release notes** are already done — no re-retain needed. They consolidated into atomic facts like *"Bug fix: … (issue #14259)"*, each carrying `version:X.Y.Z` as a tag + metadata. That's the linking key for version-awareness (§6).

---

## 3. RECALL pipeline — what happens on every prompt

> **What this shows:** the per-prompt flow on the user's machine.
> **Key insight:** the local dict (misspelling-tolerant node detection) runs first and locally — zero server load. Then progressive disclosure decides how many server calls to make.

```mermaid
sequenceDiagram
    participant U as User prompt
    participant H as auto-recall hook
    participant L as node_lookup.py<br/>(LOCAL dict)
    participant R as /public/recall
    participant M as Model (Claude)

    U->>H: "build a workflow that uploads a file to OpenAI"
    H->>L: identify_nodes(prompt)
    L-->>H: [openai] (fuzzy: handles "openai"/"opena i"/typos)
    Note over H,R: LAYER 1 — Tier 1 (always)
    H->>R: gotcha recall — tags:[node:openai] all_strict<br/>query: "openai file upload binary multipart bug"
    R-->>H: top bugs ranked by relevance + engagement
    H->>R: node-spec recall (parallel)
    R-->>H: node schema
    alt Tier 1 returned >= 2 results
        Note over H: Tier 2 SKIPPED (saves a call)
    else Tier 1 thin
        H->>R: Tier 2 — semantic/community recall
        R-->>H: fallback context
    end
    H->>M: inject: schema + KNOWN BUGS (top ~5) + workarounds
    M-->>U: workflow that uses HTTP Request (avoids the bug)
```

---

## 4. The two-layer progressive disclosure (the part that's easy to lose)

> **What this shows:** the two independent filters that keep the model from drowning in a dense node's 40 bugs.
> **Key insight:** Layer 1 decides *which source types*; Layer 2 decides *which bugs of the chosen node*. Together they mean the model sees ~5 bugs that are all about the exact thing it's building — not 5 random ones.

```mermaid
flowchart TD
    P[User prompt + detected node] --> L1

    subgraph L1["LAYER 1 — source-type tiering (Task 6)"]
        T1[Tier 1: node-filtered bugs<br/>+ node specs · ALWAYS]
        Q{Tier 1<br/>>= 2 results?}
        T2[Tier 2: community / semantic<br/>CONDITIONAL]
        T1 --> Q
        Q -->|yes| SKIP[skip Tier 2 — cheaper, less noise]
        Q -->|no| T2
    end

    L1 --> L2

    subgraph L2["LAYER 2 — within-node relevance (Task 5)"]
        CQ[gotcha query = node tag<br/>+ USER TASK KEYWORDS]
        TR[TEMPR ranks by relevance<br/>to the task]
        EN[engagement re-rank<br/>reactions + comments*4]
        CAP[take top ~5]
        CQ --> TR --> EN --> CAP
    end

    L2 --> OUT[~5 task-focused, high-engagement bugs injected]

    style L1 fill:#1f3a5f,color:#fff
    style L2 fill:#3f2b5b,color:#fff
```

**Why this dissolves the "n=5" worry.** The old mental-model plan picked "5 gotchas minimum" because *"the model can't synthesize 5+ **scattered** results."* With Layer 2, the 5 aren't scattered — they're all about *file upload to OpenAI*, not 5 random picks from OpenAI's 40 bugs. Five focused warnings are trivially digestible, so we don't need a synthesis/curation layer.

**Status check (important):** Layer 1 (tiering) is **designed, not yet built** — current code fires everything in parallel. Layer 2's task-aware query is **the missing piece** — today's gotcha query is node-name-only (`"openai node bug issue workaround error"`), which can't tell a file-upload task from a credential task. Making it task-aware is the key build step.

---

## 5. Engagement ranking — the *only* selection criterion (no curation)

> **What this shows:** how "which bugs matter" is decided without a hand-picked list.
> **Key insight:** known gotchas surface because they're genuinely high-engagement — generalizable, and it can't be gamed to pass the eval.

```mermaid
flowchart LR
    A[Node-tagged bugs for this node] --> B[engagement = reactions + comments × 4]
    B --> C{tier}
    C -->|>= 10| H[HIGH confidence]
    C -->|>= 3| M[MEDIUM]
    C -->|< 3| Lo[LOW · capped]
    H --> Z[rank + inject]
    M --> Z
    Lo --> Z
```

- `high_engagement_threshold: 10`, `medium_engagement_threshold: 3` (`plugin_config.py`). These are **display tiers**.
- **OPEN decision:** a separate *re-retain inclusion floor* (which issues are even worth tagging). No value was ever set — the "5" you remembered was the per-node mental-model density threshold, a different axis.

---

## 6. Version awareness — "is this bug fixed for MY version?"

> **What this shows:** how a bug gets a `fixed-in:X.Y.Z` tag and how the plugin uses the user's version.
> **Key insight:** release notes already carry issue→version, so this is mostly cross-referencing. Coverage is partial (some fixes never cite an issue #).

```mermaid
flowchart TD
    RN[Release-note fact:<br/>'fix … issue #NNNNN' + version:X.Y.Z] --> X[Cross-ref issue # in bug metadata]
    BUG[Node-tagged bug, has issue #] --> X
    X -->|match| TAG[tag bug fixed-in:X.Y.Z]
    X -->|no changelog cite| NOFIX[leave as open / unknown]
    TAG --> CMP{user n8n version<br/>from validator}
    CMP -->|user >= fixed-in| SUPPRESS[demote — already fixed for them]
    CMP -->|user < fixed-in| PROMOTE[promote + show workaround]
    NOFIX --> SHOW[show with current state]
```

---

## 7. What's being REMOVED (and why)

```mermaid
graph LR
    subgraph removed["Removed — all 'curation' / teaching-to-the-test"]
        MM[Mental models]
        SS[section_selector.py<br/>hardcoded keyword table]
        CB[Curated known-bug memories<br/>5 deleted 2026-06-14]
        KT[type:known-bug curation tag]
    end
    subgraph kept["Kept / replaces them"]
        NL[node_lookup.py local dict<br/>UX, misspellings, zero load]
        TAG[node:X tags on real issues]
        ENG[engagement ranking]
        PD[two-layer progressive disclosure]
    end
    MM -.replaced by.-> TAG
    SS -.replaced by.-> PD
    CB -.replaced by.-> ENG
    KT -.replaced by.-> ENG
```

`node_lookup.py` (the local fuzzy dict) is **NOT** removed — it's the prompt-side detector, runs locally, essential for UX.

---

## 8. Build status & open decisions

| Piece | State |
|---|---|
| Remove curated test memories | ✅ done (5 deleted, verified) |
| Release-note issue→version facts | ✅ already in bank |
| retain/observations missions | ✅ already n8n-tuned |
| `node:X` tagging in `sync-github.py` | ⬜ to build (server-side, validator DB) |
| `all_strict` on node recall | ⬜ to build (else untagged noise leaks) |
| Layer 1 tiering (Tier 2 conditional) | ⬜ designed, not built |
| Layer 2 task-aware gotcha query | ⬜ **missing piece** for dense nodes |
| `fixed-in` cross-ref | ⬜ best-effort, partial |
| Drop mental models + section_selector | ⬜ to build |

**Open decisions for Dan:**
1. **Engagement re-retain floor** — which issues are worth tagging at all? (≥3 reuse medium tier, ≥5, or tag-all.) No prior value exists.
2. **Auto-synthesis fallback for dense nodes** — likely unnecessary if Layer 2 is built; recommend *measure first*.

---

## Validation summary

```
DESIGN HEALTH: 88%

CONFIDENCE
  HIGH  : node:X tagging, engagement ranking, release-note version facts,
          local-dict separation (all proven / already in bank)
  MEDIUM: Layer 1 tiering (designed not built), all_strict at scale
          (verified semantically, not yet load-tested)
  LOW   : Layer 2 task-aware query quality on dense nodes (the one to prove)

TOP RISKS
  1. [6 HIGH] Layer 2 not built → dense nodes (OpenAI ~40 bugs) inject a
     generic 5, model may still pick the broken path.
     → Build task-aware gotcha query; measure dense-node eval before any fallback.
  2. [4 MOD] all_strict unverified at 430K scale (couldn't test — curated data
     was deleted). → Re-test in Task 2 with a real node-tagged sample.
  3. [4 MOD] fixed-in coverage partial (fixes that cite no issue #).
     → Accept; log coverage %. Matches existing task #83.
  4. [3 MOD] Engagement floor undecided → either over-ingest noise or drop real
     low-engagement bugs. → Dan picks the floor.

NO CRITICAL (9-12) RISKS. Ready for review.
```
