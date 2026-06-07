# Retrieval Eval Harness — Design Plan (#74)

**Status:** DESIGN — plan only. This document specifies a *measurement tool*, not the build-layer ingestion (that is #85). No code is written here.
**Date:** 2026-06-05
**Sibling docs:** `docs/specs/2026-06-05-workflow-json-ingestion-design.md` (build layer), `docs/diagrams/2026-06-05-build-layer-pipeline.md` (pipeline).

---

## Goal

Turn "does our retrieval work?" from an anecdote ("on a 44-unit toy bank, tag-filtered recall returned the right Slack op") into a **reproducible, versioned metric** computed against the real ~360k-unit `n8n` bank, and — most importantly — **detect overfitting**: prove that any retrieval heuristic we adopt (cardinality-based tag filtering, namespace selection, stemming) generalizes to node families and intents it was never tuned on.

### Why this is needed

We have two empirical findings and one structural risk:

1. **Pure semantic recall does not discriminate siblings.** Among many near-identical units (44 Slack operations, all sharing the noun "Slack message channel"), the shared resource nouns dominate ranking; the *correct operation* unit is not reliably #1.
2. **Tag-filtered recall works.** Filtering to the correct `node:`/`operation:` tag, *then* ranking, returns the correct unit at #1.
3. **The risk:** findings (1) and (2) come from tiny hand-built banks (44 units, one node family). The production `n8n` bank has **~360k units across 1,117 tags**, with tag cardinalities spanning **2 → 268,049** (e.g. `source:discourse` ≈ 268k). A rule that filters "on the resource tag" is meaningless at scale: some tags narrow 360k→3, others narrow 360k→260k (useless). **Our heuristics risk overfitting to the toy subset.** We need a harness that measures on data the heuristics were *not* tuned on.

### The candidate heuristic under test (the thing we are measuring)

A **generic, cardinality/IDF-based filtering rule** that replaces the hand-coded "filter on resource":

> Pull tag counts from `GET …/tags`. Compute each tag's selectivity (IDF ∝ `log(N / count)`). **Never filter on high-cardinality tags** (they don't narrow the candidate set). **Filter or boost on low-cardinality (selective) tags** that match the query's extracted node/operation/integration facets.

The harness's job is to decide, with numbers, whether this generic rule matches or beats the hand-tuned rule **on held-out node families**, and to pick its one free parameter (the cardinality/IDF threshold) honestly.

### Success criteria

- A single command produces a report comparing **≥3 strategies** (`no-filter` baseline, `cardinality-filtered`, `hand-tuned`) on the same ground-truth set, with precision@k / recall@k / MRR / partial-credit.
- The report distinguishes **TRAIN** metrics from **HOLDOUT** metrics. A strategy is only "adopted" if it clears the pass bar **on HOLDOUT**.
- Re-runnable: same pairs + same bank snapshot → same numbers (modulo bank drift, which is logged).
- The cardinality threshold is **swept on TRAIN and frozen before HOLDOUT is touched** (§6).

---

## Architecture (one line)

A dependency-free Python script loads a versioned ground-truth file of `(query → expected unit ids)` pairs, calls the Hindsight recall API once per pair **per strategy**, scores the returned unit ids/tags against the expected set, and emits a side-by-side strategy report split by TRAIN vs HOLDOUT.

```
ground_truth.jsonl ──► harness.py ──► (per pair, per strategy: POST /recall) ──► score ──► report.md + report.json
        ▲                  │
   tags_cache.json ◄───────┘  (GET /tags, cached: drives the cardinality strategy)
```

---

## 1. Goal & success criteria

Covered above. Restated as concrete bars:

- **Primary bar (adoption gate):** on the HOLDOUT split, the chosen strategy must achieve **MRR ≥ 0.70** and **recall@5 ≥ 0.90** for node-spec queries, and must **not regress** the `no-filter` baseline on any query family by more than 0.05 MRR.
- **Overfitting bar:** `|MRR_train − MRR_holdout| ≤ 0.10` for the adopted strategy. A larger gap is flagged as "overfit — do not ship the tuned threshold; fall back to a conservative default."
- **Partial-credit bar (diagnostic, not a gate):** "right-resource-but-wrong-operation" rate (§4) is reported so we can see *how* a strategy fails (close vs. wild miss).

