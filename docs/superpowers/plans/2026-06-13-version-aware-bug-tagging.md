# Version-Aware Bug Surfacing via Node-Tagged GitHub Issues + Engagement Ranking

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make gotcha recall surface the right node's known bugs by tagging the *entire* GitHub-issue corpus with `node:X` via automated detection, then letting **engagement metrics** (reactions/comments) be the selection pass — NOT a hand-curated bug list. The known gotchas surface because they are genuinely high-engagement bugs, so the mechanism generalizes beyond the eval set and does not teach to the test.

**Architecture:** Three layers, all generalizable:
1. **Relevance** — bulk re-retain GitHub issues with `node:X` tags assigned by automated node-detection (`node_lookup.py`, 3,537-entry dict) on issue title/body. Tags *every* node-relevant issue, not a curated 20.
2. **Importance** — the existing engagement-based confidence scoring (`github_base` + reaction/comment/state bonuses in `format_results.py`) ranks high-engagement bugs to the top. This is the *only* selection criterion.
3. **Version awareness** — release notes (already ingested, already structured) carry `version:X.Y.Z` tags on atomic "fix (issue #N)" facts; cross-reference issue numbers to derive `fixed-in` (best-effort).

**Tech Stack:** Bash (`structured_recall.sh`, `auto-recall.sh`), Python (`node_lookup.py`, `format_results.py`, n8n-hindsight `sync-github.py`), curl (Hindsight API).

---

## Background — verified findings (2026-06-14 small-scale test)

These were confirmed empirically against the live n8n bank, and they materially change the original plan:

1. **Recall endpoint is `POST /public/recall`** (no auth, hardwired to the n8n bank). The `/v1/default/banks/n8n/recall` path is 404. Retain is `POST /v1/default/banks/n8n/memories` with `{"items":[{"content":...,"tags":[...],"metadata":{...}}], "async":true}`.

2. **Tag-filtered recall works and preserves tags through consolidation.** `tags:[node:X, type:known-bug], match=all, budget=low` returned 4–6 precise results per node, with all schema tags intact. Consolidation also *distilled* each retain into atomic facts (bug statement and workaround as separate recallable units).

