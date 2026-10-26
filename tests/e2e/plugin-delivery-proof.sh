#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin-delivery-proof.sh -- post-publish proof that a plugin-only
# fast-lane release reaches an installed user through Claude Code's NORMAL
# plugin update (nexus-rbmja.4; design: T2 nexus/design-plugin-only-fast-lane.md).
#
#   tests/e2e/plugin-delivery-proof.sh --prev X.Y.Z --new X.Y.Z [--repo Hellblazer/nexus]
#                                      [--source <git url or path>] [--keep]
#
# RDR-197's anchored channel never had this check. nexus-konsk and nexus-semdv
# showed Claude Code keys the plugin cache and `claude plugin update` on the
# plugins[].version field, so a ref move that keeps the version is not
# delivered. A version-bumped release is (probe 2026-10-26, Claude Code
# 2.1.310). This script re-proves it per release, with the real CLI:
#
#   1. In a scrubbed HOME, add a marketplace whose entries are the previous
#      release's (vPREV's .claude-plugin/marketplace.json) and install every
#      plugin it lists.
#   2. Advance the marketplace the way a release does (vNEW's marketplace.json),
#      then run `claude plugin marketplace update` and `claude plugin update
#      <plugin>@<marketplace>` for each plugin. Not uninstall+install, not nx.
#   3. Assert installed_plugins.json says NEW for every plugin, and that every
#      WITNESS file (changed between vPREV and vNEW under the marketplace's
#      plugin directories) in the plugin cache is byte-identical to the tag's
#      copy (a deleted file must be gone). An empty witness set is a FAILURE:
#      a proof over nothing proves nothing.
#
# Why a path marketplace: `claude plugin marketplace add` refuses file:// URLs,
# so the marketplace is a local directory whose entries are git-subdir sources
# pointing at a local mirror of the release tags (shallow fetch of the two
# tags from --source, default https://github.com/<repo>.git). The git history
# is the real one; only the transport is local. Needs no token: plugin
# install and update make no model call.
#
# Verdicts (stdout): "PLUGIN DELIVERY PROOF PASSED -- <prev> -> <new>, N witness
# file(s), claude <version>" exit 0; "... FAILED ..." exit 1 on any mismatch;
# "... UNVERIFIED ..." exit 2 when the claude CLI, the network or a tag is
# absent. Usage errors exit 64 with no verdict. The claude CLI version is in
# the verdict because Claude Code's behaviour flips between versions.
#
# The real HOME is never read or written: claude runs under `env -i` with a
# scratch HOME, and so does everything else after the interpreter is resolved.
# Nothing here prints a token, an env dump or a config file.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
# shellcheck source=lib/fence_home.sh disable=SC1091
source "$HERE/lib/fence_home.sh"

PREV=""; NEW=""; REPO="Hellblazer/nexus"; SOURCE=""; KEEP=0
usage() { echo "usage: $0 --prev X.Y.Z --new X.Y.Z [--repo OWNER/NAME] [--source URL_OR_PATH] [--keep]" >&2; exit 64; }
while [ $# -gt 0 ]; do
    case "$1" in
        --prev) [ $# -ge 2 ] || usage; PREV="$2"; shift 2 ;;
        --new) [ $# -ge 2 ] || usage; NEW="$2"; shift 2 ;;
        --repo) [ $# -ge 2 ] || usage; REPO="$2"; shift 2 ;;
        --source) [ $# -ge 2 ] || usage; SOURCE="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$PREV" ] && [ -n "$NEW" ] || usage
case "$PREV$NEW" in *[!0-9A-Za-z.+-]*) usage ;; esac
[ "$PREV" != "$NEW" ] || usage
TAG_PREV="v$PREV"; TAG_NEW="v$NEW"

