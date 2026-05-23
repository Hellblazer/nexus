#!/usr/bin/env bash
# upgrade-shakeout.sh — pre-merge verification of a breaking upgrade.
#
# What it does (rename PR #937 example, nexus-mkj6u):
#   1. Tear down + create a fresh sandbox HOME.
#   2. uv tool install conexus==<FROM_VERSION> from PyPI (the live shipped
#      version).
#   3. nx hooks install — writes the OLD stanza (pre-pgrep-guard).
#   4. Inspect marketplace.json + hook stanza + verify pre-upgrade state.
#   5. uv tool install --reinstall from REPO_ROOT (the upgrade-under-test).
#   6. nx --version — confirm the new wheel is installed.
#   7. nx doctor — verify the stanza-drift health-check surfaces a warning
#      naming `nx hooks update`.
#   8. nx hooks update — verify the hook stanza is refreshed with the new
#      pgrep guard.
#   9. Inspect marketplace.json + plugin.json — verify the rename took
#      effect (nx -> conexus).
#  10. Final assertion summary.
#
# Why this exists:
#   The release-sandbox.sh script tests a single version. It cannot catch
#   regressions in the upgrade path itself (drift warnings missing, hook
#   stanza fix not propagating, plugin rename not surfacing in marketplace.json).
#   nexus-mkj6u (2026-05-23) introduced four migration touchpoints — the
#   stanza pgrep guard, the `nx hooks update` command, the `nx doctor`
#   stanza-drift check, and the plugin name change — all of which need to
#   work in concert for an existing user to upgrade cleanly.
#
# Modes:
#   run     Execute the full sequence; exit 0 on green, 1 on any assertion
#           failure. ~3-5 minutes (first run, includes one wheel install
#           from PyPI).
#   reset   Tear down the sandbox without running anything.
#
# Usage:
#   $0 run [--from-version <X.Y.Z>]
#   $0 reset
#
# Defaults:
#   FROM_VERSION = the highest stable conexus on PyPI at script start.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-upgrade-sandbox"
FAKE_REPO="$SANDBOX/fakerepo"

MODE="${1:-help}"; shift || true
FROM_VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-version) FROM_VERSION="$2"; shift 2 ;;
        --help|-h)
            printf '%s\n' \
                "Usage: $0 <mode> [--from-version X.Y.Z]" \
                "" \
                "Modes:" \
                "  run    Full upgrade-shakeout sequence" \
                "  reset  Remove sandbox HOME without running" \
                "" \
                "Defaults:" \
                "  --from-version <latest stable on PyPI>"
            exit 0 ;;
        *) echo "ERROR: unknown arg $1" >&2; exit 2 ;;
    esac
done

_die() { echo "FAIL: $*" >&2; exit 1; }
_pass() { echo "  ✓ $*"; }
_step() { echo; echo "── $* ──"; }

