# Build Layer Implementation Plan (#85)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the n8n-knowledge plugin a "build layer" — node specs + workflow examples — so the harness can design AND build n8n workflows with zero install.

**Architecture:** Two ingestion scripts (sync-nodes.py, sync-workflows.py) retain node-spec and workflow data to the n8n bank under named strategies. The plugin's auto-recall hook detects node names in prompts via a cached dictionary, issues structured tag-filtered recalls for node specs, and merges them with the existing semantic design-layer results.

**Tech Stack:** Python 3 (stdlib urllib, sqlite3, json), bash tests (existing harness), Hindsight retain/recall API (authenticated HTTP).

**Repos touched:** `n8n-hindsight` (ingestion scripts), `n8n-knowledge` (plugin hooks/formatter/tests).

---

### Task 1: Node-name dictionary + identifier module (`n8n-knowledge`)

The foundation — a Python module that maps service/node display names to `node:<type>` tags. Used by the auto-recall hook to decide when to issue a structured node-spec lookup.

**Files:**
- Create: `hooks/lib/node_lookup.py`
- Create: `hooks/lib/node_lookup_data.json` (generated, checked in — the cached dictionary)
- Test: `tests/test-node-lookup.sh`
- Test fixture: `tests/fixtures/node-lookup-queries.json`

- [ ] **Step 1: Write the test fixture** — 30 diverse `(query, expected_node_type)` pairs spanning named queries ("configure the Slack node"), integration queries ("send data to Postgres"), trigger queries ("listen for Gmail events"), short-name nodes ("use the If node"), and negatives ("make an API call" → no match without the node name).

```json
[
  {"query": "configure the Slack node to post a message", "expect": "nodes-base.slack"},
  {"query": "use the If node to branch", "expect": "nodes-base.if"},
  {"query": "listen for Gmail events", "expect": "nodes-base.gmailTrigger"},
  {"query": "send data to Postgres", "expect": "nodes-base.postgres"},
  {"query": "use the HTTP Request node", "expect": "nodes-base.httpRequest"},
  {"query": "make an API call", "expect": null}
]
```

- [ ] **Step 2: Write the failing test**

```bash
# tests/test-node-lookup.sh
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../hooks/lib"
PASS=0; FAIL=0
assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then echo "  PASS: $desc"; PASS=$((PASS+1))
  else echo "  FAIL: $desc (expected '$expected', got '$actual')"; FAIL=$((FAIL+1)); fi
}
echo "=== node lookup tests ==="
# Test: identify returns the correct node type for each fixture query
FIXTURE="$SCRIPT_DIR/fixtures/node-lookup-queries.json"
python3 -c "
import json, sys
sys.path.insert(0, '$LIB_DIR')
from node_lookup import identify_nodes
fixtures = json.load(open('$FIXTURE'))
for f in fixtures:
    result = identify_nodes(f['query'])
    top = result[0][1] if result else None
    # normalize: strip package prefix for comparison
    exp = f['expect']
    got_base = top.split('.')[-1] if top else None
    exp_base = exp.split('.')[-1] if exp else None
    status = 'PASS' if got_base == exp_base else 'FAIL'
    print(f'{status}|{f[\"query\"][:50]}|{exp}|{top}')
" | while IFS='|' read -r status query exp got; do
  if [ "$status" = "PASS" ]; then
    assert_eq "$query" "$exp" "$got"
  else
    assert_eq "$query" "$exp" "$got"
  fi
done
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
```

- [ ] **Step 3: Run test to verify it fails** (module not found)

Run: `bash tests/test-node-lookup.sh`
Expected: FAIL — `ModuleNotFoundError: No module named 'node_lookup'`

- [ ] **Step 4: Generate the dictionary data file**

