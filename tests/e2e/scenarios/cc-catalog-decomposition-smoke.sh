#!/usr/bin/env bash
# cc-catalog-decomposition-smoke.sh — drive Claude Code in tmux against
# the local sandbox and exercise the catalog refactor's MCP surface.
#
# Background: PRs #602 + #603 split catalog.py into a 1683-LOC facade
# plus six focused modules (_LinkOps / _DocumentOps / _SyncOps /
# _WriteOps composition + catalog_git / catalog_spans static helpers).
# Unit tests exercise the catalog through pytest fixtures with
# EphemeralClient + NX_T1_ISOLATED=1 + mocked subprocesses.  This
# scenario closes the gap by exercising the SAME refactored surfaces
# under live conditions: a real Claude Code session, a real
# nx-mcp-catalog server, real subagent dispatch, real chroma.
#
# Run after release-sandbox.sh shakedown has set up $HOME/nexus-sandbox
# with an indexed nexus catalog.  Usage:
#
#   tests/e2e/release-sandbox.sh shell
#   # ... in the subshell:
#   tests/e2e/scenarios/cc-catalog-decomposition-smoke.sh
#
# Or invoke this script after manually launching `release-sandbox.sh tmux`.
#
# What it asserts:
#   - Catalog MCP tools (search / show / links / link_query) succeed.
#   - nx_answer routing through query → catalog → T3 doesn't crash.
#   - Auto-link recipe (scratch put + catalog_search + store_put)
#     completes.
#   - No `AttributeError` (would indicate composition-order bug).
#   - No `database is locked` (would indicate atomicity regression).
#   - No `_sync` / `_docs` / `_links` / `_writes` reported as missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
TMUX_SESSION="${TMUX_SESSION:-nexus-sandbox}"

source "$REPO_ROOT/tests/e2e/lib.sh"

# ─── Pre-flight ──────────────────────────────────────────────────────────────

if [[ ! -d "$SANDBOX/.config/nexus/catalog" ]]; then
    echo "ERROR: $SANDBOX/.config/nexus/catalog missing." >&2
    echo "Run tests/e2e/release-sandbox.sh shakedown first." >&2
    exit 1
fi

if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "ERROR: tmux session '$TMUX_SESSION' not found." >&2
    echo "Run tests/e2e/release-sandbox.sh tmux first." >&2
    exit 1
fi

# Per-prompt result tracking
declare -i PASS_COUNT=0
declare -i FAIL_COUNT=0

_assert() {
    local name="$1"
    shift
    if "$@"; then
        echo "  [PASS] $name"
        PASS_COUNT+=1
    else
        echo "  [FAIL] $name"
        FAIL_COUNT+=1
    fi
}

_no_crash_markers() {
    local snapshot
    snapshot=$(capture -200)
    ! echo "$snapshot" | grep -qiE "AttributeError|database is locked|has no attribute '_sync'|has no attribute '_docs'|has no attribute '_links'|has no attribute '_writes'|RuntimeError"
}

# ─── Stress sequence ─────────────────────────────────────────────────────────

echo ""
echo "=== Catalog decomposition CC sandbox shakedown ==="
echo ""

# Probe 1 — catalog search via MCP (exercises _DocumentOps.find +
# the registry-keyed routing).
echo "[1/6] Catalog search via MCP (search by author)..."
claude_prompt "Use the mcp__plugin_conexus_nexus-catalog__search tool with text='nexus' and limit=5. Just call the tool and tell me the count of results."
claude_wait 90
_assert "catalog search returns" _no_crash_markers

# Probe 2 — catalog show on a known tumbler (exercises
# _DocumentOps.resolve via the MCP show tool).
echo "[2/6] Catalog show on a tumbler..."
claude_prompt "Use mcp__plugin_conexus_nexus-catalog__list with limit=3 to find a tumbler, then call mcp__plugin_conexus_nexus-catalog__show on the first result. Report whether show returned a document or an error."
claude_wait 90
_assert "catalog show resolves" _no_crash_markers

# Probe 3 — catalog links readback (exercises _LinkOps.links_to /
# links_from / link_query through the MCP links tool).
echo "[3/6] Catalog links readback..."
claude_prompt "Use mcp__plugin_conexus_nexus-catalog__links with the tumbler from the previous step, direction='out', depth=1. Report the number of edges returned."
claude_wait 90
_assert "catalog links readback" _no_crash_markers

# Probe 4 — nx_answer routing (exercises catalog → T3 query path,
# touches _DocumentOps + _SyncOps for the rebuild bootstrap).
echo "[4/6] nx_answer composed retrieval..."
claude_prompt "Use mcp__plugin_conexus_nexus__nx_answer to ask: 'How does the catalog handle event-sourced writes?' Limit to 1 source. Just report whether the call succeeded."
claude_wait 180
_assert "nx_answer composed retrieval" _no_crash_markers

# Probe 5 — auto-link recipe (scratch put + catalog_search + store_put,
# exercises _LinkOps.link / link_if_absent through the post-store hook).
echo "[5/6] Auto-link recipe (scratch + catalog_search + store_put)..."
claude_prompt "Run: scratch put 'auto-link probe content' --tags 'link-context'. Then store_put with project='cc-smoke', title='auto-link-test', content='probe content for auto-linker', tags='link-context'. Report whether either call raised."
claude_wait 120
_assert "auto-link recipe" _no_crash_markers

# Probe 6 — nx catalog doctor (exercises link_audit, _ensure_consistent,
# the full catalog read surface).
echo "[6/6] nx catalog doctor (full read surface)..."
claude_prompt "Run the bash command: nx catalog doctor 2>&1 | tail -20. Report the last few lines of output."
claude_wait 120
_assert "nx catalog doctor" _no_crash_markers

# ─── Summary ─────────────────────────────────────────────────────────────────

echo ""
echo "=== CC sandbox shakedown summary ==="
echo "  Passed: $PASS_COUNT"
echo "  Failed: $FAIL_COUNT"
echo ""

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "FAIL — capture last 100 lines of pane:"
    capture -100 | sed 's/^/    | /'
    exit 1
fi

echo "PASS — catalog decomposition surface holds under live MCP traffic"
exit 0
