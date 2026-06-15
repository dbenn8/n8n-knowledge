"""Node name identification for n8n knowledge lookups.

Maps service/node display names mentioned in user prompts to their canonical
n8n node type identifiers (e.g. "nodes-base.slack"). The lookup dictionary
is loaded from node_lookup_data.json, which is generated from the n8n node
catalog (nodes.db) — that DB is the single source of truth for the *data*.

============================================================================
CANONICAL SOURCE — DO NOT FORK.
============================================================================
This file is THE canonical node-detection logic. It runs in TWO roles that
form one contract:
  • RECALL side (plugin, this repo): detects nodes in the USER'S PROMPT
    (auto-recall.sh, detect-n8n.sh) — picks which node tags to QUERY.
  • INGEST side (n8n-hindsight sync-github.py, planned): detects nodes in an
    ISSUE/PR — picks which node tags to WRITE.
If these two drift, recall SILENTLY MISSES what ingest tagged (a node written
as `node:openai` but queried as `node:open-ai` returns nothing). The bug is
invisible — no error, just degraded recall — so it must be prevented mechanically,
not by discipline.

Why a copy must exist at all: the plugin ships to end users via the Claude Code
marketplace and runs on whatever Python they have (no guaranteed `pip install`),
so it MUST vendor this file + node_lookup_data.json rather than depend on a
package. n8n-hindsight (server-side) therefore keeps a byte-identical VENDORED
COPY of this logic.

RULE: change this file here first, then re-vendor the identical file to
n8n-hindsight. Never edit the n8n-hindsight copy directly. When wiring the
INGEST side (Task 1), add a hash-pin parity guard for this file mirrored in
both repos — same pattern as tests/test-hash-parity.sh — so any drift between
the two copies fails CI loudly. Never "fix" a failing parity or regression test
by weakening it: a red test means a copy drifted, not that the test is wrong.
The data file (node_lookup_data.json) is intentionally NOT hash-pinned — it is
regenerated from nodes.db (the single source for the data) and changes whenever
the node catalog updates.
"""
import json
import os
import re
from difflib import get_close_matches, SequenceMatcher

_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA = None
_KEYS = None


def _load():
    """Load and cache the node lookup dictionary from JSON."""
    global _DATA, _KEYS
    if _DATA is None:
        with open(os.path.join(_DIR, "node_lookup_data.json")) as f:
            _DATA = json.load(f)
        _KEYS = list(_DATA.keys())
    return _DATA


_TRIGGER_WORDS = {
    "trigger", "listen", "watch", "fire", "event",
    "poll", "subscribe", "detect", "monitor",
    # Event-phrasing words: "when a <service> row is added/created/...".
    # These signal that the user wants the service's *trigger* node, not its
    # action node, so they upgrade an action match to its trigger variant.
    # NOTE: bare "new" is deliberately excluded -- it is a ubiquitous
    # adjective ("create a new issue", "add a new field") that collides with
    # action phrasing and would wrongly upgrade action nodes to triggers.
    "added", "created", "inserted",
    "updated", "received", "submitted",
}


# Bare single-word keys that are far too generic to count as a node mention.
# These English words appear in nearly every plugin prompt ("n8n", "workflow"),
# so matching them injects the meta-node / workflowTrigger schema as noise and
# steals scarce schema slots. The meta-node and workflowTrigger remain
# detectable through their unambiguous multi-word keys (e.g. "n8n trigger",
# "n8n node" context, "workflow trigger"), which are distinct dictionary keys
# and are NOT demoted here.
_DEMOTED_BARE_TOKENS = {
    "n8n",
    "workflow",
    # Rare community nodes whose single-word names collide with common English,
    # so the bare word over-matches (e.g. ingest-tagging issue titles):
    #   "runn"  = n8n-nodes-runn-dotsandarrows.runn — the -ing stemmer turns
    #             "running" -> "runn" and hits it.
    #   "level" = @levelrmm/n8n-nodes-level.level — "top-level" -> "level".
    # Demote so they only resolve from an explicit multi-word reference, never a
    # stray English word. (Broader community-node/English collisions: task #84.)
    "runn",
    "level",
}


def _variant_maps(lookup):
    """Build action and trigger reverse maps from the lookup dictionary.

    action: base suffix → non-trigger node type (e.g. "slack" → "nodes-base.slack")
    trigger: base suffix → trigger node type (e.g. "gmail" → "nodes-base.gmailTrigger")
    """
    action = {}
    trigger = {}
    for name, nt in lookup.items():
        suffix = nt.split(".")[-1].lower()
        if "trigger" in suffix:
            base = re.sub(r"trigger$", "", suffix)
            if base and (base not in trigger or nt.startswith("nodes-base.")):
                trigger[base] = nt
        else:
            if suffix not in action or nt.startswith("nodes-base."):
                action[suffix] = nt
    return action, trigger


