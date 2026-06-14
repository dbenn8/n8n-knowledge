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

### Task 1: Add automated `node:X` tagging to GitHub-issue sync

> **⚠️ FINDINGS (2026-06-15) — needs Dan's decision before touching the production ingest path.** Inspected `n8n-hindsight/scripts/sync-github.py`:
> - **Insertion point is clean:** `format_item()` (lines 166–206) builds `tags`/`metadata` and has `title`, `body`, `reactions`, `comments` in scope. `--dry-run` exists (fetch+filter, no retain). Engagement floor = 5 is easy: `reactions_total + comments*4 >= 5` gates whether to add `node:X` tags.
> - **`identify_nodes()` works on issue text** — probed: "Official Supabase node rejects credentials" → `nodes-base.supabase` ✓, "Merge node silently loses rows" → `nodes-base.merge` ✓, "Wait node never resumes" → `nodes-base.wait` ✓.
> - **BUT correctness blocker:** the n8n-hindsight node_lookup is a **divergent copy** (`.worktrees/n8n-knowledge-validator-preflight/hooks/lib/node_lookup.py`) that still has the **verb/demotion false-positive** — "Improve editor performance for large **workflows**" → `nodes-base.workflowTrigger` (wrong). The plugin's `node_lookup.py` already fixed this (verb-suffix stripping + `_DEMOTED_BARE_TOKENS` in fuzzy). Tagging 4,500 issues with the buggy copy would stamp false `node:*` tags.
> - **Decision needed (the open "which source" question, now with teeth):** (a) regenerate `node_lookup_data.json` from `nodes.db` via `sync-nodes.py --refresh-lookup` AND use the **fixed** detection logic, vs (b) reconcile the two `node_lookup.py` copies into one shared module first. Either way, resolve the divergence before a production write. Left unbuilt deliberately — server-side prod write + unresolved sourcing is Dan's call.

**Files:**
- Modify: `n8n-hindsight/scripts/sync-github.py` (issue ingestion — add node-detection pass in `format_item`)
- Reference: `n8n-knowledge/hooks/lib/node_lookup.py` (FIXED detection logic — has verb-stem + demotion fixes)

The generalizable relevance mechanism: run each issue's title + body through node-detection and attach `node:X` tags for every node mentioned. This tags the whole corpus, not a curated subset.

**Where node-detection runs (resolved):** issue-tagging happens **server-side in `sync-github.py`** (n8n-hindsight) at ingest time — the resulting `node:X` tags are baked into the bank, so **plugin users never need a local `nodes.db`** for this feature (Dan's distribution concern: most plugin users won't install n8n-mcp and won't have its bundled db — that's fine here, the detection is server-side). Source the detection dictionary from the **server-side node database the n8n-validator/validator-app deployment already has** (validator-app installs n8n-mcp in its Docker image; no committed `nodes.db` in the repo — it's built into the image). Do NOT assume the plugin's shipped `node_lookup.py` dict or n8n-mcp's local install on a user machine. n8n-hindsight already has an "ingestion-integrated path … using single source of truth with database" (the `--refresh-lookup` mechanism) — reuse that to build the detector from the validator's db.

- [ ] **Step 1: Locate the issue-retain path in sync-github.py**

```bash
grep -n "tags\|retain\|node" /Users/danielbennett/codeNew/n8n-hindsight/scripts/sync-github.py | head -40
```

- [ ] **Step 2: Add a node-detection helper**

Per the design decision above, make node-detection available to the sync script (prefer building the lookup from `nodes.db` so there is no second copy of the dictionary). Detect on `title + " " + body`, dedup, cap at e.g. 5 node tags per issue to avoid tag spam on issues that name many nodes.

- [ ] **Step 3: Attach `node:X` tags to each issue's retain item**

Append detected `node:X` tags to the existing tag list (`source:github-issues`, `type:github-issue`, state tags, etc.). Preserve all existing tags and metadata.

- [ ] **Step 4: Dry-run on a small sample**

Run sync-github.py in `--dry-run`/`--test N` mode (confirm flag exists) over ~20 issues that mention known gotcha nodes (Supabase #30630, Merge, Wait #29160/#31513, OpenAI #30311). Verify each gets the correct `node:X` tag(s) and no spurious ones.

- [ ] **Step 5: Validate detection precision on the sample**

Manually check the dry-run output: did "Supabase node fails credential testing" get `node:supabase`? Did a generic "workflow won't save" issue avoid false node tags? Record precision/recall on the 20-issue sample before scaling.

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

- [ ] Task 0: 5 curated test memories removed (recall returns 0 for `type:known-bug`)
- [ ] `node:X` tagging in sync-github.py validated on a 20-issue sample (precision recorded)
- [ ] Small-scale: node-filtered recall returns real issues, engagement ranks the gotcha on top, no curated input
- [ ] Generalization: non-gotcha nodes also return their real issues
- [ ] Scaled re-retain: no cross-bleed across ~10 nodes; engagement ranking holds
- [ ] `do_gotcha_recall` sends `node:X` filter (no `type:known-bug` curation tag)
- [ ] Semantic recall skipped when Tier 1 ≥ 2 results
- [ ] Mental-model code fully removed (`do_mental_model_recall`, `section_selector.py` gone)
- [ ] `fixed-in` coverage % logged (partial is expected)
- [ ] `bash tests/run-all.sh` all pass
- [ ] No API keys committed to n8n-knowledge repo
