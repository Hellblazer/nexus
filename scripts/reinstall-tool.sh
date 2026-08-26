#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Reinstall the nx CLI as a NEW generation, leaving every live holder alone.
# nexus-utpuw.8 (P4). This script satisfies the acceptance criterion in
# nexus-utpuw comment 1: bare `scripts/reinstall-tool.sh`, zero flags, zero
# steps, ALWAYS succeeds under any number of live sessions.
#
# ── WHY THERE IS NO LONGER ANYTHING TO REFUSE ────────────────────────────────
# `uv tool install --reinstall` rebuilt the venv IN PLACE, so every live holder
# (nx-mcp in every Claude session, the storage service, an in-flight `nx index`)
# suffered delayed lazy-import failures after the swap: vanished certifi cacert,
# package metadata reading as version '0.0.0', ModuleNotFoundError for modules
# that are on disk (nexus-q3xrx, diagnosed 2026-06-11 from 95 cacert tracebacks).
# Safety was structurally impossible, so this script grew a refusal, then
# --force to override it, then a whole choreography (nexus-otnvr) that KILLED
# Claude-session MCP servers and stopped and restarted the aspect-worker, MinerU
# and the storage service to clear the tree before swapping it.
#
# None of that is needed now and all of it is gone. An install builds a fresh
# <tools>/gen-<stamp> and atomically repoints <tools>/current. The tree a
# running process resolved at spawn stays byte-identical underneath it, so
# holders are not an obstacle to be cleared -- they are a fact to report. They
# converge on their next spawn. The refusal path, --force, --cycle-daemons,
# --cycle-mcp and --no-cycle are DELETED, not kept as escape hatches: an exit
# status meaning "occupied" smuggles the refusal back in wearing a different hat.
#
# ── WHAT SURVIVES, AND WHY IT IS NOT THE SAME THING ──────────────────────────
# The downgrade and divergent-source guards are a DIFFERENT failure class and
# comment 1 preserves them explicitly. They do not protect live processes from a
# swap; they protect the install from being replaced by something older or from
# a different source. Each keeps its OWN narrow override (F4): a guard you
# cannot deliberately override is not a guard, it is a wall. There is
# deliberately NO single flag that bypasses both -- that is --force wearing a
# new name, and it drifts back into bypassing everything.
#
# Usage:
#   scripts/reinstall-tool.sh [source] [--allow-downgrade]
#                             [--allow-registry-over-dev]
#     source: install source (default "." for local dev, "conexus" for PyPI)
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_INSTALL="$_here/../src/nexus/_install"

# shellcheck source=/dev/null
. "$_INSTALL/layout.sh"
# shellcheck source=/dev/null
. "$_INSTALL/flip.sh"
# shellcheck source=/dev/null
. "$_INSTALL/shims.sh"
# shellcheck source=/dev/null
. "$_INSTALL/census.sh"
# shellcheck source=/dev/null
. "$_INSTALL/gc.sh"

SOURCE="."
ALLOW_DOWNGRADE=0
ALLOW_REGISTRY_OVER_DEV=0

_removed() {
    echo "$1 no longer exists." >&2
    echo "" >&2
    echo "  It existed because an in-place venv swap broke live holders, so the" >&2
    echo "  only choices were to refuse or to break them deliberately. Installs" >&2
    echo "  are side-by-side now: a new generation is built alongside the one" >&2
    echo "  live processes are running from, and nothing is swapped underneath" >&2
    echo "  anybody. There is nothing left to force, cycle, or opt out of." >&2
    echo "" >&2
    echo "  Run it with no flags:  scripts/reinstall-tool.sh" >&2
    exit 64
}

for arg in "$@"; do
    case "$arg" in
        --allow-downgrade)          ALLOW_DOWNGRADE=1 ;;
        --allow-registry-over-dev)  ALLOW_REGISTRY_OVER_DEV=1 ;;
        # Refused BY NAME rather than ignored. A caller passing --force believes
        # it forced something; accepting the flag and quietly doing nothing
        # special turns a removed safety story into a false one.
        --force|--cycle-daemons|--cycle-mcp|--no-cycle) _removed "$arg" ;;
        -*) echo "unknown option: $arg" >&2; exit 64 ;;
        *)  SOURCE="$arg" ;;
    esac
