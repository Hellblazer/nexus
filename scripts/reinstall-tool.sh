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
# Usage: scripts/reinstall-tool.sh [source] [--no-cycle] [--force]
#        scripts/reinstall-tool.sh [source] [--cycle-daemons] [--cycle-mcp] [--force]
#   source: install source (default: "." for local dev, use "conexus" for PyPI)
#
# DEFAULT BEHAVIOR (nexus-otnvr): the canonical post-release scenario — a
# live Claude session with its own nx-mcp/nx-mcp-catalog servers, possibly
# alongside a live aspect-worker, MinerU server, or the storage service,
# all holding the OLD venv — is handled AUTOMATICALLY, no flags needed.
# Hal's no-flag-ladder directive outranks any precedent that would leave
# ONE class of daemon behind a flag: every class this script can safely
# cycle, it cycles, with its own per-class stop/restart choreography
# (nexus-103v2, substantive-critic CRITICAL 2026-08-08 — the original cut
# of this rework left the storage service in OTHER_HOLDERS, unmet-goal for
# the exact scenario the bead documents):
#   (a) Claude-session MCP servers (nx-mcp/nx-mcp-catalog processes whose
#       ancestor chain includes a `claude` process — matching is by
#       ANCESTRY ONLY, not session-scoped, so this can cycle another live
#       Claude session's servers too, not just this one's) are killed —
#       stateless stdio children, safe, Claude Code reconnects on `/mcp`.
#   (b) aspect-worker is stopped (a fresh host reclaims any in_progress row
#       after its stale timeout) and, if it was running STANDALONE (not
#       spawned by an MCP server — see the hosted/standalone note below),
#       RESTARTED after the swap with the same args it had. An MCP-HOSTED
#       aspect-worker is not restarted here — it respawns via the same
#       enqueue-hook mechanism once its parent MCP server reconnects.
#   (c) MinerU is stopped (`nx mineru stop`) and restarted after the swap
#       (`nx mineru start`) — on-demand spawn policy would eventually
#       respawn it anyway; this just avoids the next PDF operation paying
#       that latency.
#   (d) the storage service is stopped (`nx daemon service stop`) and
#       restarted after the swap (`nx daemon service start`) — the EXACT
#       choreography that used to live behind --cycle-daemons, now the
#       default whenever the service is a live holder.
#   (e) anything else (an in-flight `nx` invocation, or an unrecognized
#       holder) still REFUSES, exactly as before — the refusal names the
#       exact single next command to run, not a flag menu.
# Every kill is pid-recycle-safe: immediately before signaling, the pid's
# CURRENT command is re-checked against the SAME classification it was
# given at snapshot time (mirrors nexus.upgrade_finish.restart_stale's
# pre-kill re-check, "Review 38b7db3d High-3" — a pid that no longer
# matches is skipped with a warning, never killed).
# A successful swap that cycled MCP servers prints exactly one next
# action: run `/mcp` in every affected Claude session to reconnect.
#
#   --no-cycle       opt OUT of the automatic dance above: refuse (exit 3)
#                     on ANY live holder, cycling nothing — today's
#                     pre-nexus-otnvr default, kept for scripted/cautious
#                     callers that want deterministic refuse-only behavior.
#   --cycle-daemons  accepted for backward compatibility; now a NO-OP
#                     alias — the storage service (and every other daemon
#                     class) is cycled by default regardless of this flag.
#                     Prints a deprecation note.
#   --cycle-mcp      accepted for backward compatibility; now a NO-OP alias
#                     — Claude-session MCP servers are cycled by default
#                     regardless of this flag. Prints a deprecation note.
#   --force          swap anyway (listed processes WILL break; restart them)

set -euo pipefail

SOURCE="."
CYCLE_DAEMONS=0
CYCLE_MCP=0
FORCE=0
NO_CYCLE=0
for arg in "$@"; do
    case "$arg" in
        --cycle-daemons) CYCLE_DAEMONS=1 ;;
        --cycle-mcp)     CYCLE_MCP=1 ;;
        --no-cycle)      NO_CYCLE=1 ;;
        --force)         FORCE=1 ;;
        *)               SOURCE="$arg" ;;
    esac
