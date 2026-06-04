"""Extract a <500-token recall query from tool content, anchored on the first
keyword not yet covered this session, so each tool call surfaces new topics."""
import re

BREAKS = "\n.?!"


def _occurrences(content, keywords):
    low = content.lower()
    hits = []
    for kw in keywords:
        for m in re.finditer(r"\b" + re.escape(kw.lower()) + r"\b", low):
            hits.append((m.start(), kw.lower()))
    hits.sort()
    return hits


def _last_break_before(content, offset):
    start = 0
    for i in range(offset - 1, -1, -1):
        if content[i] in BREAKS:
            start = i + 1
            break
    # trim leading whitespace
    while start < offset and content[start] in " \t\n":
        start += 1
    return start


def window_query(content, keywords, covered, char_budget=1600):
    """Returns (query, signature_list, more_fresh_after).
    covered: set of keywords whose topic is still active (not stale)."""
    content = content or ""
    hits = _occurrences(content, keywords)
    fresh_hits = [(off, kw) for off, kw in hits if kw not in covered]
    if not fresh_hits:
        return "", [], False

    first_off = fresh_hits[0][0]
    start = _last_break_before(content, first_off)
    end = start + char_budget
    query = content[start:end].strip()

    sig = sorted({kw for off, kw in fresh_hits if start <= off < end})
    more_fresh_after = any(off >= end for off, kw in fresh_hits)
    return query, sig, more_fresh_after