done

TOOLS_DIR="$(nx_tools_dir)"
BIN_DIR="$(nx_bin_dir)"

# THE CALLER'S OBLIGATION (nexus-14u80): install_generation.sh claims its
# directory with a bare `mkdir` -- never `mkdir -p` -- because that is what
# makes claiming a stamp race-free. It therefore cannot create its own parent,
# and on a missing tools root it fails reporting stamp collisions that never
# happened. migrate_legacy.sh:102 carries the same line for the same reason.
mkdir -p "$TOOLS_DIR"

CURRENT_LINK="$(nx_current_link "$TOOLS_DIR")"
CURRENT_GEN=""
if [ -L "$CURRENT_LINK" ]; then
    CURRENT_GEN="$(readlink "$CURRENT_LINK")"
fi

# ── Divergent-source guard (nexus-q3xrx incident #2, 2026-06-12) ─────────────
# A PyPI reinstall over a dev install wiped 31 unreleased modules while keeping
# the version string. The signal used to be a `directory = ` line in uv's
# receipt; it is now source_kind in ours, which is the same fact stated by the
# half of the system that owns it.
#
# The receipt path travels by ENV VAR, never interpolated into the python source
# (nexus-2fyb R5-I1): a path containing a quote would otherwise be executable.
SOURCE_KIND="$(nx_source_kind "$SOURCE")"

if [ -n "$CURRENT_GEN" ] && [ "$SOURCE_KIND" = "registry" ] \
   && [ "$ALLOW_REGISTRY_OVER_DEV" != "1" ]; then
    _receipt="$(nx_receipt_path "$CURRENT_GEN")"
    if [ -f "$_receipt" ]; then
        _kind="$(NX_RECEIPT="$_receipt" python3 -c '
import json, os
try:
    print(json.load(open(os.environ["NX_RECEIPT"])).get("source_kind", ""))
except Exception:
    print("")
' 2>/dev/null || true)"
        if [ "$_kind" = "directory" ]; then
            echo "REFUSING: the installed conexus came from a DIRECTORY source (dev"
            echo "  checkout, per ${_receipt}), and '${SOURCE}' is a registry package —"
            echo "  a PyPI reinstall over a dev install wipes unreleased modules while"
            echo "  keeping the version string (nexus-q3xrx incident #2, 2026-06-12)."
            echo "  Pass --allow-registry-over-dev to deliberately return to the"
            echo "  released package."
            exit 1
        fi
    fi
fi

# ── Downgrade / stale-checkout guard (nexus-q3xrx, nexus-r024j) ──────────────
# nexus-zfutt, PRESERVED VERBATIM IN INTENT: resolve the installed version from
# the TARGET tree this invocation is about to replace, never from a bare `nx`
# lookup on the ambient $PATH. tests/e2e/release-sandbox.sh activates a sandbox
# HOME and prepends its own bin dir before calling this script; on a fresh
# sandbox no `nx` exists there yet, so a PATH lookup falls through to the REAL
# global install and a lagging develop pyproject reads as a false downgrade of
# an install this run has nothing to do with. Under generations the target is
# whatever `current` resolves to. A missing one (nothing installed yet)
# correctly skips the comparison -- there is nothing to downgrade.
if [ -n "$CURRENT_GEN" ] && [ "$SOURCE_KIND" = "directory" ] \
   && [ -f "${SOURCE}/pyproject.toml" ] && [ -x "${CURRENT_GEN}/bin/nx" ]; then
    # Pipe-free tail (nexus-i66g4/wbeyi class): capture sed's full output, then
    # take the first line by parameter expansion. Under `set -o pipefail` a
    # still-writing sed closed early by `head -1` risks its SIGPIPE being
    # promoted over head's own successful exit status.
    SRC_VERSION_ALL="$(sed -n "s/^version *= *[\"']\([^\"']*\)[\"']/\1/p" "${SOURCE}/pyproject.toml")"
    SRC_VERSION="${SRC_VERSION_ALL%%$'\n'*}"
    [ -n "$SRC_VERSION" ] || echo "warn: could not parse version from ${SOURCE}/pyproject.toml — downgrade guard inactive"
    INSTALLED_ALL="$("${CURRENT_GEN}/bin/nx" --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p' || true)"
    INSTALLED_VERSION="${INSTALLED_ALL%%$'\n'*}"
    if [ -n "$SRC_VERSION" ] && [ -n "$INSTALLED_VERSION" ]; then
        NEWEST="$(printf '%s\n%s\n' "$SRC_VERSION" "$INSTALLED_VERSION" | sort -V | tail -1)"
        if [ "$SRC_VERSION" != "$INSTALLED_VERSION" ] && [ "$NEWEST" = "$INSTALLED_VERSION" ] \
           && [ "$ALLOW_DOWNGRADE" != "1" ]; then
            echo "REFUSING to reinstall: source checkout is ${SOURCE} at version"
            echo "  ${SRC_VERSION}, but the installed nx is ${INSTALLED_VERSION} — this is a"
            echo "  DOWNGRADE (stale checkout? wrong directory?). Two incidents of this"
            echo "  class silently broke the shared install (nexus-q3xrx)."
            echo "  Pass --allow-downgrade to downgrade deliberately."
            exit 1
        fi
        if [ "$SRC_VERSION" = "$INSTALLED_VERSION" ]; then
            echo "WARNING: source ${SOURCE} (branch $(git -C "${SOURCE}" branch --show-current 2>/dev/null || echo '?')) is at ${SRC_VERSION} — the SAME"
            echo "  version as the installed nx (${INSTALLED_VERSION}). Working-tree changes WILL be"
            echo "  picked up, but the version string won't move (release bumps live on"
            echo "  main, not develop — nexus-r024j). To install a released build instead:"
            echo "  scripts/reinstall-tool.sh 'conexus==${INSTALLED_VERSION}'"
        fi
    fi
