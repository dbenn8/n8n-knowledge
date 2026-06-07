"""Node name identification for n8n knowledge lookups.

Maps service/node display names mentioned in user prompts to their canonical
n8n node type identifiers (e.g. "nodes-base.slack"). The lookup dictionary
is loaded from node_lookup_data.json, which is generated from the n8n node
catalog.
"""
import json
import os
import re
from difflib import get_close_matches

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


def identify_nodes(prompt):
    lookup = _load()
    action, trigger = _variant_maps(lookup)
    pl = prompt.lower()
    has_trigger = bool(_TRIGGER_WORDS & set(re.findall(r"[a-z]+", pl)))

    hits = []
    # Pass 1: exact word-boundary matches (fast, precise)
    for name in sorted(lookup, key=len, reverse=True):
        if len(name) < 2:
            continue
        if name in _COMMON_WORDS:
            node_ctx = r"\b" + re.escape(name) + r"\s+node\b"
            if not re.search(node_ctx, pl):
                continue
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, pl):
            nt = lookup[name]
            suffix = nt.split(".")[-1].lower()
            base = re.sub(r"trigger$", "", suffix)
            if not has_trigger and base in action and "trigger" in suffix:
                nt = action[base]
            elif has_trigger and "trigger" not in suffix and suffix in trigger:
                nt = trigger[suffix]
            hits.append((name, nt))
            pl = re.sub(pattern, "", pl, count=1)

    # Pass 2: fuzzy fallback for unmatched words (catches typos)
    if not hits:
        words = re.findall(r"\b[a-z]{3,}\b", pl)
        for word in words:
            if word in _TRIGGER_WORDS:
                continue
            matched_key = _fuzzy_lookup(word, lookup)
            if matched_key:
                nt = lookup[matched_key]
                suffix = nt.split(".")[-1].lower()
                base = re.sub(r"trigger$", "", suffix)
                if not has_trigger and base in action and "trigger" in suffix:
                    nt = action[base]
                elif has_trigger and "trigger" not in suffix and suffix in trigger:
                    nt = trigger[suffix]
                hits.append((matched_key, nt))
                break

    return hits
