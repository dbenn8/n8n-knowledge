"""Select relevant sections from a mental model based on prompt context.

Mental models are curated bug catalogs organized by ## section headers. When
the full model is too large (>4K), injecting everything overwhelms the model
and it ignores the content. This module extracts only the sections relevant
to what the user is actually building.
"""

import re
import sys

MAX_CHARS = 4000
MAX_SECTIONS = 3

PROMPT_TO_SECTION = {
    r"upload|file|image|photo|pdf|binary|attachment|avatar": "file|upload|binary",
    r"credential|auth|login|connect|api.?key|token|oauth": "credential|auth",
    r"stream|real.?time|sse|chunk|event.?source": "stream",
    r"timeout|slow|latenc|long.?running|performance": "timeout|latenc",
    r"tool.?call|function.?call|structured|json.?schema": "tool|structured",
    r"memory|history|conversation|chat|context.?window": "memory|context|chat",
    r"version|update|upgrade|migration|regression": "version|crash|regression",
    r"expression|template|variable|\{\{": "expression|ui",
    r"batch|loop|split|pagina|iterate|chunk": "batch|loop|split|pagina",
    r"merge|combine|join|enrich": "merge|combine",
    r"webhook|trigger|endpoint|listen": "webhook|trigger|receive",
    r"email|smtp|imap|gmail|outlook": "email|smtp|gmail",
    r"sheet|spreadsheet|row|column|range": "sheet|row|column|range",
    r"query|sql|insert|select|database|table": "query|sql|insert|select",
}


def select_sections(content: str, prompt: str) -> str:
    if len(content) <= MAX_CHARS:
        return content

    parts = re.split(r"(?=^## )", content, flags=re.MULTILINE)
    sections = []
    for p in parts:
        if not p.strip() or not p.startswith("##"):
            continue
        header = p.split("\n")[0].strip().lstrip("#").strip()
        sections.append((header, p))

    if not sections:
        return content[:MAX_CHARS]

    prompt_lower = prompt.lower()
    prompt_words = set(re.findall(r"[a-z]{3,}", prompt_lower))

    scored = []
    for header, body in sections:
        score = 0
        header_words = set(re.findall(r"[a-z]{3,}", header.lower()))
        score += len(prompt_words & header_words) * 10

        body_lower = body.lower()
        for prompt_rx, section_rx in PROMPT_TO_SECTION.items():
            if re.search(prompt_rx, prompt_lower) and re.search(section_rx, body_lower):
                score += 20

        scored.append((score, header, body))

    scored.sort(key=lambda x: -x[0])

    selected = []
    total = 0
    for score, _header, body in scored:
        if score == 0:
            continue
        if len(selected) >= MAX_SECTIONS:
            break
        if total + len(body) > MAX_CHARS:
            remaining = MAX_CHARS - total
            if remaining > 200:
                selected.append(body[:remaining].rstrip() + "\n...(truncated)")
            break
        selected.append(body)
        total += len(body)

    if not selected:
        return sections[0][1][:MAX_CHARS]

    return "\n".join(selected)


if __name__ == "__main__":
    content = sys.stdin.read()
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    print(select_sections(content, prompt))