Run (orchestrator — needs Bash):
```bash
cd /tmp && npm pack n8n-mcp@latest 2>/dev/null && tar xzf n8n-mcp-*.tgz package/data/nodes.db
python3 -c "
import sqlite3, json, re
c=sqlite3.connect('/tmp/package/data/nodes.db'); c.row_factory=sqlite3.Row
entries={}
for row in c.execute('SELECT node_type, display_name, is_trigger FROM nodes'):
    nt=row['node_type']; dn=row['display_name'].lower().strip()
    suffix=nt.split('.')[-1].lower()
    entries[dn]=nt; entries[suffix]=nt
    split=re.sub(r'([a-z])([A-Z])',r'\1 \2',suffix).lower()
    if split!=suffix: entries[split]=nt
    if row['is_trigger'] and 'trigger' in suffix:
        base=re.sub(r'trigger$','',suffix)
        if base: entries[base]=nt
json.dump(entries, open('hooks/lib/node_lookup_data.json','w'), indent=0, sort_keys=True)
print(f'wrote {len(entries)} entries')
"
```

- [ ] **Step 5: Write minimal implementation**

```python
# hooks/lib/node_lookup.py
import json, os, re

_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA = None

def _load():
    global _DATA
    if _DATA is None:
        with open(os.path.join(_DIR, 'node_lookup_data.json')) as f:
            _DATA = json.load(f)
    return _DATA

_TRIGGER_WORDS = {'trigger','listen','watch','fire','event','poll','subscribe','detect','monitor'}

def _action_for(lookup):
    action = {}
    for name, nt in lookup.items():
        suffix = nt.split('.')[-1].lower()
        if 'trigger' not in suffix:
            action[suffix] = nt
    return action

def identify_nodes(prompt):
    lookup = _load()
    action = _action_for(lookup)
    pl = prompt.lower()
    has_trigger = bool(_TRIGGER_WORDS & set(re.findall(r'[a-z]+', pl)))
    hits = []
    for name in sorted(lookup, key=len, reverse=True):
        if len(name) < 2:
            continue
        if name in pl:
            nt = lookup[name]
            suffix = nt.split('.')[-1].lower()
            base = re.sub(r'trigger$', '', suffix)
            if not has_trigger and base in action and 'trigger' in suffix:
                nt = action[base]
            hits.append((name, nt))
            pl = pl.replace(name, '', 1)
    return hits
```

- [ ] **Step 6: Run test to verify it passes**

Run: `bash tests/test-node-lookup.sh`
Expected: PASS — all 30 fixture queries match

- [ ] **Step 7: Add to run-all.sh and commit**

```bash
# Add test-node-lookup.sh to tests/run-all.sh
git add hooks/lib/node_lookup.py hooks/lib/node_lookup_data.json tests/test-node-lookup.sh tests/fixtures/node-lookup-queries.json
git commit -m "feat: add node-name dictionary lookup for structured node-spec retrieval"
```

---

### Task 2: Structured node-spec recall in auto-detect hook (`n8n-knowledge`)

Wire the node-lookup into the existing UserPromptSubmit hook so that when a node name is detected, a **parallel structured recall** fetches the node spec alongside the existing semantic recall.

**Files:**
- Modify: `hooks/lib/recall.sh` (add a structured-recall function)
- Create: `hooks/lib/structured_recall.sh` (issues `tags_match=all` recall for node specs)
- Modify: `hooks/auto-recall.sh` (call structured recall when node_lookup hits)
- Test: `tests/test-structured-recall.sh`
- Test fixture: `tests/fixtures/node-spec-recall.json`

- [ ] **Step 1: Write the test fixture** — a mock recall response containing `type:node-spec` tagged results with `metadata.node_type`, `metadata.resource`, `metadata.operation`, `metadata.properties_json`.

- [ ] **Step 2: Write the failing test** — given a fixture response, `format_results.py` renders node-spec results with a distinct format (node type header, property summary, not the standard `<result>` block).

- [ ] **Step 3: Run test to verify it fails**

Run: `bash tests/test-structured-recall.sh`
Expected: FAIL

- [ ] **Step 4: Write structured_recall.sh** — takes `node_type` and optional `resource` args, issues a recall with `tags=[type:node-spec, node:<type>]`, `tags_match=all`, returns raw JSON.

- [ ] **Step 5: Wire into auto-recall.sh** — after the existing semantic recall, check `identify_nodes` output; if non-empty, call `structured_recall.sh` for the top match; merge both responses before passing to `format_results.py`.

- [ ] **Step 6: Add node-spec rendering to format_results.py** — detect `type:node-spec` in tags, render as: `Node: <display_name> (<node_type>)` / `Operation: <resource>.<operation>` / `Properties: <field list from metadata>` / `Source: <doc_url>`. Suppress `properties_json` from rendered output (too large); include a note "Full property spec available — ask for details."

