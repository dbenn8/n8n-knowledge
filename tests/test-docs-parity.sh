#!/usr/bin/env bash
set -euo pipefail

# Pins docs/version/path parity so a release or a path migration can't drift the
# user-facing docs out of sync with the code:
#   D1  version parity: README first **vX.Y.Z** == plugin.json version == CHANGELOG top semver
#   D2  canonical-path pin: the runtime debug-log path (derived from runtime_dirs.py,
#       with $HOME collapsed to ~) appears in README at least once
#   D3  zero stale "/tmp/n8n-knowledge-debug.log" references remain in README/PRIVACY

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB_DIR="$REPO/hooks/lib"
README="$REPO/README.md"
PRIVACY="$REPO/PRIVACY.md"
PLUGIN_JSON="$REPO/.claude-plugin/plugin.json"
CHANGELOG="$REPO/CHANGELOG.md"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "=== docs parity tests ==="

# --- D1: version parity across README / plugin.json / CHANGELOG ---
README_VER="$(grep -oE '^\*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*' "$README" | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
PLUGIN_VER="$(python3 -c "import json; print(json.load(open('$PLUGIN_JSON'))['version'])")"
CHANGELOG_VER="$(grep -oE '^## [0-9]+\.[0-9]+\.[0-9]+' "$CHANGELOG" | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"

if [ -n "$README_VER" ] && [ "$README_VER" = "$PLUGIN_VER" ] && [ "$README_VER" = "$CHANGELOG_VER" ]; then
  pass "D1: version parity (README=$README_VER, plugin.json=$PLUGIN_VER, CHANGELOG=$CHANGELOG_VER)"
else
  fail "D1: version mismatch (README='$README_VER', plugin.json='$PLUGIN_VER', CHANGELOG='$CHANGELOG_VER')"
fi

# --- D2: canonical debug-log path (default, $HOME collapsed to ~) appears in README ---
# Derive from the Python twin with both override vars unset so we get the real default.
CANON_PATH="$(env -u N8N_KNOWLEDGE_RUNTIME_DIR -u XDG_CACHE_HOME python3 -c "
import os, sys
sys.path.insert(0, '$LIB_DIR')
import runtime_dirs
p = runtime_dirs.debug_log_path()
home = os.path.expanduser('~')
if p.startswith(home):
    p = '~' + p[len(home):]
print(p)
")"
if [ -n "$CANON_PATH" ] && grep -Fq "$CANON_PATH" "$README"; then
  pass "D2: README pins the canonical debug-log path ($CANON_PATH)"
else
  fail "D2: README missing canonical debug-log path ('$CANON_PATH')"
fi

# --- D3: no stale /tmp/n8n-knowledge-debug.log references in README or PRIVACY ---
STALE="/tmp/n8n-knowledge-debug.log"
if grep -Fq "$STALE" "$README"; then
  fail "D3: README still references stale path $STALE"
elif grep -Fq "$STALE" "$PRIVACY"; then
  fail "D3: PRIVACY.md still references stale path $STALE"
else
  pass "D3: no stale '$STALE' references in README or PRIVACY.md"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