fi

# ── Extras carry forward from the generation being replaced ──────────────────
# nexus-2fyb: mineru was promoted from an extra to a default dep, so a stale
# receipt still naming it must not propagate. Genuinely optional extras (e.g.
# [local]) are receipt-driven and survive the reinstall.
EXTRAS=""
if [ -n "$CURRENT_GEN" ]; then
    _receipt="$(nx_receipt_path "$CURRENT_GEN")"
    if [ -f "$_receipt" ]; then
        EXTRAS="$(NX_RECEIPT="$_receipt" python3 -c '
import json, os
try:
    raw = json.load(open(os.environ["NX_RECEIPT"])).get("extras", "") or ""
except Exception:
    raw = ""
keep = [e for e in (p.strip() for p in raw.split(",")) if e and e != "mineru"]
print(",".join(keep))
' 2>/dev/null || true)"
    fi
fi
[ -n "$EXTRAS" ] && echo "Preserving extras: [$EXTRAS]"

# ── Build the new generation, then flip ──────────────────────────────────────
_build_args=(--source "$SOURCE")
[ -n "$EXTRAS" ] && _build_args+=(--extras "$EXTRAS")

GEN="$("$_INSTALL/install_generation.sh" "${_build_args[@]}")"
nx_flip_current "$GEN" "$TOOLS_DIR"
nx_write_shims "$GEN" "$BIN_DIR"

"${GEN}/bin/nx" --version || true

# ── Report holders. Do not act on them. ──────────────────────────────────────
# Guard semantics INVERT here (design point 5): the old script used this census
# to decide whom to refuse or kill. Nothing is killed and nothing is refused;
# the holders of older generations are named so the operator knows why those
# trees are still on disk, and told the one true thing about them -- they
# converge by themselves.
_snapshot="$(_nx_ps_snapshot refresh)"
for _gen in "$TOOLS_DIR"/"$NX_GENERATION_PREFIX"*; do
    [ -e "$_gen" ] || continue
    [ "$_gen" = "$GEN" ] && continue
    _pids="$(nx_generation_holder_pids "$_gen" "$_snapshot" || true)"
    [ -n "$_pids" ] || continue
    _count="$(printf '%s\n' "$_pids" | grep -c . || true)"
    echo "${_count} holder(s) still bound to $(basename "$_gen"); they converge at their next spawn."
done

# Reap what no rule protects. The four never-delete rules (current, previous,
# any generation with a live holder, and the generation hosting this installer)
# are absolute, so a held tree survives this call however old it is.
nx_gc_generations --keep 3 "$TOOLS_DIR" || true