TAB="$(printf '\t')"
SCRATCH=""; GATE_HOME=""
# shellcheck disable=SC2329  # invoked through the EXIT trap
cleanup() {
    local name
    [ -n "$SCRATCH" ] && [ -d "$SCRATCH" ] || return 0
    if [ "$KEEP" -eq 1 ]; then echo "kept sandbox: $SCRATCH" >&2; return 0; fi
    # Links first, as links: nothing may follow one into the real toolchain caches.
    for name in "${FENCE_MIRRORED_ENTRIES[@]}"; do
        if [ -n "$GATE_HOME" ] && [ -L "$GATE_HOME/$name" ]; then rm -f "$GATE_HOME/$name"; fi
    done
    rm -rf "$SCRATCH"
}
trap cleanup EXIT

CLAUDE_VERSION="unknown"
verdict() {  # <PASSED|FAILED|UNVERIFIED> <exit> <text>
    echo "PLUGIN DELIVERY PROOF $1 -- $3"
    exit "$2"
}
unverified() { verdict UNVERIFIED 2 "$*"; }
failed() { verdict FAILED 1 "$*"; }

# ── preconditions: UNVERIFIED, not FAILED, when the means are absent ────────
CLAUDE="$(command -v claude 2>/dev/null)" || CLAUDE=""
[ -n "$CLAUDE" ] || unverified "the claude CLI is not on PATH ($PREV -> $NEW not examined)"
command -v git >/dev/null 2>&1 || unverified "git is not on PATH"
e2e_python_resolve || unverified "no usable Python for the harness (see above)"

REAL_HOME="${HOME:-}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/nx-plugin-delivery-proof.XXXXXX")" || unverified "cannot make a scratch directory"
SCRATCH="$(cd "$SCRATCH" && pwd)"
GATE_HOME="$SCRATCH/home"
fence_home "$REAL_HOME" "$GATE_HOME"
# From here on nothing reads the real HOME: not git, not python, not claude.
export HOME="$GATE_HOME"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
unset XDG_CONFIG_HOME

run_claude() {
    env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin:$(dirname "$CLAUDE")" HOME="$GATE_HOME" TERM=dumb \
        NX_NO_TELEMETRY=1 DISABLE_AUTOUPDATER=1 "$CLAUDE" "$@"
}
CLAUDE_VERSION="$(run_claude --version 2>/dev/null </dev/null)" || unverified "claude --version failed"
CLAUDE_VERSION="${CLAUDE_VERSION%% *}"
[ -n "$CLAUDE_VERSION" ] || unverified "claude --version printed nothing"

# ── the release tags, mirrored locally ──────────────────────────────────────
if [ -z "$SOURCE" ]; then SOURCE="https://github.com/$REPO.git"; fi
if [ -d "$SOURCE" ]; then SOURCE="file://$(cd "$SOURCE" && pwd)"; fi
MIRROR="$SCRATCH/mirror.git"
git init -q --bare "$MIRROR" || unverified "cannot create the local mirror"
for tag in "$TAG_PREV" "$TAG_NEW"; do
    git -C "$MIRROR" fetch -q --depth 1 "$SOURCE" "refs/tags/$tag:refs/tags/$tag" >"$SCRATCH/fetch.log" 2>&1 \
        || unverified "cannot fetch tag $tag from $SOURCE ($(tail -n 1 "$SCRATCH/fetch.log"))"
done

PY_REWRITE='
import json, sys
src, out, url = sys.argv[1:4]
m = json.load(open(src))
for p in m["plugins"]:
    p["source"]["url"] = url
json.dump(m, open(out, "w"), indent=2)
'
PY_INFO='
import json, sys
m = json.load(open(sys.argv[1]))
print(m["name"])
for p in m["plugins"]:
    print("\t".join([p["name"], p["source"]["path"], str(p["version"])]))
'
PY_REG='
import json, sys
d = json.load(open(sys.argv[1]))
e = d.get("plugins", {}).get(sys.argv[2]) or [{}]
print("\t".join([str(e[0].get("version", "")), str(e[0].get("installPath", ""))]))
'