_COMMON_WORDS = {
    "the", "node", "set", "use", "how", "can", "get", "add", "run",
    "send", "make", "call", "put", "integration", "configure", "setup",
    "create", "update", "delete", "list", "connect", "build", "start",
    "stop", "check", "test", "flow", "data", "item", "items", "field",
    "value", "input", "output", "error", "issue", "help", "want", "need",
    "what", "whats", "when", "where", "which", "that", "this", "with",
    "from", "into", "handle", "recommended", "best", "way",
}


def _fuzzy_lookup(word, lookup, cutoff=0.85):
    """Find a close dictionary match for a misspelled word.

    Only matches against single-word dictionary keys to avoid
    false positives from partial multi-word entries."""
    if len(word) < 4 or word in _COMMON_WORDS:
        return None
    single_word_keys = [k for k in _KEYS if " " not in k and len(k) >= 4]
    matches = get_close_matches(word, single_word_keys, n=1, cutoff=cutoff)
    if matches:
        return matches[0]
    return None


def _similarity(a, b):
    """Return a 0-1 similarity ratio between two strings (SequenceMatcher)."""
    return SequenceMatcher(None, a, b).ratio()


# Proximity window for trigger-intent scoping (in word tokens).
#
# A service detection is upgraded to its *trigger* variant (or kept as a
# trigger node rather than being demoted to its action variant) only when a
# trigger word occurs LOCALLY -- i.e. within the same clause as that service's
# matched span AND within this many word tokens of it. This keeps the
# trigger-intent signal attached to the service the event phrase actually
# refers to, instead of flipping EVERY detected service globally.
#
# Window sized to 6 so that the longest in-clause fixture phrasing still
# resolves -- e.g. "trigger the workflow on a schedule" has 5 tokens between
# "trigger" and "schedule" with no punctuation between them. The clause
# boundary (any of , . ; : ? !) is the hard cutoff: in
# "... row is added, send a slack message" the comma after the event phrase
# stops "added" from reaching "slack", so slack stays an action node.
_TRIGGER_PROXIMITY_TOKENS = 6
_CLAUSE_BOUNDARY = set(",.;:?!")


def _trigger_word_near(pl, start, end):
    """Return True if a trigger word sits within the proximity window of the
    [start, end) character span, without crossing a clause boundary.

    `pl` is the (lowercased) prompt text. The span is the matched service
    name. We tokenize the surrounding text and scan outward from the match,
    stopping in each direction at the first clause-boundary punctuation.
    """
    # Tokens before the match (left context), nearest-first.
    left = pl[:start]
    # Cut the left context at the last clause boundary so we stay in-clause.
    for i in range(len(left) - 1, -1, -1):
        if left[i] in _CLAUSE_BOUNDARY:
            left = left[i + 1:]
            break
    left_tokens = re.findall(r"[a-z]+", left)
    for tok in left_tokens[::-1][:_TRIGGER_PROXIMITY_TOKENS]:
        if tok in _TRIGGER_WORDS:
            return True

    # Tokens after the match (right context), nearest-first.
    right = pl[end:]
    for i, ch in enumerate(right):
        if ch in _CLAUSE_BOUNDARY:
            right = right[:i]
            break
    right_tokens = re.findall(r"[a-z]+", right)
    for tok in right_tokens[:_TRIGGER_PROXIMITY_TOKENS]:
        if tok in _TRIGGER_WORDS:
            return True

    return False


