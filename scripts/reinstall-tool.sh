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
# Usage: scripts/reinstall-tool.sh [source] [--cycle-daemons] [--cycle-mcp] [--force]
#   source: install source (default: "." for local dev, use "conexus" for PyPI)
#   --cycle-daemons  stop nx-owned daemons first, reinstall, restart them
#   --cycle-mcp      kill every Claude-session MCP server holding the venv
#                    (nx-mcp/nx-mcp-catalog processes whose ancestor chain
#                    includes a `claude` process). Matching is by ANCESTRY
#                    ONLY, not session-scoped — this can kill another live
#                    Claude session's MCP servers, not just this session's;
#                    they all reconnect the same way. Reinstalls, then
#                    reminds the operator that EVERY affected Claude session
#                    must run `/mcp` to reconnect on the new venv. Composes
#                    with --cycle-daemons. Never touches non-MCP holders
#                    (in-flight `nx` runs, daemons) — those still refuse or
#                    go through --cycle-daemons (nexus-hrqox).
#   --force          swap anyway (listed processes WILL break; restart them)

set -euo pipefail

SOURCE="."
CYCLE_DAEMONS=0
CYCLE_MCP=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --cycle-daemons) CYCLE_DAEMONS=1 ;;
        --cycle-mcp)     CYCLE_MCP=1 ;;
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

# ── MCP-holder self-awareness (nexus-hrqox) ─────────────────────────────────
# The most common live-venv-processes refusal is a Claude session's own
# nx-mcp/nx-mcp-catalog servers: they spawn from the venv at Claude Code
# session start and never recheck it. Those are stateless stdio children —
# safe to kill, and Claude Code reconnects them on demand via `/mcp` —
# unlike an in-flight `nx` run or a daemon, which must never be silently
# killed here. Classification below is by ANCESTRY ONLY (a `claude` process
# anywhere in the ppid chain) — it does NOT scope to the invoking session,
# so a claude-ancestored holder belonging to ANOTHER live Claude session on
# the box is classified and killable exactly the same as this session's own.
# Classifying holders lets the refusal message (and the opt-in --cycle-mcp
# arm below) tell MCP holders apart from everything else.

# True if a live-holder command names nx-mcp or nx-mcp-catalog exactly
# (basename of the first token — tolerates a full venv-bin path, and never
# matches a lookalike like a hypothetical "nx-mcp-old").
_is_mcp_server_cmd() {
    local first_tok base
    first_tok="${1%% *}"
    base="$(basename "$first_tok" 2>/dev/null || true)"
    [[ "$base" == "nx-mcp" || "$base" == "nx-mcp-catalog" ]]
}

# True if a `claude` process appears in PID's ancestor chain. Walks the ppid
# chain via `ps -o ppid=,comm=` (same tool the rest of this script already
# depends on) so it stays swappable via PATH for tests — no other external
# dependency. Depth-capped against a broken/cyclic ppid chain.
_pid_has_claude_ancestor() {
    local pid="$1" depth=0 line ppid comm
    while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" && $depth -lt 32 ]]; do
        line="$(ps -o ppid=,comm= -p "$pid" 2>/dev/null || true)"
        [[ -n "$line" ]] || return 1
        read -r ppid comm <<< "$line"
        [[ "$(basename "$comm" 2>/dev/null || true)" == "claude" ]] && return 0
        pid="$ppid"
        depth=$((depth + 1))
    done
    return 1
}

# Classify $LIVE's holder lines into MCP_CLAUDE_PIDS (claude-ancestored
# nx-mcp/nx-mcp-catalog — the class --cycle-mcp is allowed to kill) and a
# count of everything else (OTHER_HOLDERS — in-flight nx runs, daemons,
# standalone MCP servers with no claude ancestor). Populates both as globals;
# call only when $LIVE is non-empty.
_classify_live_holders() {
    MCP_CLAUDE_PIDS=()
    OTHER_HOLDERS=0
    local line pid cmd
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        read -r pid cmd <<< "$line"
        if _is_mcp_server_cmd "$cmd" && _pid_has_claude_ancestor "$pid"; then
            MCP_CLAUDE_PIDS+=("$pid")
        else
            OTHER_HOLDERS=$((OTHER_HOLDERS + 1))
        fi
    done <<< "$1"
}

if [[ "$CYCLE_DAEMONS" == "1" ]]; then
    echo "Stopping nx-owned daemons before the venv swap (--cycle-daemons)…"
    # No `nx daemon t2 stop`: the T2 daemon was retired in nexus-i711w Stage 2
    # sub-stage B and the whole `t2` verb group deleted with it.
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

MCP_CLAUDE_PIDS=()
OTHER_HOLDERS=0
if [[ -n "$LIVE" ]]; then
    _classify_live_holders "$LIVE"
fi

