#!/bin/bash
# Reinstall the nx CLI tool, preserving optional extras (e.g. [local])
# from the previous installation.
#
# nexus-2fyb: mineru was promoted from extras to a default dep. The
# previous "preserve extras" logic silently propagated empty-extras
# state for any install that didn't start with [mineru] — which was
# every fresh install per README. mineru is now always present;
# only genuinely-optional extras like [local] are receipt-driven.
#
# nexus-q3xrx: `uv tool install --reinstall` rebuilds the venv tree IN
# PLACE. Every live process holding that venv (T2 daemon, storage-service
# supervisor, nx-mcp servers in EVERY Claude session, in-flight `nx index`
# runs) suffers delayed lazy-import failures after the swap: vanished
# certifi cacert path, package metadata reading as version '0.0.0'
# (T2 handshake skew), ModuleNotFoundError for modules that exist on
# disk. Empirically diagnosed 2026-06-11 (95 cacert tracebacks + a
# mid-run manifest-hook death in index.log; contributed to the daemon
# silent-death cluster). So: refuse to swap under live processes.
#
# Usage: scripts/reinstall-tool.sh [source] [--cycle-daemons|--force]
#   source: install source (default: "." for local dev, use "conexus" for PyPI)
#   --cycle-daemons  stop nx-owned daemons first, reinstall, restart them
#   --force          swap anyway (listed processes WILL break; restart them)

set -euo pipefail

SOURCE="."
CYCLE_DAEMONS=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --cycle-daemons) CYCLE_DAEMONS=1 ;;
        --force)         FORCE=1 ;;
        *)               SOURCE="$arg" ;;
    esac
done

VENV_DIR="$(uv tool dir)/conexus"
RECEIPT="${VENV_DIR}/uv-receipt.toml"

live_venv_processes() {
    # Processes whose command line references the tool venv, excluding
    # transient greps. Catches the daemons, MCP servers, and in-flight
    # nx runs started from the installed tool.
    ps ax -o pid=,command= | grep -F "$VENV_DIR" | grep -v grep || true
}

if [[ "$CYCLE_DAEMONS" == "1" ]]; then
    echo "Stopping nx-owned daemons before the venv swap (--cycle-daemons)…"
    nx daemon t2 stop 2>/dev/null || true
    nx daemon service stop 2>/dev/null || true
    sleep 1
fi

LIVE="$(live_venv_processes)"
if [[ -n "$LIVE" && "$FORCE" != "1" ]]; then
    echo "REFUSING to reinstall: live processes hold the conexus venv and a"
    echo "swap underneath them causes delayed import/cacert/version-skew"
    echo "failures (nexus-q3xrx). Holders:"
    echo "$LIVE" | sed 's/^/  /'
    echo ""
    echo "Remedies:"
    echo "  scripts/reinstall-tool.sh --cycle-daemons   # stop daemons, install, restart"
    echo "  (close other Claude sessions' nx-mcp servers, or accept they break)"
    echo "  scripts/reinstall-tool.sh --force           # swap anyway"
    exit 3
elif [[ -n "$LIVE" ]]; then
    echo "WARNING (--force): swapping the venv under live processes — these"
    echo "WILL fail on their next lazy import and must be restarted:"
    echo "$LIVE" | sed 's/^/  /'
fi

EXTRAS=""
if [[ -f "$RECEIPT" ]]; then
    # nexus-2fyb code-review R5-I1: pass the receipt path via an env var
    # rather than shell-interpolating it into the python -c heredoc. The
    # prior `open('$RECEIPT')` form was vulnerable to Python-injection if
    # $RECEIPT ever contained a quote (low real-world risk via uv tool
    # dir, but a clean fix).
    EXTRAS=$(NEXUS_RECEIPT_PATH="$RECEIPT" python3 -c "
import os, re
text = open(os.environ['NEXUS_RECEIPT_PATH']).read()
m = re.search(r'extras\s*=\s*\[([^\]]*)\]', text, re.DOTALL)
if m:
    extras = re.findall(r'\"([^\"]+)\"', m.group(1))
    # 'mineru' is now a default dep — drop it if a stale receipt still lists it
    extras = [e for e in extras if e != 'mineru']
    if extras:
        print(','.join(extras))
" 2>/dev/null || true)
fi

if [[ -n "$EXTRAS" ]]; then
    echo "Preserving extras: [$EXTRAS]"
    uv tool install --reinstall --from "${SOURCE}[${EXTRAS}]" conexus
else
    uv tool install --reinstall "$SOURCE"
fi

nx --version

# Symlink dependency console_scripts (mineru-api, mineru) into ~/.local/bin.
# uv only auto-symlinks the project's own entrypoints (nx, nx-mcp); deps stay
# inside the tool venv. mineru is always present now (nexus-2fyb), so
# unconditionally symlink it if the binaries exist.
TOOL_BIN="$(uv tool dir)/conexus/bin"
LOCAL_BIN="${HOME}/.local/bin"

if [[ -d "$TOOL_BIN" ]]; then
    for cmd in mineru-api mineru; do
        if [[ -f "$TOOL_BIN/$cmd" ]]; then
            ln -sf "$TOOL_BIN/$cmd" "$LOCAL_BIN/$cmd"
            echo "Symlinked: $cmd"
        fi
    done
fi

# nexus-5ldk1: a running T2 daemon froze its code at start and now predates
# this reinstall. Bring it to the freshly-installed version so the reinstall
# is live, not pending a manual `nx daemon t2 stop && ensure-running`.
# ensure-running is version-aware: no-op on a current daemon, graceful cycle
# on a stale one. Best-effort; never fails the reinstall.
if command -v nx >/dev/null 2>&1; then
    nx daemon t2 ensure-running --quiet --timeout=10 2>/dev/null || \
        echo "(note: daemon cycle skipped/failed; run 'nx daemon t2 ensure-running' manually)"
fi

# nexus-q3xrx: restart the storage service when --cycle-daemons stopped it
# (best-effort — boxes without an initialized service stack skip cleanly).
if [[ "$CYCLE_DAEMONS" == "1" ]] && command -v nx >/dev/null 2>&1; then
    nx daemon service start 2>/dev/null || \
        echo "(note: storage service not restarted; run 'nx daemon service start' manually)"
fi