done

if [[ "$CYCLE_MCP" == "1" || "$CYCLE_DAEMONS" == "1" ]]; then
    echo "NOTE: --cycle-mcp / --cycle-daemons are no longer required —"
    echo "  Claude-session MCP servers and every known daemon (aspect-worker,"
    echo "  mineru, the storage service) are cycled by DEFAULT now"
    echo "  (nexus-otnvr). These flags are accepted for backward"
    echo "  compatibility and no longer change behavior on their own. Pass"
    echo "  --no-cycle for the old refuse-only default."
fi

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
# unlike an in-flight `nx` run, which must never be silently killed here.
# Classification below is by ANCESTRY ONLY (a `claude` process anywhere in
# the ppid chain) — it does NOT scope to the invoking session, so a
# claude-ancestored holder belonging to ANOTHER live Claude session on the
# box is classified and killable exactly the same as this session's own.
# Classifying holders lets the refusal message (and the default-dance
# cycling below — nexus-otnvr) tell MCP holders apart from everything else.

# True if a live-holder command names nx-mcp or nx-mcp-catalog (basename of
# the first OR second whitespace-separated token — nexus-e1m2v). `uv tool
# install` writes console-script shims whose shebang line points DIRECTLY at
# the venv's own python binary (e.g. `#!/…/conexus/bin/python3`), not
# `/usr/bin/env`. On exec, the kernel rewrites the process's argv to
# [python_interpreter, script_path, ...original_args] — `ps ax -o command=`
# reflects that rewritten argv, so the script's own name (nx-mcp /
# nx-mcp-catalog) lands in the SECOND token, never the first, for every
# nx-mcp process spawned this way (i.e. every canonical uv-tool-installed
# one — proven live 2026-08-08, reproduced here with real shebang-exec'd
# processes in tests/scripts/test_reinstall_tool_classifier_real_processes.py).
# Checking only the first token (the pre-fix behavior) matched a hypothetical
# direct-exec shape but silently missed the shape that actually fires in
# production, misclassifying every live nx-mcp/nx-mcp-catalog holder as a
# non-MCP one and making --cycle-mcp inert in its primary scenario.
_is_mcp_server_cmd() {
    local cmd="$1" first second tok base
    read -r first second _ <<< "$cmd"
    for tok in "$first" "$second"; do
        [[ -n "$tok" ]] || continue
        base="$(basename "$tok" 2>/dev/null || true)"
        [[ "$base" == "nx-mcp" || "$base" == "nx-mcp-catalog" ]] && return 0
    done
    return 1
}

# Generic ancestor walk: true if some ancestor of PID — walking the ppid
# chain via `ps -o ppid=,command=` — has a first-token basename matching one
# of the acceptable names in "$@" (from $2 onward). `command=`, not `comm=`:
# `command=` (the reconstructed full argv) is NEVER truncated on any
# platform, and while `comm=` was originally suspected to truncate at
# MAXCOMLEN on macOS for a long argv[0], THAT premise did not hold on this
# box (substantive-critic 2026-08-08 falsified it with a real 93-char-argv0
# repro — `ps -o comm=` did not truncate here). `command=` remains the
# right choice regardless: Linux's `ps -o comm=` (backed by
# `/proc/[pid]/comm`, kernel TASK_COMM_LEN=16, i.e. 15 visible chars) DOES
# truncate a long argv[0] — a real, well-documented, stable kernel ABI
# limit on the platform this unit suite's CI actually runs the `tests/
# scripts/` job on (ubuntu-latest) — so `command=` is strictly never worse
# and closes a real (if unverified-on-macOS) gap on the platform that
# matters for CI. Same `ps` tool the rest of this script already depends
# on, so it stays swappable via PATH for tests — no other external
# dependency. Depth-capped against a broken/cyclic ppid chain.
_pid_has_ancestor_named() {
    local pid="$1"; shift
    local depth=0 line ppid rest first_tok base want
    while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" && $depth -lt 32 ]]; do
        line="$(ps -o ppid=,command= -p "$pid" 2>/dev/null || true)"
        [[ -n "$line" ]] || return 1
        read -r ppid rest <<< "$line"
        first_tok="${rest%% *}"
        base="$(basename "$first_tok" 2>/dev/null || true)"
        for want in "$@"; do
            [[ "$base" == "$want" ]] && return 0
        done
        pid="$ppid"
        depth=$((depth + 1))
    done
    return 1
}