# ── --cycle-mcp arm (nexus-hrqox) ────────────────────────────────────────
# Opt-in, composes with --cycle-daemons. Kills ONLY the claude-ancestored
# nx-mcp/nx-mcp-catalog holders classified above; refuses (never partially
# kills) if any non-MCP holder is also present, or if a killed holder
# survives the wait. Never swaps the venv under a surviving holder —
# nexus-q3xrx stays untouched.
if [[ "$CYCLE_MCP" == "1" && -n "$LIVE" && "$FORCE" != "1" ]]; then
    if [[ ${#MCP_CLAUDE_PIDS[@]} -eq 0 || "$OTHER_HOLDERS" != "0" ]]; then
        echo "REFUSING --cycle-mcp: it only kills Claude-session nx-mcp/"
        echo "  nx-mcp-catalog servers (this session's or another live"
        echo "  session's — matching is by ancestry only) — never an"
        echo "  in-flight nx invocation or a daemon. Non-MCP (or no MCP)"
        echo "  holder(s) remain:"
        echo "$LIVE" | sed 's/^/  /'
        echo "  Use --cycle-daemons for daemons, or --force to swap anyway."
        exit 3
    fi
    echo "Killing Claude session MCP server(s) — this session's or another"
    echo "live session's — before the venv swap (--cycle-mcp): ${MCP_CLAUDE_PIDS[*]}"
    # PID-reuse TOCTOU: MCP_CLAUDE_PIDS was classified from a ps SNAPSHOT
    # ($LIVE, captured above); if a PID were reaped and reused for something
    # else before this kill fires, we could in principle signal the wrong
    # process. The survivor recheck immediately below is the accepted
    # backstop for that window — it re-snapshots live_venv_processes() AFTER
    # the kill and refuses on whatever is ACTUALLY still holding the venv,
    # never on what we assumed the PIDs were.
    kill "${MCP_CLAUDE_PIDS[@]}" 2>/dev/null || true
    sleep 0.5
    STILL="$(live_venv_processes)"
    if [[ -n "$STILL" ]]; then
        echo "REFUSING: holder(s) survived --cycle-mcp's kill — refusing to swap"
        echo "  the venv under them (nexus-q3xrx). Survivors:"
        echo "$STILL" | sed 's/^/  /'
        exit 3
    fi
    LIVE=""
    CYCLE_MCP_KILLED=1
fi

if [[ -n "$LIVE" && "$FORCE" != "1" ]]; then
    echo "REFUSING to reinstall: live processes hold the conexus venv and a"
    echo "swap underneath them causes delayed import/cacert/version-skew"
    echo "failures (nexus-q3xrx). Holders:"
    echo "$LIVE" | sed 's/^/  /'
    echo ""
    if [[ ${#MCP_CLAUDE_PIDS[@]} -gt 0 && "$OTHER_HOLDERS" == "0" ]]; then
        # Self-aware refusal (nexus-hrqox): every holder is a Claude-session
        # MCP server from the OLD venv — the sanctioned dance is a kill +
        # re-run + /mcp reconnect, not the generic three-remedy list.
        echo "All holders above are Claude-session MCP servers (nx-mcp /"
        echo "nx-mcp-catalog) spawned from the OLD venv — matched by ancestry"
        echo "only, so this may include another live Claude session's"
        echo "servers, not just this one. PID(s): ${MCP_CLAUDE_PIDS[*]}"
        echo ""
        echo "Sanctioned sequence:"
        echo "  kill ${MCP_CLAUDE_PIDS[*]}"
        echo "  scripts/reinstall-tool.sh   # re-run this script"
        echo "  then in EVERY affected Claude session: /mcp   # reconnects on the NEW venv"
        echo ""
        echo "Or let this script do it: scripts/reinstall-tool.sh --cycle-mcp"
    else
        echo "Remedies:"
        echo "  scripts/reinstall-tool.sh --cycle-daemons   # stop daemons, install, restart"
        if [[ ${#MCP_CLAUDE_PIDS[@]} -gt 0 ]]; then
            echo "  scripts/reinstall-tool.sh --cycle-mcp       # kill Claude session MCP server(s) (this or another live session), install, then /mcp to reconnect"
        fi
        echo "  (close other Claude sessions' nx-mcp servers, or accept they break)"
        echo "  scripts/reinstall-tool.sh --force           # swap anyway"
    fi
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

# nexus-5ldk1 is CLOSED BY DELETION (nexus-i711w Stage 2 sub-stage B): the
# stale-code-after-reinstall problem it describes was the T2 DAEMON freezing
# its code at start, and that daemon no longer exists. The `nx daemon t2
# ensure-running` cycle that lived here died with the verb group — left in
# place it fired its `||` branch on EVERY reinstall, telling the operator to
# run a command that now exits "No such command 't2'". The surviving storage
# service is cycled by the --cycle-daemons block below.

# nexus-q3xrx: restart the storage service when --cycle-daemons stopped it
# (best-effort — boxes without an initialized service stack skip cleanly).
if [[ "$CYCLE_DAEMONS" == "1" ]] && command -v nx >/dev/null 2>&1; then
    nx daemon service start 2>/dev/null || \
        echo "(note: storage service not restarted; run 'nx daemon service start' manually)"
fi

# nexus-hrqox: --cycle-mcp killed every claude-ancestored MCP holder it
# found — this session's or another live session's, since matching is by
# ancestry only, not session-scoped. They don't come back on their own,
# unlike the storage service above.
if [[ "${CYCLE_MCP_KILLED:-0}" == "1" ]]; then
    echo ""
    echo "REMINDER: Claude-session MCP server(s) were killed for the venv"
    echo "  swap (--cycle-mcp) — possibly in another live Claude session, not"
    echo "  just this one. EVERY affected Claude session must run '/mcp' to"
    echo "  reconnect on the NEW venv."
fi
