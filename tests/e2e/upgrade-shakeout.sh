#!/usr/bin/env bash
# upgrade-shakeout.sh — pre-merge verification of a breaking upgrade.
#
# What it does (rename PR #937 example, nexus-mkj6u):
#   1. Tear down + create a fresh sandbox HOME.
#   2. uv tool install conexus==<FROM_VERSION> from PyPI (the live shipped
#      version).
#   3. nx hooks install — writes the baseline stanza (may or may not predate
#      the pgrep guard, depending on FROM_VERSION).
#   4. Inspect marketplace.json + hook stanza + verify pre-upgrade state.
#   5. uv tool install --reinstall from REPO_ROOT (the upgrade-under-test).
#   6. nx --version — confirm the new wheel is installed.
#   7. nx doctor — capture whether the stanza-drift health-check fires, then
#      run `nx hooks update` and cross-check that doctor's drift claim agrees
#      with whether the stanza bytes actually changed (nexus-a3nqp). This
#      makes the test runnable from any baseline: a pre-guard baseline
#      exercises the drift→reconcile path, the latest stable exercises the
#      clean-no-op path, and a doctor false-positive/negative fails the run.
#   8. nx hooks update — verify the hook stanza is refreshed / idempotent.
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
#                  Runnable from any baseline; pass --from-version 4.34.6 to
#                  exercise the pre-pgrep-guard drift→reconcile path explicitly.

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

# ── 3. Install hooks (baseline stanza) ───────────────────────────────────────
_step "3/12 nx hooks install (writes the baseline stanza)"
# Run inside fakerepo so the hook lands in fakerepo/.git/hooks
(cd "$FAKE_REPO" && git init -q && git config user.email t@t.invalid && git config user.name T)
HOME="$SANDBOX" nx hooks install "$FAKE_REPO" >/dev/null
HOOK_OLD=$(cat "$FAKE_REPO/.git/hooks/post-commit")
echo "$HOOK_OLD" | grep -q '# >>> nexus managed begin >>>' || _die "baseline hook missing sentinel"
# The pgrep guard shipped in 5.0.1; whether the baseline already carries it
# depends on FROM_VERSION. Do NOT presume a pre-guard baseline — the script
# detects stanza drift at runtime (step 7) so it stays runnable from any
# baseline, including the latest stable. (nexus-a3nqp)
if echo "$HOOK_OLD" | grep -q 'pgrep -f'; then
    _pass "baseline stanza installed (already has pgrep guard — expecting a clean upgrade)"
else
    _pass "baseline stanza installed (pre-pgrep-guard — expecting drift on upgrade)"
fi

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

# ── 6. nx doctor drift report (captured; cross-checked in step 7) ─────────────
_step "6/12 nx doctor stanza-drift report (captured for cross-check)"
DOCTOR_OUT="$(HOME="$SANDBOX" nx doctor 2>&1 || true)"
if echo "$DOCTOR_OUT" | grep -qi 'stanza drift'; then
    DRIFT_REPORTED=1
    echo "$DOCTOR_OUT" | grep -q 'nx hooks update' \
        || _die "doctor reported drift but omitted the 'nx hooks update' fix suggestion"
    _pass "doctor reports stanza drift + names 'nx hooks update' as the fix"
else
    DRIFT_REPORTED=0
    _pass "doctor reports no stanza drift"
fi

# ── 7. nx hooks update + drift cross-check ───────────────────────────────────
_step "7/12 nx hooks update refreshes the stanza in place"
HOME="$SANDBOX" nx hooks update "$FAKE_REPO" >/dev/null
HOOK_NEW=$(cat "$FAKE_REPO/.git/hooks/post-commit")
echo "$HOOK_NEW" | grep -q '# >>> nexus managed begin >>>' \
    || _die "after update, sentinel block missing"
SENTINEL_COUNT=$(echo "$HOOK_NEW" | grep -c '# >>> nexus managed begin >>>' || true)
[[ "$SENTINEL_COUNT" == "1" ]] || _die "expected 1 sentinel block, found $SENTINEL_COUNT"

# Cross-check the two INDEPENDENT drift signals: what nx doctor claimed
# (DRIFT_REPORTED, step 6) must agree with whether the stanza bytes actually
# changed on update (STANZA_CHANGED). This catches both doctor false-negatives
# (claims clean, stanza changed) and false-positives (claims drift, no change)
# without presuming any particular baseline version. (nexus-a3nqp)
if [[ "$HOOK_NEW" != "$HOOK_OLD" ]]; then STANZA_CHANGED=1; else STANZA_CHANGED=0; fi
[[ "$STANZA_CHANGED" == "$DRIFT_REPORTED" ]] || _die \
    "drift signal mismatch: nx doctor DRIFT_REPORTED=$DRIFT_REPORTED but actual STANZA_CHANGED=$STANZA_CHANGED"