MARKET_SRC="$SCRATCH/market-$TAG_NEW.json"
git -C "$MIRROR" show "$TAG_NEW:.claude-plugin/marketplace.json" >"$MARKET_SRC" 2>/dev/null \
    || unverified "$TAG_NEW has no .claude-plugin/marketplace.json"
INFO_NEW="$("$E2E_PYTHON" -I -c "$PY_INFO" "$MARKET_SRC")" || unverified "cannot read $TAG_NEW's marketplace.json"
MARKET="$(printf '%s\n' "$INFO_NEW" | sed -n '1p')"
PLUGINS="$(printf '%s\n' "$INFO_NEW" | sed '1d')"   # name<TAB>path<TAB>version
PLUGIN_DIRS="$(printf '%s\n' "$PLUGINS" | cut -f2)"

# ── witness set: files that changed under the plugin directories ────────────
WITNESS="$SCRATCH/witness.txt"; : >"$WITNESS"
for d in $PLUGIN_DIRS; do
    git -C "$MIRROR" diff --name-status --no-renames "$TAG_PREV" "$TAG_NEW" -- "$d" >>"$WITNESS" \
        || unverified "cannot diff $TAG_PREV..$TAG_NEW"
done
N_WITNESS="$(grep -c . "$WITNESS")" || N_WITNESS=0
[ "$N_WITNESS" -gt 0 ] || failed "$PREV -> $NEW: the witness set is empty (no file changed under the plugin directories: $(printf '%s' "$PLUGIN_DIRS" | tr '\n' ' ')); a proof over no files proves nothing, and this is not a plugin release"

# ── step 1: install the previous release ────────────────────────────────────
WORK="$SCRATCH/market"
mkdir -p "$WORK/.claude-plugin"
gw() { git -C "$WORK" -c user.name=proof -c user.email=proof@example.invalid -c commit.gpgsign=false "$@"; }
write_market() {  # <tag>
    git -C "$MIRROR" show "$1:.claude-plugin/marketplace.json" >"$SCRATCH/market-$1.json" || return 1
    "$E2E_PYTHON" -I -c "$PY_REWRITE" "$SCRATCH/market-$1.json" "$WORK/.claude-plugin/marketplace.json" "file://$MIRROR"
}
write_market "$TAG_PREV" || unverified "$TAG_PREV has no usable marketplace.json"
if ! { gw init -q -b main && gw add -A && gw commit -q -m "marketplace at $TAG_PREV"; }; then
    unverified "cannot build the local marketplace"
fi