- [ ] **Step 7: Run test to verify it passes**

Run: `bash tests/test-structured-recall.sh && bash tests/run-all.sh`
Expected: all pass, no regressions

- [ ] **Step 8: Commit**

```bash
git add hooks/lib/structured_recall.sh hooks/auto-recall.sh hooks/lib/format_results.py tests/test-structured-recall.sh tests/fixtures/node-spec-recall.json
git commit -m "feat: structured node-spec recall via auto-detect hook"
```

---

### Task 3: Node-spec ingestion script (`n8n-hindsight`)

`sync-nodes.py` — extracts from czlonkowski's `nodes.db`, splits big multi-resource nodes into per-operation units, retains with production tags and the `node_spec` strategy.

**Files:**
- Create: `scripts/sync-nodes.py`
- No test file in-repo (orchestrator validates via `--test 3 --dry-run` and live recall probe)

- [ ] **Step 1: Write sync-nodes.py** following the `sync-code.py` pattern (env vars, `--full`/`--dry-run`/`--test N`, state file, batch retain via authenticated HTTP). Key logic:

```python
# Per node from nodes.db:
# 1. If multi-resource AND properties_schema > SPLIT_THRESHOLD (12000 chars):
#    → split into per-(resource, operation) units using displayOptions boundaries
#    → document_id = nodespec-<node_type>-<resource>.<operation>
#    → content = NL intent sentence + field list (NO raw JSON in content)
#    → metadata.properties_json = JSON property spec for that operation (capped at 9000 chars)
# 2. Else (small/single-op node):
#    → single unit
#    → document_id = nodespec-<node_type>
#    → content = NL description + operations list
#    → metadata.properties_json = full properties_schema (capped)
# Tags: type:node-spec, source:n8n-node-introspection, node:<type>, nodeclass:<class>, integration:<service>
# Per-op: + resource:<r>, operation:<r>.<op>
# Strategy: node_spec (verbatim, chunk_size 25000)
```

- [ ] **Step 2: Run dry-run to verify** (orchestrator)

```bash
python3 scripts/sync-nodes.py --dry-run --test 5
```

Expected: prints 5 node units with correct tags/metadata, no API calls

- [ ] **Step 3: Run live test** (orchestrator, against real bank)

```bash
python3 scripts/sync-nodes.py --test 3
```

Expected: 3 nodes retained, recall probe confirms they're findable

- [ ] **Step 4: Commit**

```bash
git add scripts/sync-nodes.py
git commit -m "feat: sync-nodes.py — ingest node specs from czlonkowski nodes.db"
```

---

### Task 4: Workflow-example ingestion script (`n8n-hindsight`)

`sync-workflows.py` — ingests the 28 official `docs/_workflows/**.json` as node-level + topology + source units.

**Files:**
- Create: `scripts/sync-workflows.py`

- [ ] **Step 1: Write sync-workflows.py** following the same pattern. Key logic:

```python
# For each workflow JSON file:
# 1. Parse nodes + connections
# 2. Build wiring map (who receives from / sends to whom, via which connection type)
# 3. Per node → node unit:
#    content = "Node <name> (type <type>) in workflow <wf>. Receives: ... Sends: ... Config JSON: <node obj>"
#    document_id = wf-<slug>-node-<nodeNameSlug>
#    tags = type:workflow-node, source:n8n-docs-workflows, wf:<slug>, node:<type>, trigger:<type>, ...
# 4. Topology unit:
#    content = "Topology of <wf>: <edges>"
#    document_id = wf-<slug>-topo
# 5. Source unit (full JSON, suppressed from auto-recall):
#    content = full workflow JSON
#    document_id = wf-<slug>-source
#    tags += type:workflow-source
# Strategy: workflow_json (verbatim)
```

- [ ] **Step 2: Dry-run**

```bash
python3 scripts/sync-workflows.py --dry-run --test 2
```

- [ ] **Step 3: Live test** — ingest 2 workflows, recall probe

- [ ] **Step 4: Commit**

```bash
git add scripts/sync-workflows.py
git commit -m "feat: sync-workflows.py — ingest official workflow examples as node-level units"
```

