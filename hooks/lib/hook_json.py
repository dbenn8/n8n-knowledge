#!/usr/bin/env python3
"""
Shared hook-JSON helper for the n8n-knowledge hooks.

Centralizes the two JSON shapes that were previously duplicated as inline
`python3 -c`/heredoc blocks across auto-recall.sh and validate-workflow.sh:

  1. The "emit hookSpecificOutput" wrapper:
       {"hookSpecificOutput": {"hookEventName": <event>, "additionalContext": <text>}}
  2. The "merge additionalContext with cap" behavior used when injecting recall
     results, DB schema, and validator guidance — capped at MAX_CTX characters so
     the context always stays inline (never spills to a skipped file).

Subcommands (each reads the prior hook JSON from stdin where relevant):

  emit <event> <text>
      Print a fresh hookSpecificOutput wrapper. <text> is taken verbatim from
      argv and JSON-escaped. (validate-workflow lines 59/140/255, auto-recall
      recall-skipped guidance path.)

  cap
      Read a hookSpecificOutput JSON on stdin; if additionalContext exceeds
      MAX_CTX, truncate to MAX_CTX + TRUNC_SUFFIX. The existing hookEventName is
      preserved as-is. Empty stdin -> no output (exit 0). Unparseable stdin ->
      echo the raw input back. (auto-recall recall-only cap, lines 130-144.)

  prepend-cap <event>
      Read prior hookSpecificOutput JSON on stdin; prepend HOOK_JSON_EXTRA env
      with a blank-line separator, strip, then cap at MAX_CTX (+TRUNC_SUFFIX).
      Force hookEventName=<event>. Empty stdin -> fresh wrapper with just the
      extra. (auto-recall DB-inject merge, lines 151-172.)

  prepend <event>
      Like prepend-cap but with NO cap, and the separator is suppressed when the
      existing context is empty. Force hookEventName=<event>. Empty stdin ->
      fresh wrapper with just the extra. (auto-recall validator-guidance merge,
      lines 179-196.)

Env:
  HOOK_JSON_EXTRA  text to prepend (used by prepend / prepend-cap)
"""
import json
import os
import sys

# Single source of truth for the inline-context character budget. Kept here so
# every hook caps additionalContext at the same value (was a magic 10000 repeated
# inline in auto-recall.sh).
MAX_CTX = int(os.environ.get("NK_MAX_CTX", "10000"))

# Exact suffix appended when truncation occurs — must stay byte-identical to the
# pre-refactor inline blocks so downstream output is unchanged.
TRUNC_SUFFIX = "\n... (recall truncated to stay inline)"


def _wrapper(event, text):
    """Build the hookSpecificOutput envelope dict."""
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _cap(text):
    """Truncate text to MAX_CTX + suffix if it overflows; otherwise return as-is."""
    if len(text) > MAX_CTX:
        return text[:MAX_CTX] + TRUNC_SUFFIX
    return text


def cmd_emit(event, text):
    print(json.dumps(_wrapper(event, text)))


def cmd_cap():
    raw = sys.stdin.read().strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
        ctx = data.get("hookSpecificOutput", {}).get("additionalContext", "")
        if len(ctx) > MAX_CTX:
            ctx = _cap(ctx)
            data.setdefault("hookSpecificOutput", {})["additionalContext"] = ctx
        print(json.dumps(data))
    except Exception:
        print(raw)


def cmd_prepend_cap(event):
    extra = os.environ.get("HOOK_JSON_EXTRA", "")
    raw = sys.stdin.read().strip()
    if raw:
        try:
            data = json.loads(raw)
            existing = data.get("hookSpecificOutput", {}).get("additionalContext", "")
            combined = (extra + "\n\n" + existing).strip()
            combined = _cap(combined)
            data.setdefault("hookSpecificOutput", {})["additionalContext"] = combined
            data["hookSpecificOutput"]["hookEventName"] = event
            print(json.dumps(data))
        except Exception:
            print(raw)
    else:
        print(json.dumps(_wrapper(event, extra)))


def cmd_prepend(event):
    extra = os.environ.get("HOOK_JSON_EXTRA", "")
    raw = sys.stdin.read().strip()
    if raw:
        try:
            data = json.loads(raw)
            existing = data.get("hookSpecificOutput", {}).get("additionalContext", "")
            combined = (extra + ("\n\n" + existing if existing else "")).strip()
            data.setdefault("hookSpecificOutput", {})["additionalContext"] = combined
            data["hookSpecificOutput"]["hookEventName"] = event
            print(json.dumps(data))
        except Exception:
            print(raw)
    else:
        print(json.dumps(_wrapper(event, extra)))


def main(argv):
    if not argv:
        sys.exit(2)
    sub = argv[0]
    if sub == "emit":
        # emit <event> <text>
        cmd_emit(argv[1], argv[2])
    elif sub == "cap":
        cmd_cap()
    elif sub == "prepend-cap":
        cmd_prepend_cap(argv[1])
    elif sub == "prepend":
        cmd_prepend(argv[1])
    else:
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