CLAUDE_LOG="$SCRATCH/claude.log"
step() {  # <label> <claude args...>; a step that exits non-zero is a FAILED proof
    local label="$1"; shift
    if ! run_claude "$@" >"$CLAUDE_LOG" 2>&1 </dev/null; then
        failed "$PREV -> $NEW: '$label' exited non-zero (claude $CLAUDE_VERSION): $(tail -n 3 "$CLAUDE_LOG" | tr '\n' ' ')"
    fi
}
step "marketplace add" plugin marketplace add "$WORK"
MARKET_NAME="$MARKET"
# Only what vPREV's marketplace lists can be installed at vPREV; a plugin the
# new release adds (tool-spill at 7.86.3) is installed, not updated, below.
PREV_INFO="$("$E2E_PYTHON" -I -c "$PY_INFO" "$SCRATCH/market-$TAG_PREV.json")" || unverified "cannot read $TAG_PREV's marketplace.json"
PREV_NAMES="$(printf '%s\n' "$PREV_INFO" | sed '1d' | cut -f1)"
ALL_NAMES="$(printf '%s\n' "$PLUGINS" | cut -f1)"
in_prev() { case "
$PREV_NAMES
" in *"
$1
"*) return 0 ;; esac; return 1; }
for name in $ALL_NAMES; do
    if in_prev "$name"; then step "install $name" plugin install "$name@$MARKET_NAME"; fi
done

REG="$GATE_HOME/.claude/plugins/installed_plugins.json"
[ -f "$REG" ] || failed "$PREV -> $NEW: install left no installed_plugins.json (claude $CLAUDE_VERSION)"

# ── step 2: the normal update ───────────────────────────────────────────────
write_market "$TAG_NEW" || unverified "$TAG_NEW marketplace.json unusable"
if ! { gw add -A && gw commit -q -m "marketplace at $TAG_NEW"; }; then
    unverified "cannot advance the local marketplace"
fi
step "marketplace update" plugin marketplace update "$MARKET_NAME"
for name in $ALL_NAMES; do
    if in_prev "$name"; then
        step "update $name" plugin update "$name@$MARKET_NAME"
    else
        step "install (new in $TAG_NEW) $name" plugin install "$name@$MARKET_NAME"
    fi
done

# ── step 3: assertions ──────────────────────────────────────────────────────
PROBLEMS="$SCRATCH/problems.txt"; : >"$PROBLEMS"
problem() { printf '%s\n' "$*" >>"$PROBLEMS"; }
INSTALL_ROOTS="$SCRATCH/roots.txt"; : >"$INSTALL_ROOTS"   # path<TAB>cache root

while IFS="$(printf '\t')" read -r pname ppath pver; do
    [ -n "$pname" ] || continue
    got="$("$E2E_PYTHON" -I -c "$PY_REG" "$REG" "$pname@$MARKET_NAME")" || got=""
    gver="${got%%"$TAB"*}"; groot="${got#*"$TAB"}"
    if [ "$gver" != "$NEW" ] || [ "$pver" != "$NEW" ]; then
        problem "installed_plugins.json: $pname@$MARKET_NAME is at '${gver:-absent}', want $NEW (marketplace entry says $pver)"
    fi
    [ -n "$groot" ] || groot="$GATE_HOME/.claude/plugins/cache/$MARKET_NAME/$pname/$NEW"
    printf '%s\t%s\n' "$ppath" "$groot" >>"$INSTALL_ROOTS"
done <<EOF
$PLUGINS
EOF

BLOB="$SCRATCH/blob"
while IFS="$(printf '\t')" read -r status wpath; do
    [ -n "$wpath" ] || continue
    top="${wpath%%/*}"; rel="${wpath#*/}"
    root="$(awk -F'\t' -v t="$top" '$1 == t { print $2; exit }' "$INSTALL_ROOTS")"
    if [ -z "$root" ]; then problem "$wpath: no installed plugin owns $top/"; continue; fi
    cached="$root/$rel"
    if [ "$status" = "D" ]; then
        [ ! -e "$cached" ] || problem "$wpath: deleted in $TAG_NEW but still in the plugin cache ($cached)"
        continue
    fi
    git -C "$MIRROR" cat-file blob "$TAG_NEW:$wpath" >"$BLOB" 2>/dev/null || { problem "$wpath: not in $TAG_NEW"; continue; }
    if [ ! -f "$cached" ]; then problem "$wpath: missing from the plugin cache ($cached)"
    elif ! cmp -s "$BLOB" "$cached"; then problem "$wpath: plugin cache copy differs from $TAG_NEW's"
    fi
done <"$WITNESS"

if [ -s "$PROBLEMS" ]; then
    n="$(grep -c . "$PROBLEMS")"
    echo "delivery mismatches ($n):" >&2
    head -n 20 "$PROBLEMS" | sed 's/^/  /' >&2 || true
    failed "$PREV -> $NEW: $n mismatch(es) after the normal plugin update, claude $CLAUDE_VERSION (first: $(sed -n '1p' "$PROBLEMS"))"
fi
verdict PASSED 0 "$PREV -> $NEW, $N_WITNESS witness file(s), claude $CLAUDE_VERSION"