_pid_has_claude_ancestor() { _pid_has_ancestor_named "$1" claude; }

# nexus-103v2 item 2 (substantive-critic Significant-1): aspect-worker's
# RDR-173 credential model spawns it as a CHILD of whatever process called
# the enqueue hook — often an nx-mcp server handling a `store_put`. Whether
# a live aspect-worker holder is "MCP-hosted" (an nx-mcp/nx-mcp-catalog
# ancestor — will respawn on its own once that server reconnects via
# `/mcp`) or "standalone" (no such ancestor, typically ppid 1/launchd after
# detachment — nothing else will ever restart it) decides whether the
# default dance restarts it explicitly after the swap.
_pid_has_mcp_ancestor() { _pid_has_ancestor_named "$1" nx-mcp nx-mcp-catalog; }

# True if a live-holder command names a known, safely-cyclable nx daemon —
# echoes its kind ("aspect-worker" | "mineru" | "service") and returns 0, or
# returns 1 for anything else. aspect-worker and service are matched on
# their CONTIGUOUS CLI-verb shape ("daemon aspect-worker start" / "daemon
# service start"), not a bare substring (nexus-103v2 code-review
# Important-2: a bare "*aspect-worker*" could coincidentally match an
# unrelated `nx` invocation over a path/branch/file literally containing
# that text, e.g. `nx index repo ~/proj/aspect-worker-notes`, and get
# silently SIGTERM'd). mineru keeps a plain substring (its primary action
# is `nx mineru stop`, a real functional verb that resolves its own PID via
# `~/.config/nexus/mineru.pid` rather than trusting this match — see the
# default-dance cycling below); the match here only decides classification
# and the rare kill-fallback. These patterns appear literally in the full
# command line regardless of which argv token the shebang-rewritten
# interpreter occupies (nexus-e1m2v). Mirrors
# `nexus.upgrade_finish._classify`'s "aspect-worker" / "mineru" / "service"
# kinds, generalized here to include "service" alongside the two
# `StaleProcess.restartable` already marks safe — that Python precedent
# governs in-place auto-restart during `upgrade_finish` (an ALREADY-BOOTED
# process discovering it is stale), not this reinstall journey (a PLANNED
# swap with a sanctioned stop→install→restart choreography this script
# already owns for the storage service); Hal's no-flag-ladder directive
# (nexus-103v2 CRITICAL) folds that choreography into the default dance too.
_daemon_kind() {
    case "$1" in
        *"daemon aspect-worker start"*) echo "aspect-worker" ;;
        *mineru*)                       echo "mineru" ;;
        *"daemon service start"*)       echo "service" ;;
        *)                              return 1 ;;
    esac
}

# Kill each pid in "$@" ONLY if a FRESH `ps -o command= -p pid` immediately
# before the signal still classifies it as $1's kind ("mcp" |
# "aspect-worker" | "mineru") — the pid-recycle TOCTOU backstop between the
# classify-time snapshot and the kill (nexus-103v2 code-review
# Important-1), mirroring `nexus.upgrade_finish.restart_stale`'s pre-kill
# re-check ("Review 38b7db3d High-3: re-verify the pid still runs OUR
# command immediately before signaling — the same convention as
# t2_daemon's pre-kill re-check"). Re-uses `_is_mcp_server_cmd` /
# `_daemon_kind` — the IDENTICAL predicates classify-time used — so the
# kill-time check can never be looser than the classification it is
# verifying (code-review Important-2's "tighten to the same standard").
# A pid that fails re-verify (gone, or its command no longer matches) is
# reported with a WARNING line and skipped — never killed.
_kill_verified() {
    local kind="$1"; shift
    local pid now_cmd ok
    for pid in "$@"; do
        now_cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
        ok=0
        if [[ -n "$now_cmd" ]]; then
            case "$kind" in
                mcp)
                    if _is_mcp_server_cmd "$now_cmd" && _pid_has_claude_ancestor "$pid"; then
                        ok=1
                    fi
                    ;;
                aspect-worker|mineru)
                    if [[ "$(_daemon_kind "$now_cmd" || true)" == "$kind" ]]; then
                        ok=1
                    fi
                    ;;
            esac
        fi
        if [[ "$ok" == "1" ]]; then
            kill "$pid" 2>/dev/null || true
        else
            echo "  WARNING: pid $pid no longer matches its classified class"
            echo "    (gone or recycled since classification) — skipped, never killed."
        fi
    done
}