def identify_nodes(prompt):
    lookup = _load()
    action, trigger = _variant_maps(lookup)
    pl = prompt.lower()

    hits = []
    # Pass 1: exact word-boundary matches (fast, precise)
    for name in sorted(lookup, key=len, reverse=True):
        if len(name) < 2:
            continue
        if name in _COMMON_WORDS or name in _DEMOTED_BARE_TOKENS:
            # Overly-generic bare tokens (e.g. "n8n", "workflow") only count as
            # a node mention when explicitly qualified as "<token> node".
            # Unambiguous multi-word keys (e.g. "workflow trigger") are
            # distinct names and bypass this gate entirely.
            node_ctx = r"\b" + re.escape(name) + r"\s+node\b"
            if not re.search(node_ctx, pl):
                continue
        # Single-word keys also match common verb forms (merges, filtered, etc.)
        if " " not in name:
            pattern = r"\b" + re.escape(name) + r"(?:es|ed|ing|s|d)?\b"
        else:
            pattern = r"\b" + re.escape(name) + r"\b"
        m = re.search(pattern, pl)
        if m:
            nt = lookup[name]
            suffix = nt.split(".")[-1].lower()
            base = re.sub(r"trigger$", "", suffix)
            # Trigger intent is scoped LOCALLY to this match's span, not
            # globally across the whole prompt.
            local_trigger = _trigger_word_near(pl, m.start(), m.end())
            if not local_trigger and base in action and "trigger" in suffix:
                nt = action[base]
            elif local_trigger and "trigger" not in suffix and suffix in trigger:
                nt = trigger[suffix]
            hits.append((name, nt))
            pl = re.sub(pattern, "", pl, count=1)

    # Pass 2: fuzzy fallback for unmatched words (catches typos + verb forms)
    if not hits:
        words = re.findall(r"\b[a-z]{3,}\b", pl)
        for w in words:
            if w in _COMMON_WORDS or w in _DEMOTED_BARE_TOKENS:
                continue
            # Strip common verb suffixes to match node names that are bare nouns
            # (e.g. "merges" -> "merge", "splits" -> "split", "filtered" -> "filter").
            stems = [w]
            if w.endswith("es") and len(w) > 4:
                stems.append(w[:-2])
            if w.endswith("s") and len(w) > 3:
                stems.append(w[:-1])
            if w.endswith("ed") and len(w) > 4:
                stems.append(w[:-2])
            if w.endswith("ing") and len(w) > 5:
                stems.append(w[:-3])
            for stem in stems:
                # A stem that lands on a demoted/common bare token (e.g. the
                # plural "workflows" -> "workflow" -> workflowTrigger) must NOT
                # match — the original-word guard above only saw "workflows".
                if stem in _COMMON_WORDS or stem in _DEMOTED_BARE_TOKENS:
                    continue
                if stem in lookup:
                    nt = lookup[stem]
                    hits.append((stem, nt))
                    break
            if hits:
                break
            # Original fuzzy similarity check (for typos, min 4 chars)
            if len(w) >= 4:
                best, best_score = None, 0.0
                for name in lookup:
                    if len(name) < 4 or " " in name:
                        continue
                    if name in _COMMON_WORDS or name in _DEMOTED_BARE_TOKENS:
                        continue
                    ratio = _similarity(w, name)
                    if ratio > best_score:
                        best, best_score = name, ratio
                if best_score >= 0.85:
                    hits.append((best, lookup[best]))
                    break

    return hits


# --- Node-type -> community tag mapping (THE tag<->query contract) ----------
# This is the single canonical implementation. The bash recall path
# (structured_recall.sh:_node_to_community_tag) routes through service_to_tag,
# and the ingest side (n8n-hindsight sync-github.py) uses community_tag, so both
# produce the IDENTICAL `node:<tag>` string. If they ever diverge, recall
# silently misses what ingest tagged. Do not fork this mapping.

# Community tags that diverge from the mechanical camelCase->kebab-case form.
_COMMUNITY_TAG_MAP = {
    "open-ai": "openai",
    "lm-chat-open-ai": "openai",
    "lm-open-ai": "openai",
    "open-ai-assistant": "openai",
    "http-request": "http-request",
    "split-in-batches": "split-in-batches",
    "execute-workflow": "execute-workflow",
    "schedule-trigger": "schedule-trigger",
    "form-trigger": "form-trigger",
}


def service_to_tag(service):
    """Map a bare service name (already stripped of node prefix + Trigger/Tool
    suffix) to its community tag: camelCase -> kebab-case, then known overrides.
    Mirrors structured_recall.sh:_node_to_community_tag exactly."""
    s = (service or "").strip()
    tag = re.sub(r"([a-z])([A-Z])", r"\1-\2", s).lower()
    return _COMMUNITY_TAG_MAP.get(tag, tag)


def community_tag(node_type):
    """Map a full node type (e.g. 'nodes-base.openAi',
    '@n8n/n8n-nodes-langchain.openAi') to its community tag, mirroring how
    do_gotcha_recall derives it: take the segment after the last '.', strip a
    trailing 'Trigger' or 'Tool', then service_to_tag()."""
    service = (node_type or "").rsplit(".", 1)[-1]
    service = re.sub(r"Trigger$", "", service)
    service = re.sub(r"Tool$", "", service)
    return service_to_tag(service)
