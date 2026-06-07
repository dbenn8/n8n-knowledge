#!/usr/bin/env python3
"""Head-to-head eval: n8n-knowledge plugin vs n8n-mcp.

For each ground-truth prompt, runs both tools and compares:
- Node identification accuracy
- Operation match (is the right spec in the returned context?)
- Gotcha coverage (community/GitHub context — plugin-only)
- Context size (tokens)
- Time

Usage:
    python3 scripts/eval/eval.py
    python3 scripts/eval/eval.py --limit 5
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..", "..")
LIB_DIR = os.path.join(REPO_DIR, "hooks", "lib")

sys.path.insert(0, LIB_DIR)
from node_lookup import identify_nodes

HINDSIGHT_URL = "https://n8nhindsight.applikuapp.com"
PORTFOLIO_ENV = os.path.join(os.path.expanduser("~"), "codeNew", "portfolio", ".env")
HINDSIGHT_KEY = ""

def load_api_key():
    global HINDSIGHT_KEY
    if os.path.exists(PORTFOLIO_ENV):
        with open(PORTFOLIO_ENV) as f:
            for line in f:
                if line.startswith("N8N_HINDSIGHT_API_KEY="):
                    HINDSIGHT_KEY = line.split("=", 1)[1].strip()
                    return
    HINDSIGHT_KEY = os.environ.get("N8N_HINDSIGHT_API_KEY", "")


def recall_plugin(query, node_type=None):
    """Simulate the plugin pipeline: semantic recall + structured recall if node detected."""
    t0 = time.time()

    # Semantic recall
    payload = json.dumps({
        "query": query, "budget": "low", "max_tokens": 3000,
        "include": {"source_facts": {}}
    }).encode()
    req = urllib.request.Request(
        f"{HINDSIGHT_URL}/public/recall",
        data=payload, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        semantic = json.loads(resp.read())

    # Structured recall if node detected
    structured = {"results": []}
    if node_type:
        payload2 = json.dumps({
            "query": f"{node_type.split('.')[-1]} node specification",
            "budget": "low", "max_tokens": 3000,
            "tags": ["type:node-spec", f"node:{node_type}"],
            "tags_match": "all",
        }).encode()
        req2 = urllib.request.Request(
            f"{HINDSIGHT_URL}/public/recall",
            data=payload2, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=30) as resp2:
            structured = json.loads(resp2.read())

    elapsed = time.time() - t0

    all_results = structured.get("results", []) + semantic.get("results", [])
    context_text = "\n".join(r.get("text", "") for r in all_results)
    context_size = len(context_text)

    return {
        "results": all_results,
        "context_text": context_text,
        "context_size": context_size,
        "time": elapsed,
        "tool_calls": 0,
    }


def recall_mcp(query):
    """Call n8n-mcp search_nodes tool via subprocess."""
    t0 = time.time()

    # Use npx to call n8n-mcp's search functionality via its DB directly
    try:
        result = subprocess.run(
            ["python3", "-c", f"""
import sqlite3, json, sys
db = '/tmp/package/data/nodes.db'
try:
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    q = '%{query.lower()}%'
    rows = c.execute(
        'SELECT node_type, display_name, description, properties_schema '
        'FROM nodes WHERE LOWER(display_name) LIKE ? OR LOWER(description) LIKE ? '
        'OR LOWER(node_type) LIKE ? LIMIT 10', (q, q, q)
    ).fetchall()
    results = []
    for r in rows:
        schema = r['properties_schema'] or '[]'
        results.append({{
            'node_type': r['node_type'],
            'display_name': r['display_name'],
            'description': r['description'],
            'schema_size': len(schema),
        }})
    print(json.dumps(results))
except Exception as e:
    print(json.dumps([]))