# Classify $LIVE's holder lines into MCP_CLAUDE_PIDS (claude-ancestored
# nx-mcp/nx-mcp-catalog), ASPECT_WORKER_PIDS (+ parallel ASPECT_WORKER_CMDS
# full command lines and ASPECT_WORKER_HOSTED 0/1 flags — nexus-103v2 item
# 2), MINERU_PIDS, STORAGE_SERVICE_PIDS (the daemon kinds above), and a
# count of everything else (OTHER_HOLDERS — in-flight nx runs, standalone
# MCP servers with no claude ancestor, anything unrecognized). The first
# four are exactly the classes the default dance (nexus-otnvr) cycles
# automatically; OTHER_HOLDERS always refuses. Populates all as globals;
# call only when $LIVE is non-empty.
_classify_live_holders() {
    MCP_CLAUDE_PIDS=()
    ASPECT_WORKER_PIDS=()
    ASPECT_WORKER_CMDS=()
    ASPECT_WORKER_HOSTED=()
    MINERU_PIDS=()
    STORAGE_SERVICE_PIDS=()
    OTHER_HOLDERS=0
    local line pid cmd kind
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        read -r pid cmd <<< "$line"
        if _is_mcp_server_cmd "$cmd" && _pid_has_claude_ancestor "$pid"; then
            MCP_CLAUDE_PIDS+=("$pid")
            continue
        fi
        kind="$(_daemon_kind "$cmd" || true)"
        case "$kind" in
            aspect-worker)
                ASPECT_WORKER_PIDS+=("$pid")
                ASPECT_WORKER_CMDS+=("$cmd")
                if _pid_has_mcp_ancestor "$pid"; then
                    ASPECT_WORKER_HOSTED+=("1")
                else
                    ASPECT_WORKER_HOSTED+=("0")
                fi
                ;;
            mineru)  MINERU_PIDS+=("$pid") ;;
            service) STORAGE_SERVICE_PIDS+=("$pid") ;;
            *)       OTHER_HOLDERS=$((OTHER_HOLDERS + 1)) ;;
        esac
    done <<< "$1"
}

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
ASPECT_WORKER_PIDS=()
ASPECT_WORKER_CMDS=()
ASPECT_WORKER_HOSTED=()
MINERU_PIDS=()
STORAGE_SERVICE_PIDS=()
OTHER_HOLDERS=0
if [[ -n "$LIVE" ]]; then
    _classify_live_holders "$LIVE"
fi

CYCLED_MCP=0
CYCLED_ASPECT=0
CYCLED_MINERU=0
CYCLED_SERVICE=0
ASPECT_RESTART_LINES=()

# Bounded wait for the default dance's killed/stopped holders to actually
# exit, in seconds. Overridable for tests (NX_REINSTALL_CYCLE_POLL_SECONDS)
# — the real default (5s) is deliberately longer than a bare `sleep 0.5`
# would give: an MCP stdio server dies near-instantly, but a killed
# aspect-worker may be mid `claude -p` extraction and needs real time to
# drain, and `nx daemon service stop` has its own supervisor shutdown work.
_POLL_SECONDS="${NX_REINSTALL_CYCLE_POLL_SECONDS:-5}"