if [[ "$STANZA_CHANGED" == "1" ]]; then
    # When the baseline predated the pgrep guard, the refreshed stanza must
    # now carry it — the concrete 5.0.1 migration this script was born to guard.
    if ! echo "$HOOK_OLD" | grep -q 'pgrep -f'; then
        echo "$HOOK_NEW" | grep -q 'pgrep -f' \
            || _die "stanza changed but pgrep guard still absent after update"
    fi
    _pass "stanza drift detected + reconciled (doctor and byte-diff agree)"
else
    _pass "no stanza drift (update is a clean no-op; doctor and byte-diff agree)"
fi

# Idempotency: a second update must not change the stanza further.
HOME="$SANDBOX" nx hooks update "$FAKE_REPO" >/dev/null
HOOK_NEW2=$(cat "$FAKE_REPO/.git/hooks/post-commit")
[[ "$HOOK_NEW2" == "$HOOK_NEW" ]] || _die "nx hooks update is not idempotent (second run changed the stanza)"
_pass "nx hooks update is idempotent"

# ── 8. nx doctor: drift resolved ─────────────────────────────────────────────
_step "8/12 nx doctor should NOT report drift after update"
DOCTOR_OUT="$(HOME="$SANDBOX" nx doctor 2>&1 || true)"
if echo "$DOCTOR_OUT" | grep -qi 'stanza drift'; then
    _die "drift warning persists after nx hooks update. Output:\n$DOCTOR_OUT"
fi
_pass "drift resolved"

# ── 9. Plugin rename + tag-pin surface (marketplace.json) ────────────────────
_step "9/12 plugin marketplace.json reflects rename + tag pinning"
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
_step "10/12 plugin-name drift detected when OLD nx plugin still installed"
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
echo "$DOCTOR_OUT" | grep -q '/plugin install conexus@nexus-plugins' \
    || _die "doctor warning missing /plugin install hint"
echo "$DOCTOR_OUT" | grep -q '/reload-plugins' \
    || _die "doctor warning missing /reload-plugins hint"
_pass "nx doctor names both /plugin install and /reload-plugins migration commands"
# (The structlog warning at every MCP startup is covered by unit test
# tests/test_plugin_name_drift.py::test_check_version_compatibility_logs_plugin_name_mismatch
# — easier to assert there than to spawn nx-mcp + watch stderr here.)

# ── 11. .mcpb production bundle packs from REPO_ROOT ─────────────────────────
_step "11/12 .mcpb bundle packs cleanly from REPO_ROOT/mcpb"
if [ -f "$REPO_ROOT/mcpb/manifest.json" ]; then
    cd "$REPO_ROOT/mcpb"
    rm -f conexus.mcpb
    npx -y @anthropic-ai/mcpb@latest pack . conexus.mcpb >/dev/null 2>&1 \
        || _die "mcpb pack failed in $REPO_ROOT/mcpb"
    BUNDLE_SIZE=$(stat -f%z conexus.mcpb 2>/dev/null || stat -c%s conexus.mcpb 2>/dev/null)
    rm -f conexus.mcpb
    cd - >/dev/null
    if [ -z "$BUNDLE_SIZE" ] || [ "$BUNDLE_SIZE" -gt 100000 ]; then
        _die "conexus.mcpb is $BUNDLE_SIZE bytes (expected <100 KB); check .mcpbignore"
    fi
    _pass "mcpb pack produces a $BUNDLE_SIZE-byte bundle (well under 100 KB)"
else
    _pass "skipped (mcpb/ not present on this branch)"
fi

# ── 12. Summary ──────────────────────────────────────────────────────────────
_step "12/12 PASS"
echo "  Upgrade-shakeout green: $FROM_VERSION → $NEW_VER"
if [[ "$STANZA_CHANGED" == "1" ]]; then
    echo "  - hook stanza drift detected + reconciled (doctor + byte-diff agree)"
else
    echo "  - hook stanza unchanged across upgrade (clean no-op; doctor + byte-diff agree)"
fi
echo "  - nx doctor drift detection works (cross-checked against actual diff)"
echo "  - nx hooks update refreshes in-place + is idempotent"
echo "  - plugin rename visible in marketplace.json"
echo
echo "Sandbox preserved at $SANDBOX (inspect, then '$0 reset' to remove)"
