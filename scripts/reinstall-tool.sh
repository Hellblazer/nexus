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

# ── Downgrade / divergent-source guard (nexus-q3xrx; also nexus-r024j) ──────
# Two live incidents (2026-06-11, 2026-06-12): a reinstall from a STALE
# checkout silently DOWNGRADED the shared installed CLI (nx daemon service
# vanished, stack unrestartable), and a PyPI-source reinstall wiped 31
# unreleased modules while keeping the version string. Refuse a reinstall
# whose source pyproject version is BEHIND the installed nx without --force.
# PyPI-shape SOURCE (no local pyproject): the SECOND incident's exact vector
# — a PyPI reinstall over a dev install wiped 31 unreleased modules while
# keeping the version string. If the CURRENT install came from a directory
# (dev checkout, per the uv receipt) and this invocation would replace it
# with a registry package, refuse without --force.
if [[ ! -f "${SOURCE}/pyproject.toml" && -f "$RECEIPT" && "$FORCE" != "1" ]]; then
    if grep -q 'directory = ' "$RECEIPT" 2>/dev/null; then
        echo "REFUSING: the installed conexus came from a DIRECTORY source (dev"
        echo "  checkout, per ${RECEIPT}), and '${SOURCE}' is a registry package —"
        echo "  a PyPI reinstall over a dev install wipes unreleased modules while"
        echo "  keeping the version string (nexus-q3xrx incident #2, 2026-06-12)."
        echo "  Pass --force to deliberately return to the released package."
        exit 1
    fi
fi

# nexus-zfutt: resolve the "installed" version from the TARGET venv this
# invocation is about to reinstall (${VENV_DIR}/bin/nx, which honors
# whatever $HOME is active — including an isolated sandbox HOME), never from
# a bare `nx` lookup on the ambient $PATH. `tests/e2e/release-sandbox.sh`
# activates a sandbox HOME + prepends its own bin dir to $PATH before
# calling this script; on a fresh (or not-yet-populated) sandbox no `nx`
# exists there yet, so a PATH-based lookup falls through to the REAL global
# install and a lagging develop-branch pyproject version reads as a false
# "downgrade" of an install this reinstall has nothing to do with. A missing
# target-venv nx (nothing installed there yet) correctly skips the
# comparison — there is nothing to downgrade.
NX_BIN="${VENV_DIR}/bin/nx"
if [[ -f "${SOURCE}/pyproject.toml" && -x "$NX_BIN" ]]; then
    SRC_VERSION="$(sed -n "s/^version *= *[\"']\([^\"']*\)[\"']/\1/p" "${SOURCE}/pyproject.toml" | head -1)"
    [[ -n "$SRC_VERSION" ]] || echo "warn: could not parse version from ${SOURCE}/pyproject.toml — downgrade guard inactive"
    INSTALLED_VERSION="$("$NX_BIN" --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p' | head -1)"
    if [[ -n "$SRC_VERSION" && -n "$INSTALLED_VERSION" ]]; then
        NEWEST="$(printf '%s\n%s\n' "$SRC_VERSION" "$INSTALLED_VERSION" | sort -V | tail -1)"
        if [[ "$SRC_VERSION" != "$INSTALLED_VERSION" && "$NEWEST" == "$INSTALLED_VERSION" && "$FORCE" != "1" ]]; then
            echo "REFUSING to reinstall: source checkout is ${SOURCE} at version"
            echo "  ${SRC_VERSION}, but the installed nx is ${INSTALLED_VERSION} — this is a"
            echo "  DOWNGRADE (stale checkout? wrong directory?). Two incidents of this"
            echo "  class silently broke the shared install (nexus-q3xrx)."
            echo "  Pass --force to downgrade deliberately."
            exit 1
        fi
        if [[ "$SRC_VERSION" == "$INSTALLED_VERSION" ]]; then
            echo "WARNING: source ${SOURCE} (branch $(git -C "${SOURCE}" branch --show-current 2>/dev/null || echo '?')) is at ${SRC_VERSION} — the SAME"
            echo "  version as the installed nx (${INSTALLED_VERSION}). Working-tree changes WILL be"
            echo "  picked up, but the version string won't move (release bumps live on"
            echo "  main, not develop — nexus-r024j). To install a released build instead:"
            echo "  scripts/reinstall-tool.sh 'conexus==${INSTALLED_VERSION}'"
        fi
    fi
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
    # PEP 508 (nexus-r024j item b): extras precede a version pin —
    # "conexus==X[local]" is invalid; build "conexus[local]==X".
    if [[ "$SOURCE" == *"=="* && "$SOURCE" != *"/"* ]]; then
        SPEC="${SOURCE%%==*}[${EXTRAS}]==${SOURCE#*==}"
    else
        SPEC="${SOURCE}[${EXTRAS}]"
    fi
    uv tool install --reinstall --from "${SPEC}" conexus
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