if [[ -n "$LIVE" && "$FORCE" != "1" ]]; then
    if [[ "$NO_CYCLE" == "1" ]]; then
        # ── --no-cycle: today's pre-nexus-otnvr default, verbatim posture ──
        # Deterministic refuse-only behavior for scripted/cautious callers:
        # never kill anything, always refuse on any live holder.
        echo "REFUSING to reinstall: live processes hold the conexus venv and a"
        echo "swap underneath them causes delayed import/cacert/version-skew"
        echo "failures (nexus-q3xrx). Holders:"
        echo "$LIVE" | sed 's/^/  /'
        echo ""
        echo "--no-cycle was passed: refusing without attempting any automatic"
        echo "cycling. Close or stop the holder(s) above and re-run, or drop"
        echo "--no-cycle to let this script cycle Claude-session MCP servers"
        echo "and known daemons (aspect-worker, mineru, the storage service)"
        echo "automatically."
        exit 3
    fi

    # ── Default dance (nexus-otnvr, storage service folded in per nexus-103v2) ──
    # Anything outside the four cyclable classes is never auto-cycled —
    # refuse (never partially act) and name the single next command.
    if [[ "$OTHER_HOLDERS" != "0" ]]; then
        echo "REFUSING to reinstall: live processes hold the conexus venv and a"
        echo "swap underneath them causes delayed import/cacert/version-skew"
        echo "failures (nexus-q3xrx). Holders:"
        echo "$LIVE" | sed 's/^/  /'
        echo ""
        echo "Claude-session MCP servers and known daemons (aspect-worker,"
        echo "mineru, the storage service) are cycled automatically — at"
        echo "least one holder above is none of those (an in-flight nx"
        echo "invocation, or something unrecognized) and is never cycled"
        echo "automatically."
        echo ""
        echo "Stop or close the holder(s) above, then re-run:"
        echo "  scripts/reinstall-tool.sh"
        exit 3
    fi

    if [[ ${#MCP_CLAUDE_PIDS[@]} -gt 0 ]]; then
        echo "Killing Claude session MCP server(s) — this session's or another"
        echo "live session's — before the venv swap: ${MCP_CLAUDE_PIDS[*]}"
        _kill_verified mcp "${MCP_CLAUDE_PIDS[@]}"
        CYCLED_MCP=1
    fi
    if [[ ${#ASPECT_WORKER_PIDS[@]} -gt 0 ]]; then
        echo "Stopping aspect-worker daemon(s) before the venv swap — safe, a"
        echo "fresh host reclaims any in_progress row after its stale timeout:"
        echo "  ${ASPECT_WORKER_PIDS[*]}"
        idx=0
        for _aw_pid in "${ASPECT_WORKER_PIDS[@]}"; do
            _kill_verified aspect-worker "$_aw_pid"
            if [[ "${ASPECT_WORKER_HOSTED[$idx]}" == "1" ]]; then
                ASPECT_RESTART_LINES+=("hosted:${_aw_pid}")
            else
                ASPECT_RESTART_LINES+=("standalone:${ASPECT_WORKER_CMDS[$idx]}")
            fi
            idx=$((idx + 1))
        done
        CYCLED_ASPECT=1
    fi
    if [[ ${#MINERU_PIDS[@]} -gt 0 ]]; then
        echo "Stopping MinerU server before the venv swap — on-demand spawn"
        echo "policy respawns it: ${MINERU_PIDS[*]}"
        nx mineru stop >/dev/null 2>&1 || _kill_verified mineru "${MINERU_PIDS[@]}"
        CYCLED_MINERU=1
    fi
    if [[ ${#STORAGE_SERVICE_PIDS[@]} -gt 0 ]]; then
        echo "Stopping the storage service before the venv swap — the same"
        echo "choreography --cycle-daemons used to gate, now default:"
        echo "  ${STORAGE_SERVICE_PIDS[*]}"
        # No raw kill here (so no TOCTOU re-verify needed): `nx daemon
        # service stop` resolves its own tracked pid via the ServiceRegistry
        # rather than trusting this classification, the same trust model as
        # `nx mineru stop` above.
        nx daemon service stop 2>/dev/null || true
        CYCLED_SERVICE=1
    fi

    if [[ "$CYCLED_MCP" == "1" || "$CYCLED_ASPECT" == "1" || "$CYCLED_MINERU" == "1" || "$CYCLED_SERVICE" == "1" ]]; then
        DEADLINE=$(( $(date +%s) + _POLL_SECONDS ))
        STILL="$(live_venv_processes)"
        while [[ -n "$STILL" && $(date +%s) -lt $DEADLINE ]]; do
            sleep 0.5
            STILL="$(live_venv_processes)"
        done
        if [[ -n "$STILL" ]]; then
            echo "REFUSING: holder(s) survived the cycle attempt — refusing to"
            echo "  swap the venv under them (nexus-q3xrx). Survivors:"
            echo "$STILL" | sed 's/^/  /'
            exit 3
        fi
        LIVE=""
    fi
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
# service is restarted below whenever the default dance stopped it.

# nexus-otnvr / nexus-103v2: restart the storage service when the default
# dance (or the now-deprecated --cycle-daemons alias, which folds into the
# same CYCLED_SERVICE flag) stopped it — best-effort, boxes without an
# initialized service stack skip cleanly.
if [[ "$CYCLED_SERVICE" == "1" ]] && command -v nx >/dev/null 2>&1; then
    nx daemon service start 2>/dev/null || \
        echo "(note: storage service not restarted; run 'nx daemon service start' manually)"
fi

# nexus-otnvr: the default dance stopped MinerU (if it was a live holder) to
# clear the venv for the swap; restart it now on the NEW install, mirroring
# the storage-service restart above (best-effort — MinerU's own on-demand
# spawn policy would eventually respawn it anyway, this just avoids the
# next PDF operation paying that latency).
if [[ "$CYCLED_MINERU" == "1" ]] && command -v nx >/dev/null 2>&1; then
    nx mineru start >/dev/null 2>&1 || true
fi

# nexus-103v2 item 2: aspect-worker restart symmetry with mineru/service
# above. STANDALONE holders (no MCP-server ancestor at classify time) get
# an explicit best-effort restart with their ORIGINAL args, backgrounded
# (the `start` verb blocks in the foreground by design — RDR-173). A
# HOSTED holder (spawned as a child of an MCP server we just killed) is
# deliberately NOT restarted here — the same enqueue-hook mechanism that
# spawned it the first time respawns it once its parent MCP server
# reconnects via /mcp; explicitly restarting it here would double-spawn
# and, credential-context-wise, may not even carry the right `claude -p`
# inheritance this bash script itself has no special claim to.
if command -v nx >/dev/null 2>&1; then
    for _line in "${ASPECT_RESTART_LINES[@]:-}"; do
        [[ -n "$_line" ]] || continue
        case "$_line" in
            hosted:*)
                _pid="${_line#hosted:}"
                echo "aspect-worker (was pid ${_pid}) was MCP-hosted — not"
                echo "  restarted here; it respawns via the enqueue hook once"
                echo "  you run /mcp."
                ;;
            standalone:*)
                _cmd="${_line#standalone:}"
                _args="${_cmd#*daemon aspect-worker start}"
                echo "aspect-worker (was standalone) restarting:"
                echo "  nx daemon aspect-worker start${_args}"
                # $_args is a flag/value sequence captured from the
                # original command line (e.g. "--config-dir X --tenant Y").
                # Split on spaces via read -ra rather than unquoted
                # expansion: bare ${_args} would ALSO undergo pathname/glob
                # expansion (round-2 review repro: "--tenant tenant-*"
                # silently expanded against matching files in cwd). read
                # -ra word-splits WITHOUT globbing.
                IFS=' ' read -ra _arg_arr <<< "$_args"
                nohup nx daemon aspect-worker start "${_arg_arr[@]}" >/dev/null 2>&1 &
                disown 2>/dev/null || true
                ;;
        esac
    done
fi

# nexus-otnvr: the default dance killed every claude-ancestored MCP holder
# it found — this session's or another live session's, since matching is by
# ancestry only, not session-scoped. They don't come back on their own,
# unlike the daemons above — announce the ONE remaining manual action.
if [[ "$CYCLED_MCP" == "1" ]]; then
    echo ""
    echo "MCP servers were cycled — run /mcp in your Claude session(s) to"
    echo "reconnect on the new install."
fi
