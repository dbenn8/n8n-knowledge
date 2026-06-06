"""Node name identification for n8n knowledge lookups.

Maps service/node display names mentioned in user prompts to their canonical
n8n node type identifiers (e.g. "nodes-base.slack"). The lookup dictionary
is loaded from node_lookup_data.json, which is generated from the n8n node
catalog.
"""
import json
import os
import re

_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA = None


def _load():
    """Load and cache the node lookup dictionary from JSON."""
    global _DATA
    if _DATA is None:
        with open(os.path.join(_DIR, "node_lookup_data.json")) as f:
            _DATA = json.load(f)
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


def identify_nodes(prompt):
    lookup = _load()
    action, trigger = _variant_maps(lookup)
    pl = prompt.lower()
    has_trigger = bool(_TRIGGER_WORDS & set(re.findall(r"[a-z]+", pl)))

    hits = []
    for name in sorted(lookup, key=len, reverse=True):
        if len(name) < 2:
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

    return hits