"""],
            capture_output=True, text=True, timeout=10,
        )
        results = json.loads(result.stdout.strip() or "[]")
    except Exception:
        results = []

    elapsed = time.time() - t0

    context_text = "\n".join(
        f"{r['display_name']} ({r['node_type']}): {r['description']}"
        for r in results
    )

    return {
        "results": results,
        "context_text": context_text,
        "context_size": len(context_text),
        "time": elapsed,
        "tool_calls": 1,
    }


def score_plugin(pair, result, node_hits):
    """Score plugin results against ground truth."""
    scores = {}
    expected_node = pair.get("expected_node")
    expected_op = pair.get("expected_op")

    # Node ID hit
    if expected_node is None:
        scores["node_id"] = 1 if not node_hits else 0
    else:
        detected = node_hits[0][1] if node_hits else None
        scores["node_id"] = 1 if detected == expected_node else 0

    # Operation in context
    if expected_op and result["context_text"]:
        scores["op_match"] = 1 if expected_op.lower() in result["context_text"].lower() else 0
    elif expected_op is None:
        scores["op_match"] = 1
    else:
        scores["op_match"] = 0

    # Gotcha coverage (count results from community/github sources)
    gotcha_count = 0
    for r in result.get("results", []):
        tags = r.get("tags", [])
        if any("source:discourse" in t or "source:github" in t for t in tags):
            gotcha_count += 1
    scores["gotcha_count"] = gotcha_count

    return scores


def score_mcp(pair, result):
    """Score n8n-mcp results against ground truth."""
    scores = {}
    expected_node = pair.get("expected_node")
    expected_op = pair.get("expected_op")

    # Node ID hit
    if expected_node is None:
        scores["node_id"] = 1 if not result["results"] else 0
    else:
        found = any(r["node_type"] == expected_node for r in result["results"])
        scores["node_id"] = 1 if found else 0

    # Operation in context (MCP returns node-level, not operation-level)
    if expected_op:
        scores["op_match"] = 1 if expected_op.lower() in result["context_text"].lower() else 0
    elif expected_op is None:
        scores["op_match"] = 1
    else:
        scores["op_match"] = 0

    # Gotcha coverage (MCP has zero community/GitHub data)
    scores["gotcha_count"] = 0

    return scores


def run_eval(limit=None):
    load_api_key()

    gt_path = os.path.join(SCRIPT_DIR, "ground_truth.jsonl")
    with open(gt_path) as f:
        pairs = [json.loads(line) for line in f if line.strip()]

    if limit:
        pairs = pairs[:limit]

    print(f"Running eval: {len(pairs)} prompts\n")
    print(f"{'ID':<25} {'Cat':<12} {'Plugin':>8} {'MCP':>8} {'P-Op':>6} {'M-Op':>6} {'Gotch':>6} {'P-ms':>7} {'M-ms':>7} {'P-ctx':>7} {'M-ctx':>7}")
    print("-" * 115)

    plugin_scores = []
    mcp_scores = []
    plugin_times = []
    mcp_times = []
    plugin_ctx_sizes = []
    mcp_ctx_sizes = []

    for pair in pairs:
        prompt = pair["prompt"]
        pid = pair["id"]
        cat = pair.get("category", "?")

        # Plugin path
        node_hits = identify_nodes(prompt)
        node_type = node_hits[0][1] if node_hits else None
        try:
            p_result = recall_plugin(prompt, node_type)
        except Exception as e:
            p_result = {"results": [], "context_text": "", "context_size": 0, "time": 0, "tool_calls": 0}

        # MCP path
        try:
            m_result = recall_mcp(prompt)
        except Exception:
            m_result = {"results": [], "context_text": "", "context_size": 0, "time": 0, "tool_calls": 1}

        p_scores = score_plugin(pair, p_result, node_hits)
        m_scores = score_mcp(pair, m_result)

        plugin_scores.append(p_scores)
        mcp_scores.append(m_scores)
        plugin_times.append(p_result["time"])
        mcp_times.append(m_result["time"])
        plugin_ctx_sizes.append(p_result["context_size"])
        mcp_ctx_sizes.append(m_result["context_size"])

        p_nid = "Y" if p_scores["node_id"] else "N"
        m_nid = "Y" if m_scores["node_id"] else "N"
        p_op = "Y" if p_scores["op_match"] else "N"
        m_op = "Y" if m_scores["op_match"] else "N"
        p_gotch = p_scores["gotcha_count"]
        p_ms = int(p_result["time"] * 1000)
        m_ms = int(m_result["time"] * 1000)
        p_ctx = p_result["context_size"]
        m_ctx = m_result["context_size"]

        print(f"{pid:<25} {cat:<12} {p_nid:>8} {m_nid:>8} {p_op:>6} {m_op:>6} {p_gotch:>6} {p_ms:>6}ms {m_ms:>6}ms {p_ctx:>7} {m_ctx:>7}")

        time.sleep(0.2)

    # Aggregates
    n = len(pairs)
    p_nid_rate = sum(s["node_id"] for s in plugin_scores) / n * 100
    m_nid_rate = sum(s["node_id"] for s in mcp_scores) / n * 100
    p_op_rate = sum(s["op_match"] for s in plugin_scores) / n * 100
    m_op_rate = sum(s["op_match"] for s in mcp_scores) / n * 100
    p_gotch_total = sum(s["gotcha_count"] for s in plugin_scores)
    p_avg_time = sum(plugin_times) / n * 1000
    m_avg_time = sum(mcp_times) / n * 1000
    p_avg_ctx = sum(plugin_ctx_sizes) / n
    m_avg_ctx = sum(mcp_ctx_sizes) / n

    print("\n" + "=" * 115)
    print(f"\n{'METRIC':<30} {'Plugin':>15} {'n8n-mcp':>15} {'Delta':>15}")
    print("-" * 75)
    print(f"{'Node ID accuracy':<30} {p_nid_rate:>14.0f}% {m_nid_rate:>14.0f}% {p_nid_rate - m_nid_rate:>+14.0f}%")
    print(f"{'Operation in context':<30} {p_op_rate:>14.0f}% {m_op_rate:>14.0f}% {p_op_rate - m_op_rate:>+14.0f}%")
    print(f"{'Gotcha results (total)':<30} {p_gotch_total:>15} {'0':>15} {f'+{p_gotch_total}':>15}")
    print(f"{'Avg time (ms)':<30} {p_avg_time:>14.0f}ms {m_avg_time:>14.0f}ms {p_avg_time - m_avg_time:>+14.0f}ms")
    print(f"{'Avg context size (chars)':<30} {p_avg_ctx:>14.0f} {m_avg_ctx:>14.0f} {p_avg_ctx - m_avg_ctx:>+14.0f}")
    print(f"{'Tool calls per prompt':<30} {'0':>15} {'1':>15} {'-1':>15}")
    print()


if __name__ == "__main__":
    limit = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])
    run_eval(limit)