---

## 2. Ground-truth dataset construction

A ground-truth pair is:

```jsonc
{
  "id": "ns-slack-postMessage-001",
  "query": "send a message to a slack channel",     // natural build/design/question intent
  "expected_ids": ["node-spec-n8n-nodes-base.slack-message.post"], // 1..n acceptable unit document_ids
  "expected_tags": {                                  // tag-level ground truth for partial credit
    "node": "n8n-nodes-base.slack",
    "operation": "message.post",
    "resource": "message"
  },
  "kind": "node_spec",            // node_spec | workflow | gotcha
  "family": "slack",              // split key (§3) — node family / integration
  "intent": "send",               // coarse intent bucket
  "source": "curated",            // curated | mined-accepted-answer | mined-codex
  "split": "train"                // train | holdout  (assigned by §3, stored in file)
}
```

Stored as **`docs/eval/ground_truth.jsonl`** (one JSON object per line; git-tracked; the canonical dataset). The split assignment lives *in the file* so runs are reproducible and the holdout boundary is auditable.

### Three sources of ground truth

**(a) Node-spec pairs — node/operation intent → known `node_spec` unit.**
Semi-automatic. The `node_spec` corpus (#85 §8b) is generated from the introspected `nodes.db`-equivalent: every unit already carries `node:<fullType>` and (for split big nodes) `operation:<resource>.<op>` tags and a deterministic `document_id`. So we can *generate* candidate pairs programmatically:
- For each node type: a template query from `display_name` + intent verb (e.g. "create a row in postgres", "make an http GET request", "upload a file to google drive"). Verb templates per `nodeclass` (trigger → "trigger when…", integration ops → resource/operation verbs read from the `operations` column).
- `expected_ids` = the node's spec unit(s); for split nodes, the specific `operation:` sub-unit.
- A human reviews the generated query for naturalness and fixes obviously wrong verbs. Target: generate ~400 candidates, hand-keep the best ~120 across diverse families.

**(b) Workflow pairs — "build X" → known example workflow.**
Hand-curated from the 28 official `docs/_workflows/**.json` (#85 §8). For each workflow: 1–2 intent queries ("build a flow that lets an AI call an API", "scrape a page on a schedule and notify slack"). `expected_ids` = that workflow's node/topology units (`wf:<slug>` group); a hit = any in-group unit ranks in top-k, or the topology unit specifically for wiring-intent queries. ~30–40 pairs.

**(c) Gotcha pairs — question → known accepted-answer unit. (Exploit `has_accepted_answer`.)**
Semi-automatic and high-leverage. The `n8n` bank's community posts carry **`has_accepted_answer` metadata**. Mine pairs:
- Query `GET …/tags` / list memories filtered to `source:discourse` (or `source:community`) units **where metadata `has_accepted_answer == true`**.
- For a sampled subset, the **question title/body becomes the query**, and the **accepted-answer unit's `document_id` becomes the (high-confidence) expected id**. This gives ground truth *for free* at real scale and real phrasing — exactly the distribution we serve.
- Sample for breadth across topics (expressions, webhooks, credentials, docker, error handling). Target ~60–80 mined pairs, with a 10% hand-audit to discard noisy ones (accepted ≠ relevant).

### Target size & breadth

- **Total target: 200–300 pairs** (v2). v1 ships a 40–60 pair hand set (§7).
- **Breadth dimensions** (track counts per cell, aim for coverage not uniformity): node *family* (slack, google\*, postgres, http, langchain/AI, core/transform), *resource/operation* depth (single-op vs. 44-op nodes), *intent* (build / configure / wire / debug / "why is X broken"), *source* (curated / mined-accepted-answer / docs / codex).
- **Why breadth matters:** overfitting hides in under-represented cells. The split (§3) deliberately holds out *whole* families so the harness can't memorize family-specific quirks.

### Generation vs. curation rule

- **Generate** the scaffold (queries from node metadata, accepted-answer mining) to get scale cheaply.
- **Hand-curate** acceptance: every pair is human-confirmed (the query is natural; the expected id is genuinely the best unit). Generated-but-unreviewed pairs are marked `"reviewed": false` and **excluded from the adoption-gate metrics** (they may be reported separately as a smoke signal).

---

## 3. TRAIN / HOLDOUT split — the overfitting detector

**The split is by *node family / integration*, not random per-pair.** This is the core anti-overfitting mechanism: a per-pair random split would leak family-specific signal (if 40 Slack pairs are split 30/10, the threshold can tune to "Slack" and still score well on the 10 held-out Slack pairs). Holding out **entire families** forces generalization.

### Split strategy

- **HOLDOUT families (the heuristic-tuning code never sees these):** reserve a set of *whole* node families / integrations and *whole* intent buckets — e.g. **all `google*` nodes, all `postgres` nodes, all `langchain`/AI-agent nodes, and the "debug/why-broken" intent bucket**. Every pair whose `family` ∈ holdout-set is `split: "holdout"`.
- **TRAIN families:** everything else (slack, http, core/transform, notification workflows, expression gotchas, …) is `split: "train"`.
- **Frozen boundary:** the holdout family list is written to **`docs/eval/holdout_families.txt`** and committed *before* any threshold sweeping. Adding a family to holdout later requires a new dataset version (bump a `DATASET_VERSION` constant) so we can't quietly move the goalposts.
- **Workflow & gotcha pairs** follow the same rule: hold out by usecase family (e.g. all `usecase:rag` workflows held out) and by topic family for gotchas (e.g. all "credentials" gotchas held out).

### What runs where

- **Tuning (§6) reads TRAIN pairs only.** The threshold sweep, namespace-inclusion choices, and stemming toggle are all selected to maximize **TRAIN MRR**.
- **Final adoption metrics are computed on HOLDOUT only.** The report shows both, but the gate (§1) and the train↔holdout gap check are what decide adoption.
- The harness **refuses to read HOLDOUT during a `tune` run** (separate subcommands, §5) so the boundary can't be accidentally crossed.

---

## 4. Metrics

For each pair, the strategy returns a ranked list of unit ids `r_1..r_k`. Let `E` = `expected_ids`.

- **precision@k** = `|{r_1..r_k} ∩ E| / k`. (For single-answer pairs, this rewards not padding the top-k with junk.)
- **recall@k** = `|{r_1..r_k} ∩ E| / |E|`. (Did we surface *an* acceptable unit within k?)
- **MRR** = mean over pairs of `1 / rank_of_first_hit` (0 if no expected id in top-k). Primary headline metric — sensitive to *position*, which is exactly the sibling-discrimination problem.
- **Hit@1** = fraction with an expected id at rank 1 (reported alongside MRR for legibility).
- **Partial-credit / "right-resource-wrong-operation" signal:** using `expected_tags`, classify each top-1 result as:
  - `exact` — returned id ∈ E;
  - `right-node-wrong-op` — top-1 unit's `node:` tag matches `expected_tags.node` but `operation:` differs;
  - `right-resource-wrong-op` — `resource` matches, `operation` differs;
  - `wrong` — neither matches.
  Report the distribution. This is the **diagnostic** that tells us whether semantic recall is "close" (right node, wrong op → a tag filter will fix it) or "lost" (wrong node → a deeper problem). It is the quantitative version of the original Slack finding.

### 4b. Node-identification metric (front-door to structured lookup)

The `structured_lookup` strategy requires knowing `node:<type>` before it can query. **Node identification = can the system extract the correct `node:<type>` from the prompt?** This is measured as a separate step before recall scoring:

- **Node-ID accuracy:** for each node-spec pair, run the facet extractor on the query and check whether the correct `node:<type>` tag was extracted. Report: `node_id_hit` (correct node extracted), `node_id_close` (correct integration extracted but wrong node type), `node_id_miss` (nothing relevant).
- **Node-ID is the front-door gate:** if node-ID fails, `structured_lookup` can't even fire. So the *combined* metric is `node_id_hit_rate × structured_lookup_MRR_given_hit`.
- **2026-06-05 finding:** Dan's hypothesis is that this is *not* a real problem in practice — n8n users know which systems they're connecting ("Slack" not "a messaging platform"), so the facet extractor's name-match against `display_name`/`aliases` should have high coverage. This can be validated: report the distribution of `node_id_miss` queries and categorize them as "vague intent" vs "named but not matched" — the latter is a codex-gap bug, the former is a design-layer referral.

**k values:** report **k ∈ {1, 3, 5, 10}**. Headline = MRR (uncapped within k=10) + recall@5.

**Pass bar:** as in §1 — HOLDOUT MRR ≥ 0.70, recall@5 ≥ 0.90, no >0.05 MRR regression vs baseline on any family, train↔holdout MRR gap ≤ 0.10.

---

## 5. Harness mechanics

### File layout (new)

```
docs/eval/
  ground_truth.jsonl        # the dataset (§2), split assigned per-pair
  holdout_families.txt      # frozen holdout family list (§3)
  tags_cache.json           # cached GET /tags snapshot (§ cardinality strategy)
scripts/eval/
  harness.py                # the runnable tool (stdlib only: urllib, json, argparse, statistics)
  strategies.py             # strategy implementations (no-filter / cardinality / hand-tuned)
  mine_accepted_answers.py  # builds gotcha pairs from has_accepted_answer (§2c)
  gen_nodespec_pairs.py     # builds node-spec candidate pairs from the spec corpus (§2a)
out/eval/
  report-<DATASET_VERSION>-<timestamp>.md
  report-<DATASET_VERSION>-<timestamp>.json   # machine-readable, for diffing runs
```

### Config / connection

- **Bank:** `n8n` on the dedicated instance. **URL `N8N_HINDSIGHT_URL`** and **Bearer key `N8N_HINDSIGHT_API_KEY`** read from `portfolio/.env` (loaded by a tiny dotenv parse — no dependency). A `--bank` flag allows pointing at a throwaway bank for harness self-tests.
- **Recall endpoint:** authenticated `POST {N8N_HINDSIGHT_URL}/v1/default/banks/{bank}/recall` with `Authorization: Bearer …`. (The plugin's unauthenticated `/public/recall` is the same engine but without tag filters; the harness must use the authenticated form to pass `tags` + `tags_match`.) Body:
  ```jsonc
  {
    "query": "<pair.query>",
    "budget": "low",
    "max_tokens": 3000,
    "tags": ["node:n8n-nodes-base.slack"],   // injected by the strategy (empty for no-filter)
    "tags_match": "any",                       // any | all — strategy-controlled
    "include": {"source_facts": {}}
  }
  ```
- **Tags endpoint:** `GET {N8N_HINDSIGHT_URL}/v1/default/banks/{bank}/tags` → `{tag: count}` map, cached to `tags_cache.json` (refresh with `--refresh-tags`; cache age logged in the report so we know which snapshot the cardinality math used).

### Strategy interface

Each strategy is a function `propose_filters(query, extracted_facets, tags_index) -> {tags: [...], tags_match: "any"|"all"}`:

- **`no_filter`** — returns `{tags: [], tags_match: "any"}` (pure semantic/keyword/graph fusion baseline).
- **`hand_tuned`** — the current heuristic: extract node/resource/operation facets from the query (reuse the deterministic tag-derivation table from #85 §9.3), filter on the matching `node:`/`operation:` tag. The thing we suspect overfits.
- **`cardinality`** — the generic rule under test (§6): from extracted facets, candidate tags are kept **only if** `tags_index[tag].count ≤ THRESHOLD` (selective). High-cardinality candidate tags are dropped (not used as filters). Optionally **boost** rather than hard-filter mid-cardinality tags (a sub-variant `cardinality_boost`).
- **`structured_lookup`** — **the strategy that actually worked at real 360k scale** (see #85 §8c). Uses `tags_match: "all"` with `[type:node-spec, node:<identified-type>]` and optionally `resource:<r>`. This is a *deterministic scoped query*, not semantic ranking — the tags are the index, semantic only tiebreaks within scope. **Scored 8/8 at real scale** vs 0/12 baseline, 2/12 cardinality, 4/12 type-only. This is now the primary strategy under test.

    **Critical implication: this strategy requires NODE IDENTIFICATION as a front-door step.** The structured lookup needs `node:<type>` — which works when the query names the node/service. For vague-intent queries ("post to a chat channel when a form fires"), node-identification must come from either (a) name-match against a cached codex of node display-names/aliases, or (b) a prior semantic design-layer recall that suggests the node. **The harness must measure node-identification separately** — see §4b.

Facet extraction is shared and deterministic (no LLM in the harness loop — keeps runs cheap and reproducible). If a query mentions "slack" we derive candidate tag `node:n8n-nodes-base.slack`; the *strategy* decides whether to use it based on its count.

**Real-scale context (2026-06-05):** strategies `no_filter`, `cardinality`, and `hand_tuned` were all designed before the 360k live test. The test showed: (a) community topic-tags (`tag:slack`, `tag:postgres`) collide with our node tags under `tags_match=any`, making cardinality filtering ineffective for node specs; (b) per-op homogeneity means even scoping to `type:node-spec` alone only reaches 4/12 because 190 per-op units swamp single-unit nodes. Only `structured_lookup` with node+resource identity worked. The cardinality strategy may still help the *design/gotcha* layer (task #84) but is not the node-spec answer.

### Subcommands

```
harness.py tune    --strategy cardinality   # reads TRAIN only; sweeps threshold; writes chosen params
harness.py eval    --strategy <name> --split holdout|train|all
harness.py compare --strategies no_filter,hand_tuned,cardinality --split all   # the headline report
```

### Loop (per pair, per strategy)

1. Load pairs (filter by `--split`; refuse holdout in `tune`).
2. For each pair → each strategy: build filters → POST recall → collect returned unit `document_id`s + their tags (in rank order).
3. Score per-pair metrics (§4); bucket by `kind`, `family`, `intent`, `split`.
4. Aggregate: mean MRR / recall@k / precision@k / Hit@1 + partial-credit distribution, per (strategy × split) and per (strategy × family).
5. Emit `report.md` (human side-by-side table: rows = strategies, columns = TRAIN MRR | HOLDOUT MRR | gap | recall@5 | Hit@1 | right-node-wrong-op%) and `report.json` (full per-pair detail for diffing).
6. **Rate-limit / robustness:** small fixed delay between calls, 3× retry with backoff on 429/5xx, and a `--limit N` for smoke runs. Network errors on a pair are recorded as `error` (excluded from metric denominators, counted in the report header) — never silently scored as a miss.

### Outputs

- **`report.md`** — the decision artifact a human reads: the strategy comparison table split TRAIN/HOLDOUT, the adoption-gate verdict (pass/fail per bar), and the partial-credit breakdown.
- **`report.json`** — every pair's ranked ids + scores, for regression diffing across bank snapshots and for debugging individual failures.

---

## 6. How the harness tunes the cardinality filter

The cardinality strategy has effectively one continuous knob (the selectivity **THRESHOLD** on tag count / equivalently an IDF cutoff) plus two discrete knobs (**which tag namespaces** are eligible to filter on — `node:`/`operation:`/`integration:`/`resource:` yes, `source:`/`type:` no; and **stemming on/off** for facet→tag matching).

**Tuning procedure (TRAIN only):**

1. **Sweep THRESHOLD** over a grid derived from the real distribution — e.g. percentiles of the `tags_cache.json` count distribution, plus fixed anchors `{50, 200, 1000, 5000, 20000}`. (The real range is 2→268,049, so the grid must be log-spaced, not linear.)
2. For each grid point, run `eval --split train` with the cardinality strategy and record **TRAIN MRR**.
3. **Pick the THRESHOLD that maximizes TRAIN MRR**, breaking ties toward the *smaller* threshold (more conservative filtering → less risk of over-filtering rare-but-correct units).
4. Repeat the sweep with each discrete knob setting (namespace set, stemming) — small grid, full cross-product is fine.
5. **Freeze** the winning `{threshold, namespaces, stemming}` into `scripts/eval/strategies.py` (or a `tuned_params.json`) with the `DATASET_VERSION` it was tuned against.
6. **Validate on HOLDOUT exactly once:** run `eval --split holdout` with the frozen params. Report HOLDOUT MRR and the **train↔holdout gap**. If the gap > 0.10 → flagged overfit; the recommendation becomes "ship a conservative default threshold (e.g. the 25th-percentile count), not the TRAIN-optimal one," and we re-broaden the dataset.

This makes the cardinality rule's single free parameter chosen *honestly*: optimized where we're allowed to look, judged where we're not.

---

## 7. Phasing

**v1 — minimal, node-spec only (prove the harness, not the heuristic).**
- 40–60 hand-curated node-spec pairs across ~6 families (slack incl. multi-op, http, set/core, one google\*, postgres, one langchain).
- Strategies: `no_filter` + `hand_tuned` only (skip cardinality tuning).
- Metrics: MRR, recall@5, Hit@1, partial-credit. Split: hold out google\* + langchain families.
- Deliverable: `harness.py compare` produces a TRAIN/HOLDOUT table. Goal of v1 = **the measurement loop runs end-to-end against the real bank** and reproduces the "semantic-alone fails / tag-filter wins" finding *at scale* (or surprises us).

**v2 — full anti-overfitting evaluation.**
- Add **accepted-answer-mined gotcha pairs** (§2c) and **workflow pairs** (§2b) → 200–300 total.
- Add the **`cardinality` strategy + the tuning sweep** (§6) and the train↔holdout gap gate.
- Broaden holdout to whole families across all three kinds.
- Deliverable: the adoption decision — *which* filtering strategy ships, with the cardinality threshold chosen on TRAIN and justified on HOLDOUT, plus the documented overfitting gap.

---

## 8. Open questions / risks

1. **Recall API tag-filter semantics at scale.** Does `tags_match: "all"` with a selective tag actually pre-filter the candidate set server-side (cheap, correct) or post-filter after ranking (could drop a correct unit that ranked outside the recall window)? **Verify against the live `n8n` bank early** — it changes whether `cardinality` should hard-filter or only boost. (Resolve before §6 tuning.)
2. **Ground-truth quality for mined gotchas.** `has_accepted_answer` means *an* answer was accepted, not that it's the uniquely-best unit, and the accepted answer may have been chunked into multiple units. Mitigate: accept any in-thread answer unit as a hit (`expected_ids` = all units of the accepted answer), and hand-audit a 10% sample. Risk: noisy ground truth inflates/deflates all strategies equally-ish, but could mask small differences.
3. **Tag-cache staleness vs. bank drift.** The bank is synced nightly; counts and unit ids move. The report logs the `tags_cache.json` age and a bank-stats snapshot so two runs are comparable; large drift between a TRAIN tune and a HOLDOUT eval invalidates the gap check. Consider pinning both to one snapshot window.
4. **Family-holdout reduces effective training size.** Holding out whole families is correct for overfitting detection but shrinks TRAIN. If TRAIN MRR is noisy, widen the dataset before trusting the swept threshold (don't tune on 15 pairs).
5. **Facet extraction is itself a heuristic.** The deterministic query→candidate-tag mapping (shared across strategies) could be the real bottleneck or the real source of overfit. Keep it strategy-independent so it can't favor one strategy, and consider a future ablation that measures extraction recall separately.
6. **Single-answer bias.** Many node-spec pairs have exactly one correct unit, which makes precision@k mechanically low for k>1. Report precision@k for transparency but **gate on MRR/recall**, which are the meaningful signals for single-answer retrieval.
7. **Does `cardinality` need per-namespace thresholds?** One global count threshold may be wrong if `node:` counts and `integration:` counts live on different scales. The sweep (§6 step 4) treats namespace-eligibility as a knob, but a v3 may need a *per-namespace* threshold — out of scope for v2, noted.
8. **Hand-tuned strategy is a moving target.** It must be frozen (a committed snapshot of the current heuristic) for the comparison to be meaningful across runs; otherwise we can't attribute metric changes to the bank vs. the code.

---

## Relationship to other tasks

- **#85 (build-layer ingestion)** produces the units and the tag schema this harness measures retrieval over. The harness's facet→tag mapping reuses #85 §9.3's deterministic derivation table — do not fork it.
- The harness is **read-only** against the bank (recall + tags GET). It never retains or mutates units, so it is safe to run against the production `n8n` bank.