3. **Release notes are ALREADY correctly structured — no re-retain needed.** The 621 ingested releases consolidated into atomic facts like *"Bug fix: Summarize Node - Fix type casting (issue #14259)"*, each carrying `version:X.Y.Z` as BOTH metadata and a `version:` tag. Issue→version linking already works (e.g. #8013 → 1.21.0).

4. **`retain_mission` AND `observations_mission` are ALREADY set and n8n-tuned.** retain_mission explicitly says: *"For release notes: capture the version number, release date, bug fixes with their GitHub issue numbers..."*. observations_mission says: *"release history linking fixes to versions... link fixes to their issue numbers."* No mission change is required.

5. **Raw GitHub issues do NOT carry `node:X` tags** (confirmed: `node:supabase` returns only hand-curated memories, zero raw issues). This is the one real gap. The current untagged semantic gotcha recall returns ~1 on-target + 3 noise (Facebook/Slack/tangential).

6. **Curated known-bug memories = teaching to the test.** Hand-authoring a memory for each of the 20 eval gotchas inflates the score without generalizing. **Rejected.** The 5 test memories retained on 2026-06-14 (2 OpenAI, supabase, merge, wait) must be REMOVED before any eval run (see Task 0 — done).

7. **`tags_match` must be `all_strict` for precision (cross-session finding).** `tags_match: "all"` and `"any"` deliberately **include untagged memories** by design — so `tags:["node:X"], "all"` would let the old Facebook/Salesforce noise bleed back in at scale. The strict variants `all_strict` / `any_strict` exclude untagged memories. **All gotcha/node recall must use `all_strict`.** (Today's small-scale tests used `"all"` and looked clean only because the curated memories were nearly the only tagged matches; this won't hold against 430K mostly-untagged memories.) Recall can also return duplicates — dedupe downstream (auto-recall.sh already does via its round-robin seen-set). Re-verify the leak empirically in Task 2 once real issues are node-tagged.

8. **Engagement "waterline" — no recorded ≥5 gate exists.** Searched dan-shared AND this session's full transcript: there is NO retained "re-retain if engagement ≥ 5" decision. The only configured engagement numbers are *display/scoring tiers* in `plugin_config.py`: `high_engagement_threshold: 10`, `medium_engagement_threshold: 3`, formula `engagement = reactions_total + (comments × 4)`. These gate confidence/inclusion-depth at render time, not re-retain. A re-retain inclusion floor is an OPEN decision (Task 3).

### Tag Schema (for reference / version cross-ref only)

- **Node:** `node:openai`, `node:merge`, `node:supabase`, `node:wait`, `node:httpRequest`, …
- **Version:** `version:X.Y.Z` (on release-note facts, already present), `fixed-in:X.Y.Z` (derived, best-effort)
- **State (from existing GitHub metadata):** `state:open` / `state:closed` + `state_reason` — already enriched at sync time (task #63).

We deliberately do NOT introduce a `type:known-bug` curation tag, because deciding what counts as a "known bug" by hand is the teaching-to-the-test move. Bug-ness is inferred from engagement + state, not asserted by us.

---

### Task 0: Remove the teaching-to-the-test curated memories — ✅ DONE (2026-06-14)

**Files:** None (API calls only)

The 5 curated known-bug memories retained during the 2026-06-14 test would pollute eval results by hand-feeding the exact gotchas the eval checks (and they carry `node:X` tags, so they'd surface even under the new node-only recall). Removed.

- [x] **Step 1: Captured** all 5 retains (consolidated into 20 `type:known-bug` units across 3 parent documents) — full text saved to dan-shared as a `type:training-template` memory for later reproduction/automation.
- [x] **Step 2: Deleted** via `DELETE /v1/default/banks/n8n/documents/{document_id}` (the working mechanism — `DELETE /memories/{id}` and `/memories/clear` both return 405). Deleting the 3 parent docs removed all 20 units (the 9 `document_id:None` units were chunks of the same parents).
- [x] **Step 3: Verified** 0 `type:known-bug` units remain, per-node (supabase/merge/wait/openai) and globally.

**Endpoint reference (learned):** retain = `POST /v1/default/banks/n8n/memories` `{items:[{content,tags,metadata}],async:true}`; recall = `POST /public/recall` (no auth); delete = `DELETE /v1/default/banks/n8n/documents/{document_id}`.

---

### Task 1: Wire the canonical node detector into GitHub sync (node:X tagging)

> **Status (2026-06-15):** node-detection false-positive FIXED + canonical-source documented (commits dc382c4, 701761e on `feat/version-aware-bug-surfacing`). Design resolved (below). Engagement floor = **5** (Dan, changeable). Dry-run with NO writes first; then **1–3 test retains only** — no bulk re-retain (don't burn DeepSeek before validation).

**Design (resolved in conversation 2026-06-15):**
- **Canonical detector = the plugin's `hooks/lib/node_lookup.py` + `node_lookup_data.json`.** It has the verb-stem + demotion fixes (incl. `workflows`→workflowTrigger). It carries a CANONICAL SOURCE header (do-not-fork rule).
- **The same detector is the tag↔query contract.** Recall (`do_gotcha_recall`) queries `node:<community_tag(service)>`; ingest must WRITE the *identical* tag string or recall silently misses (a node written `node:openai` but queried `node:open-ai` returns nothing — no error, just degraded recall). So the tag-format mapping (`_node_to_community_tag`) must ALSO be single-canonical, not a second copy.
- **Distribution constraint forces a vendored copy:** the plugin ships to users via the marketplace with no guaranteed `pip install`, so it MUST vendor `node_lookup.py` + data (can't depend on a package). Therefore n8n-hindsight keeps a **byte-identical vendored copy** of the *logic*, and a **hash-pin parity guard** (mirrored in both repos, same pattern as `test-hash-parity.sh`) fails CI on drift. The **data file is regenerated from `nodes.db`** (single source for the data) and is NOT hash-pinned (it changes on every catalog refresh).
- A shared pip library was considered and rejected for the plugin side (breaks on user machines); it's optional polish for server-side consumers only.

**Files:**
- Modify: `hooks/lib/node_lookup.py` — add canonical `service_to_tag(service)` (the `_node_to_community_tag` body) + `community_tag(node_type)` (strip prefix + `Trigger`/`Tool` suffix → service → `service_to_tag`).
- Modify: `hooks/lib/structured_recall.sh` — `_node_to_community_tag` calls `node_lookup.service_to_tag` (single source; delete the inline python mapping copy).
- Vendor → n8n-hindsight: copy fixed `node_lookup.py`; regenerate `node_lookup_data.json` via `sync-nodes.py --refresh-lookup`; delete the stale `.worktrees/...preflight` copy.
- Modify: `n8n-hindsight/scripts/sync-github.py:format_item()` — node-detect + floor + `node:X` tags.
- Add: hash-pin parity guard (`tests/test-node-lookup-parity.sh`) in both repos.

- [x] **Step 1: Extract the tag mapping to canonical Python (plugin, TDD).** `service_to_tag()`/`community_tag()` in `node_lookup.py`; `tag_*`/`svc_to_tag_*` tests. (commit 21b69fd)

- [x] **Step 2: Route the bash recall path through it.** `_node_to_community_tag` now imports `node_lookup.service_to_tag` (inline MAP copy deleted). Recall E4 still sees `node:` tags; full suite green. (commit 21b69fd)

- [x] **Step 3: Vendor to n8n-hindsight.** Byte-identical `node_lookup.py` + `node_lookup_data.json` in `n8n-hindsight/scripts/lib/` (branch `feat/github-node-tagging`). Note: master had NO prior node_lookup in `scripts/` — the stale `.worktrees/...preflight` copy lives on another branch and was left untouched. Data regeneratable from `nodes.db` via `sync-nodes.py --refresh-lookup`. (commit bb024fc)

- [x] **Step 4: Wire `sync-github.py:format_item()`.** `RETAIN_ENGAGEMENT_FLOOR = 5`; `detect_node_tags()` gates on `reactions_total + comments*4 >= floor`, runs `identify_nodes`, maps via `community_tag`, dedup, cap 5. Best-effort (import failure → sync still runs). (commit bb024fc)

- [x] **Step 5: Hash-pin parity guard** in both repos — `sha256(node_lookup.py)` pinned (`bc8ea6c5…`) in `n8n-knowledge/tests/test-node-lookup-parity.sh` AND `n8n-hindsight/.../test_github_node_tagging.py::test_parity_node_lookup_hash`. (commits 4f52581, bb024fc)

- [x] **Step 6 (unit-level): tagging logic validated on synthetic issues** — supabase/merge/wait (high-eng) → tagged; below-floor → no tags; generic "large workflows" (eng 30) → NO `node:workflowTrigger`; floor boundary (5) inclusive. (n8n-hindsight pytest 6/6; full sync suite 109/109)

- [ ] **Step 6 (network): live dry-run** — ⛔ HAND-OFF: needs `GITHUB_TOKEN` (absent in build shell). Run `python3 scripts/sync-github.py --dry-run --test N` on real fetched issues; confirm tags on real text match the unit expectations before any write.

- [ ] **Step 7: 1–3 live test retains** — ⛔ HAND-OFF: needs token + Dan's go (writes to prod bank). Idempotent via `document_id`. Verify recall finds them with `tags:["node:X"], tags_match:"all_strict"`, and confirm `"all"` leaks vs `"all_strict"`. Then STOP — bulk re-retain (Task 3) stays gated on Dan.

---

### Task 2: Small-scale re-retain + engagement-ranked recall test

**Files:** None (API + sync script)

Prove the generalizable path end-to-end on a handful of REAL issues (not curated memories): node-tag them via the sync pass, then confirm engagement-ranked recall surfaces the genuine high-engagement bug first.

- [ ] **Step 1: Re-retain ~20–50 node-tagged issues** (the sample from Task 1.4) into the n8n bank. Dedup via existing `document_id` so this is idempotent.

- [ ] **Step 2: Tag-filtered recall by node ONLY (no curation tag)**

```bash
curl -s -X POST -H "Content-Type: application/json" \
  "https://n8nhindsight.applikuapp.com/public/recall" \
  -d '{"query":"supabase credential authorization bug","budget":"low","max_tokens":2000,"tags":["node:supabase"],"tags_match":"all_strict"}' \
  | python3 -m json.tool
```

Expect: real Supabase issues (now node-tagged), ranked by engagement, NOT Facebook/Slack noise. **Use `all_strict`** — `all`/`any` include untagged memories and would re-admit the noise (finding #7). Also run the same query with `"all"` here to empirically confirm the leak (it should pull in untagged noise that `all_strict` excludes).

- [ ] **Step 3: Confirm engagement ranking surfaces the gotcha**

Verify the high-engagement Supabase credential issue (#30630) ranks above low-engagement Supabase chatter. This is the "engagement as selection criterion" proof — it works WITHOUT us tagging #30630 as special.

- [ ] **Step 4: Repeat for merge, wait, and 1–2 non-gotcha nodes**

The non-gotcha nodes (e.g. `node:httpRequest`) are the generalization check: recall should return that node's real issues too, proving the mechanism isn't gotcha-specific.

---

### Task 3: Bulk re-retain GitHub issues with node tags (greater scale)

**Files:** `n8n-hindsight/scripts/sync-github.py`

Once the small-scale test passes, re-retain the full bug-relevant GitHub corpus with node tags.

- [ ] **Step 1: Apply corpus scope + engagement floor.** Freshness caps (Dan, 2026-06-14): **newest ~4,500 issues** + **newest ~1,500 PRs**. **Engagement floor = 5 (DECIDED 2026-06-15, Dan):** only node-tag / re-retain GitHub issues where `engagement = reactions_total + (comments × 4) ≥ 5`. Make the floor a named constant (`RETAIN_ENGAGEMENT_FLOOR = 5`) so it's a one-line change — Dan explicitly wants to roll back / retune n later. Below the floor: skip node-tagging (issue stays as-is, not promoted). Current bank state (verified): ~4,530 issue docs already ingested (`TARGET_TOTAL=4500`); ~4,800 old closed issues intentionally excluded. Log how many issues fall below the floor (no silent truncation).

- [ ] **Step 2: Run the Hindsight bulk sync checklist** (CLAUDE.md):
  - Instance n8nhindsight / bank n8n ✓
  - observations_mission set ✓ (verified — no change)
  - State file / cron overlap (github cron is 03:00 UTC — don't collide)
  - Budget: ~$0.003/consolidation × corpus size — estimate before running
  - Save formatted items to local JSON before retaining (resumable)

- [ ] **Step 3: Bulk re-retain** with `--full` (dedup via document_id).

- [ ] **Step 4: Scaled precision test** — repeat Task 2's recall probes across ~10 nodes (gotcha + non-gotcha). Check for cross-bleed and confirm engagement ranking holds at scale.

---

### Task 4: Cross-reference release notes → `fixed-in` (best-effort, Tier 2)

**Files:** Create `scripts/tag-fixed-bugs.py` (one-off)

Release-note facts already carry `version:X.Y.Z`. For issues whose number appears in a release-note fix fact, derive `fixed-in:X.Y.Z`.

- [ ] **Step 1: Build the issue→version map** by recalling release-note facts and extracting `(issue#, version:tag)` pairs.

- [ ] **Step 2: Match against node-tagged issues** and attach `fixed-in:X.Y.Z` where a release note cites the issue.

- [ ] **Step 3: Accept partial coverage.** Verified: many fixes (e.g. the OpenAI credential bug) cite NO issue number in any changelog. Log coverage %; this is the existing task #83 "last ~10%", not a blocker.

---

### Task 5: Modify `do_gotcha_recall` to node-filtered + engagement-ranked recall

**Files:**
- Modify: `hooks/lib/structured_recall.sh` (`do_gotcha_recall`)
- Modify: `tests/test-auto-recall-resilience.sh`

Switch from untagged semantic search to `tags:["node:X"], tags_match:"all"` (relevance), relying on the existing engagement-based scoring in `format_results.py` for selection. No `type:known-bug` filter (that would require curation).

- [ ] **Step 1: Write the failing test** — assert gotcha requests include a `node:X` tag filter (grep the stub body log for `"node:`), exactly one per detected node.

- [ ] **Step 2: Run test to verify it fails** — current `do_gotcha_recall` sends no tags.

- [ ] **Step 3: Implement node-filtered recall**

```bash
do_gotcha_recall() {
  local node_type="$1"
  local service
  service=$(echo "$node_type" | sed 's/.*\.//' | sed 's/Trigger$//' | sed 's/Tool$//')
  local tag
  tag=$(_node_to_community_tag "$service")

  local query_escaped
  query_escaped=$(printf '%s node bug issue workaround error' "$service" | recall_json_escape)

  # Relevance via node tag; importance via engagement scoring downstream.
  # No curation tag — bug-ness is inferred from engagement + state, not asserted.
  # all_strict (NOT all): all/any include untagged memories by design and would
  # re-admit the cross-node noise this whole change exists to eliminate.
  local body
  body=$(printf '{"query": %s, "budget": "low", "max_tokens": 2000, "tags": ["node:%s"], "tags_match": "all_strict", "include": {"source_facts": {}}}' \
    "$query_escaped" "$tag")

  local result
  result=$(recall_post "$body")
  if ! echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
    sleep 1
    result=$(recall_post "$body")
  fi
  echo "$result"
}
```

- [ ] **Step 4: Run test to verify it passes.**

- [ ] **Step 5: Run full suite** — `bash tests/run-all.sh`.

- [ ] **Step 6: Commit.**

---

### Task 6: Restructure auto-recall to tiered architecture + drop mental models

**Files:**
- Modify: `hooks/auto-recall.sh`, `hooks/lib/structured_recall.sh`, `tests/test-auto-recall-resilience.sh`
- Remove: `hooks/lib/section_selector.py`

Mental models are curated bug catalogs — the same teaching-to-the-test problem as curated memories, plus a staleness/maintenance burden. Now that GitHub issues are node-tagged and engagement-ranked, drop mental models entirely.

#### New flow:
```
Tier 1 (always, parallel):  node-filtered gotcha recall (per node) + structured node-spec recall + nodes.db inject
Tier 2 (conditional):       semantic recall — only if Tier 1 returned < 2 results
```

- [ ] **Step 1: Update tests** — remove mental-model isolation vars; add E4 asserting Tier 2 semantic recall is skipped when Tier 1 has ≥ 2 results.
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Remove mental-model code** from `structured_recall.sh` (`_ensure_manifest`, `_manifest_hash`, `do_mental_model_recall`, `MENTAL_MODEL_*` vars).
- [ ] **Step 4: Restructure `auto-recall.sh`** to Tier 1 (node-filtered gotcha + structured, parallel) → conditional Tier 2 (semantic when `TIER1_COUNT < 2`). Remove the `MM_CONTENT` phase, `MM_DIR`, and the mental-model header injection block.
- [ ] **Step 5: Remove `section_selector.py`** (`git rm`) and its references.
- [ ] **Step 6: Run full suite.**
- [ ] **Step 7: Commit.**

---

### Task 7: Version-aware relevance filtering (deferred)

**Files:** `hooks/lib/format_results.py`, `hooks/auto-recall.sh`

**Deferred until Tasks 1–6 validated.** Once issues carry `fixed-in:X.Y.Z` and the validator proxy exposes the user's n8n version:
- user version ≥ fixed-in → demote/suppress (already fixed for them)
- user version < fixed-in → promote + show workaround
- no fixed-in → show with current state context

Depends on: validator version exposure (works for validation-enabled users) + Task 4 coverage.

---

### Verification Checklist

- [x] Task 0: 5 curated test memories removed (recall returns 0 for `type:known-bug`)
- [x] `do_gotcha_recall` sends `node:X` filter with `all_strict` (no `type:known-bug` curation tag) — commit 5428a16
- [x] Layer 2: gotcha query is task-aware (folds prompt keywords) — commit 5428a16
- [x] Semantic recall skipped when Tier 1 ≥ 2 results — commit 54ce8fb
- [x] Mental-model code fully removed (`do_mental_model_recall`, `section_selector.py` gone) — commit 54ce8fb
- [x] Node-detection false-positive fixed (`workflows`→workflowTrigger) + canonical-source header — commits dc382c4, 701761e
- [ ] Tag↔query contract single-canonical: `community_tag()` in `node_lookup.py`, bash `_node_to_community_tag` routes through it (no second copy)
- [ ] n8n-hindsight vendors byte-identical `node_lookup.py`; stale preflight copy removed; hash-pin parity guard mirrored in both repos
- [ ] `node:X` tagging in `sync-github.py` validated on a dry-run sample (precision recorded, floor=5 gating works, no false workflowTrigger)
- [ ] Small-scale: node-filtered recall returns real issues, engagement ranks the gotcha on top, no curated input; `all` leak vs `all_strict` confirmed
- [ ] Generalization: non-gotcha nodes also return their real issues
- [ ] Scaled re-retain (Task 3, gated on Dan): no cross-bleed across ~10 nodes; engagement ranking holds
- [ ] `fixed-in` coverage % logged (partial is expected)
- [ ] `bash tests/run-all.sh` all pass
- [ ] No API keys committed to n8n-knowledge repo