---

### Task 5: Node-lookup dictionary refresh script (`n8n-knowledge`)

A small script that regenerates `node_lookup_data.json` from a fresh `nodes.db`. Run manually when bumping the n8n version.

**Files:**
- Create: `scripts/refresh-node-lookup.sh`

- [ ] **Step 1: Write refresh script**

```bash
#!/usr/bin/env bash
# Refresh node_lookup_data.json from the latest n8n-mcp npm package
set -euo pipefail
cd /tmp && npm pack n8n-mcp@latest 2>/dev/null && tar xzf n8n-mcp-*.tgz package/data/nodes.db
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/../hooks/lib/generate_lookup.py" /tmp/package/data/nodes.db "$SCRIPT_DIR/../hooks/lib/node_lookup_data.json"
echo "Updated node_lookup_data.json"
```

- [ ] **Step 2: Extract generate_lookup.py** from the inline code in Task 1 Step 4 into a reusable script.

- [ ] **Step 3: Run it, verify the JSON is regenerated, commit**

```bash
bash scripts/refresh-node-lookup.sh
git add scripts/refresh-node-lookup.sh hooks/lib/generate_lookup.py hooks/lib/node_lookup_data.json
git commit -m "feat: refresh-node-lookup.sh — regenerate node dictionary from latest n8n-mcp"
```

---

### Task 6: Full-scale node-spec ingestion + validation (orchestrator-only)

Run `sync-nodes.py --full` to ingest all ~1,851 nodes (the 190-unit prototype is already in the bank from testing; this replaces/extends it). Then run the eval harness v1 ground-truth queries to validate.

- [ ] **Step 1: Ensure the `node_spec` strategy exists** on the n8n bank (already added during prototype — verify with GET config)

- [ ] **Step 2: Run full ingestion**

```bash
python3 scripts/sync-nodes.py --full
```

- [ ] **Step 3: Verify via recall probes** — 10 diverse named queries, structured lookup, confirm >95% hit rate

- [ ] **Step 4: Run eval harness v1** (if built by then) to get baseline metrics

- [ ] **Step 5: Record results in the spec, commit**

---

### Task 7: Full-scale workflow ingestion + e2e test (orchestrator-only)

Run `sync-workflows.py --full` to ingest all 28 official workflows. Then test the full design→build recall chain.

- [ ] **Step 1: Run full ingestion**

```bash
python3 scripts/sync-workflows.py --full
```

- [ ] **Step 2: e2e recall test** — query "build a flow that posts to Slack when a webhook fires" and verify:
  - Semantic recall surfaces workflow example + gotcha
  - Node-lookup identifies webhook + slack
  - Structured recall returns webhook spec + slack message.post spec
  - All three layers surface in one merged response

- [ ] **Step 3: Record results, commit**

---

### Task 8: Cleanup throwaway experiment banks

- [ ] **Step 1: Delete** `wf-strategy-test`, `wf-node-test`, `node-op-test`, `node-op-test2`, `node-op-test3` on n8nhindsight (confirm with Dan first per destructive-ops policy)

```bash
for bank in wf-strategy-test wf-node-test node-op-test node-op-test2 node-op-test3; do
  echo "Delete $bank? (y/n)"
done
```

- [ ] **Step 2: Verify the 190 prototype units in the real `n8n` bank are still intact** (they use production tags, stable IDs — kept as intended)

---

## Execution notes

- **Tasks 1–2** are plugin-side (n8n-knowledge repo). Task 1 is pure authoring (subagent can do it). Task 2 modifies existing hooks (subagent can author, orchestrator runs tests).
- **Tasks 3–4** are ingestion-side (n8n-hindsight repo). Subagent writes the script, orchestrator runs it.
- **Tasks 5** is a small utility (either repo works).
- **Tasks 6–8** are orchestrator-only (Bash-heavy: API calls, recall probes, bank operations).
- **Task 1 must complete before Task 2** (Task 2 depends on `node_lookup.py`).
- **Tasks 3 and 4 are independent** of Tasks 1–2 (different repo) and can run in parallel.
- **Tasks 6–7 require Tasks 3–4** (ingestion scripts must exist).
- **Task 8** can run anytime after Tasks 6–7 pass.