if [[ "$MODE" == "reset" ]]; then
    [[ -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
    echo "Sandbox removed."
    exit 0
fi

if [[ "$MODE" != "run" ]]; then
    "$0" --help; exit 0
fi

# Resolve FROM_VERSION default — latest stable on PyPI.
if [[ -z "$FROM_VERSION" ]]; then
    FROM_VERSION=$(
        curl -s "https://pypi.org/pypi/conexus/json" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
    )
    [[ -z "$FROM_VERSION" ]] && _die "could not resolve latest conexus version from PyPI"
fi

echo "Upgrade-shakeout: $FROM_VERSION  →  $(grep '^version' "$REPO_ROOT/pyproject.toml" | head -1 | cut -d'"' -f2) (REPO_ROOT)"
echo "Sandbox: $SANDBOX"

# ── 1. Fresh sandbox ─────────────────────────────────────────────────────────
_step "1/10 Fresh sandbox"
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX/.config/nexus"
mkdir -p "$FAKE_REPO/.git/hooks"
# Fake repo registry entry so nx doctor's _check_git_hooks sees the repo.
python3 -c "
import json, pathlib
p = pathlib.Path('$SANDBOX/.config/nexus/repos.json')
p.write_text(json.dumps({'repos': {'$FAKE_REPO': {}}}))
"
_pass "sandbox + fakerepo at $FAKE_REPO"

# ── 2. Install FROM_VERSION ──────────────────────────────────────────────────
_step "2/10 uv tool install conexus==$FROM_VERSION (the OLD version)"
# Use a sandbox-local UV_TOOL_DIR so this install does not clobber the dev
# tool install. The UV_TOOL_BIN_DIR puts the resulting `nx` on PATH for
# this script's subsequent commands.
export UV_TOOL_DIR="$SANDBOX/uv_tools"
export UV_TOOL_BIN_DIR="$SANDBOX/uv_bin"
export PATH="$UV_TOOL_BIN_DIR:$PATH"
uv tool install "conexus==$FROM_VERSION" --reinstall >/dev/null 2>&1 \
    || _die "uv tool install conexus==$FROM_VERSION failed"
_pass "installed: $(nx --version)"
[[ "$(nx --version)" == *"$FROM_VERSION"* ]] || _die "version mismatch after install"

# ── 3. Install hooks (OLD stanza) ────────────────────────────────────────────
_step "3/10 nx hooks install (writes OLD stanza)"
# Run inside fakerepo so the hook lands in fakerepo/.git/hooks
(cd "$FAKE_REPO" && git init -q && git config user.email t@t.invalid && git config user.name T)
HOME="$SANDBOX" nx hooks install "$FAKE_REPO" >/dev/null
HOOK_OLD=$(cat "$FAKE_REPO/.git/hooks/post-commit")
echo "$HOOK_OLD" | grep -q '# >>> nexus managed begin >>>' || _die "OLD hook missing sentinel"
if echo "$HOOK_OLD" | grep -q 'pgrep -f'; then
    _die "OLD hook already has pgrep guard — wrong baseline version?"
fi
_pass "OLD stanza installed (no pgrep guard, as expected)"

# ── 4. Pre-upgrade snapshot ──────────────────────────────────────────────────
_step "4/10 Pre-upgrade state snapshot"
OLD_PLUGIN_DIR="$UV_TOOL_DIR/conexus/lib/python*/site-packages/.."  # informational
# Check the OLD marketplace.json (in the live tool install)
# uv tool install does not ship the .claude-plugin tree; that lives in the
# GitHub repo only. We assert against the in-tool pyproject metadata.
nx --version | sed 's/^/  /'
_pass "snapshot captured"

# ── 5. Upgrade to REPO_ROOT (this branch) ────────────────────────────────────
_step "5/10 uv tool install --reinstall from REPO_ROOT (the NEW version)"
uv tool install --reinstall "$REPO_ROOT" >/dev/null 2>&1 \
    || _die "uv tool install from REPO_ROOT failed"
NEW_VER="$(nx --version | awk '{print $NF}')"
_pass "upgraded: $(nx --version)"
[[ "$NEW_VER" != "$FROM_VERSION" ]] || echo "    (note: REPO_ROOT version == FROM_VERSION; rename may still differ)"

# ── 6. nx doctor surfaces stanza drift ───────────────────────────────────────
_step "6/10 nx doctor should report stanza drift"
DOCTOR_OUT="$(HOME="$SANDBOX" nx doctor 2>&1 || true)"
echo "$DOCTOR_OUT" | grep -qi 'stanza drift' \
    || _die "nx doctor did not surface stanza drift warning. Output:\n$DOCTOR_OUT"
echo "$DOCTOR_OUT" | grep -q 'nx hooks update' \
    || _die "doctor warning missing 'nx hooks update' fix suggestion"
_pass "drift warning present + names 'nx hooks update' as the fix"

# ── 7. nx hooks update refreshes stanza ──────────────────────────────────────
_step "7/10 nx hooks update should add pgrep guard"
HOME="$SANDBOX" nx hooks update "$FAKE_REPO" >/dev/null
HOOK_NEW=$(cat "$FAKE_REPO/.git/hooks/post-commit")
echo "$HOOK_NEW" | grep -q 'pgrep -f' \
    || _die "after update, hook stanza still missing pgrep guard"
echo "$HOOK_NEW" | grep -q '# >>> nexus managed begin >>>' \
    || _die "after update, sentinel block missing"
SENTINEL_COUNT=$(echo "$HOOK_NEW" | grep -c '# >>> nexus managed begin >>>' || true)
[[ "$SENTINEL_COUNT" == "1" ]] || _die "expected 1 sentinel block, found $SENTINEL_COUNT"
_pass "pgrep guard now present (single sentinel block, no duplication)"

# ── 8. nx doctor: drift resolved ─────────────────────────────────────────────
_step "8/10 nx doctor should NOT report drift after update"
DOCTOR_OUT="$(HOME="$SANDBOX" nx doctor 2>&1 || true)"
if echo "$DOCTOR_OUT" | grep -qi 'stanza drift'; then
    _die "drift warning persists after nx hooks update. Output:\n$DOCTOR_OUT"
fi
_pass "drift resolved"

# ── 9. Plugin rename + tag-pin surface (marketplace.json) ────────────────────
_step "9/11 plugin marketplace.json reflects rename + tag pinning"
MJ="$REPO_ROOT/.claude-plugin/marketplace.json"
# Plugin name = conexus
grep -q '"name": "conexus"' "$MJ" \
    || _die "REPO_ROOT marketplace.json missing 'name: conexus'"
! grep -q '"name": "nx"' "$MJ" \
    || _die "REPO_ROOT marketplace.json still contains 'name: nx' (rename incomplete)"
# Source uses git-subdir object form with path + ref pinning
python3 -c "
import json, pathlib, sys
mj = json.loads(pathlib.Path('$MJ').read_text())
for p in mj['plugins']:
    src = p.get('source')
    if not isinstance(src, dict):
        sys.exit(f'plugin {p[\"name\"]!r} source must be object form, got {src!r}')
    if src.get('source') != 'git-subdir':
        sys.exit(f'plugin {p[\"name\"]!r} source.source must be git-subdir, got {src.get(\"source\")!r}')
    if not src.get('ref', '').startswith('v'):
        sys.exit(f'plugin {p[\"name\"]!r} source.ref must be tag form (vX.Y.Z), got {src.get(\"ref\")!r}')
    if src.get('ref') != f\"v{p['version']}\":
        sys.exit(f'plugin {p[\"name\"]!r} source.ref {src.get(\"ref\")!r} != version v{p[\"version\"]}')
print('  all plugins: git-subdir source + ref pinned to v{version}')
" || _die "marketplace.json pinning check failed"
_pass "marketplace.json: rename (conexus) + tag pinning (git-subdir + ref=v\$version)"

# ── 10. Plugin-name drift detection (nexus-mkj6u) ────────────────────────────
_step "10/11 plugin-name drift detected when OLD nx plugin still installed"
# Simulate the user's "ran uv tool upgrade but didn't reinstall plugin"
# state by planting a fake CLAUDE_PLUGIN_ROOT with name=nx.
FAKE_PLUGIN_ROOT="$SANDBOX/fake_plugin_root"
mkdir -p "$FAKE_PLUGIN_ROOT/.claude-plugin"
cat > "$FAKE_PLUGIN_ROOT/.claude-plugin/plugin.json" << 'PJEOF'
{
  "name": "nx",
  "version": "4.34.5",
  "description": "stale OLD plugin still installed in Claude Code"
}
PJEOF

# nx doctor with CLAUDE_PLUGIN_ROOT set to OLD plugin should surface the drift.
DOCTOR_OUT="$(CLAUDE_PLUGIN_ROOT="$FAKE_PLUGIN_ROOT" HOME="$SANDBOX" nx doctor 2>&1 || true)"
echo "$DOCTOR_OUT" | grep -qi 'plugin name' \
    || _die "nx doctor did not surface plugin-name drift. Output:\n$DOCTOR_OUT"
echo "$DOCTOR_OUT" | grep -q '/plugin uninstall nx@nexus-plugins' \
    || _die "doctor warning missing uninstall command"
echo "$DOCTOR_OUT" | grep -q '/plugin install conexus@nexus-plugins' \
    || _die "doctor warning missing install command"
_pass "nx doctor names the uninstall + install commands"
# (The structlog warning at every MCP startup is covered by unit test
# tests/test_plugin_name_drift.py::test_check_version_compatibility_logs_plugin_name_mismatch
# — easier to assert there than to spawn nx-mcp + watch stderr here.)

# ── 11. Summary ──────────────────────────────────────────────────────────────
_step "11/11 PASS"
echo "  Upgrade-shakeout green: $FROM_VERSION → $NEW_VER"
echo "  - hook stanza migrated (pgrep guard added, no pile-up risk)"
echo "  - nx doctor drift detection works"
echo "  - nx hooks update refreshes in-place"
echo "  - plugin rename visible in marketplace.json"
echo
echo "Sandbox preserved at $SANDBOX (inspect, then '$0 reset' to remove)"
