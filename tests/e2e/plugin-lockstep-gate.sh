#!/usr/bin/env bash
# tests/e2e/plugin-lockstep-gate.sh — nexus-2uwag: `nx upgrade` converges the
# Claude Code plugins, exercised against the REAL `claude plugin` CLI in an
# isolated HOME. The unit tests drive a fake `claude`; this is the only leg
# that proves the real CLI's contract (marketplace refresh, -y off a TTY,
# the "updated from A to B" line, the registry write) still matches what
# nexus.plugin_lockstep parses.
#
# Journey (two to three minutes; network: two git fetches of this repo's tags
# and one PyPI install of the previous release):
#   1. scrubbed HOME; assert the real ~/.claude registry is never touched
#   2. clone this repo at the PREVIOUS published client tag; register it as a
#      marketplace named nexus-plugins; install conexus + sn from it (they
#      resolve to that tag's source.ref, i.e. the previous release)
#   3. move the clone to the NEWEST published tag (marketplace.json now pins
#      the newest release)
#   4. run this checkout's `nx upgrade --skip-t3` under the sandbox HOME with
#      every data step neutralised (a dev tree has no service; the plugin
#      step is what is under test)
#   5. assert: both plugins moved to the newest tag in the sandbox registry,
#      the output carries one "Plugin update: <id> <prev> -> <new>" line per
#      plugin and the restart line, and the real registry's mtime is unchanged
#   6. SKEW: the NEWEST plugin's SessionStart lockstep hook run against the
#      PREVIOUS published CLI (a plain venv, so the detached RDR-143 action
#      finds no generation and no uv-tool receipt and skips): the one session
#      every plugin-first update lives through. Asserts exit 0 and the
#      additionalContext nudge naming the plugin version.
#
# What this does NOT prove: the release being cut. Pre-tag, the newest
# published tag is the PREVIOUS release, so the gate exercises the mechanics
# on (PREV-1 -> PREV) and the same mechanics carry the new tag once it
# exists. Run it again post-publish to close that loop (critique [24831]).
#
# Must end PLUGIN-LOCKSTEP GATE PASSED. Exit 1 on any miss, 2 when a
# dependency (claude CLI, uv, network) is absent: "could not check" is never a pass.
set -euo pipefail
export NX_NO_TELEMETRY=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nx-plugin-lockstep.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
_fail() { echo "PLUGIN-LOCKSTEP GATE FAILED — $*" >&2; exit 1; }
_unverified() { echo "PLUGIN-LOCKSTEP GATE UNVERIFIED — $*" >&2; exit 2; }

command -v claude >/dev/null || _unverified "claude CLI not on PATH"
command -v uv >/dev/null || _unverified "uv not on PATH"
command -v git >/dev/null || _unverified "git not on PATH"

mapfile -t TAGS < <(git -C "$REPO_ROOT" tag -l 'v*' | sort -V | tail -2)
[ "${#TAGS[@]}" = 2 ] || _unverified "need two published v* tags, found ${#TAGS[@]}"
PREV_TAG="${TAGS[0]}"; NEW_TAG="${TAGS[1]}"
PREV="${PREV_TAG#v}"; NEW="${NEW_TAG#v}"
echo "gate: previous client tag $PREV_TAG -> newest $NEW_TAG"

REAL_REGISTRY="$HOME/.claude/plugins/installed_plugins.json"
REAL_MTIME="$( [ -f "$REAL_REGISTRY" ] && stat -f %m "$REAL_REGISTRY" 2>/dev/null || stat -c %Y "$REAL_REGISTRY" 2>/dev/null || echo none )"

SB="$WORK/home"; mkdir -p "$SB"
CLONE="$WORK/marketplace-src"
git clone -q --no-checkout "$REPO_ROOT" "$CLONE"
git -C "$CLONE" checkout -q "$PREV_TAG"
# The clone's marketplace.json pins source.ref = $PREV_TAG against GitHub;
# the install fetches that tag from origin, exactly as a user's does.
_claude() { env -i HOME="$SB" PATH="$PATH" TERM=dumb NX_NO_TELEMETRY=1 ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} claude "$@"; }

echo "── 1/5 register the marketplace at $PREV_TAG and install both plugins ──"
_claude plugin marketplace add "$CLONE" </dev/null || _fail "marketplace add failed"
_claude plugin install conexus@nexus-plugins -y </dev/null || _fail "install conexus@$PREV_TAG failed"
_claude plugin install sn@nexus-plugins -y </dev/null || _fail "install sn@$PREV_TAG failed"
SB_REGISTRY="$SB/.claude/plugins/installed_plugins.json"
[ -f "$SB_REGISTRY" ] || _fail "sandbox registry not written at $SB_REGISTRY (isolation broken?)"
_ver() { python3 -c "import json,sys; print(json.load(open('$SB_REGISTRY'))['plugins']['$1@nexus-plugins'][0]['version'])"; }
[ "$(_ver conexus)" = "$PREV" ] || _fail "conexus installed at $(_ver conexus), expected $PREV"
[ "$(_ver sn)" = "$PREV" ] || _fail "sn installed at $(_ver sn), expected $PREV"
echo "  installed conexus + sn at $PREV"

echo "── 2/5 advance the marketplace clone to $NEW_TAG ──"
git -C "$CLONE" checkout -q "$NEW_TAG"

echo "── 3/5 nx upgrade from this checkout under the sandbox HOME ──"
# Data steps neutralised by construction: NEXUS_CONFIG_DIR is a fresh dir
# with no service, --skip-t3 suppresses the engine/process convergence, and
# the ladder has nothing to walk. The wheel version is this checkout's.
OUT="$WORK/upgrade.out"
set +e
env -i HOME="$SB" PATH="$PATH" TERM=dumb NX_NO_TELEMETRY=1 NX_LOCAL=1 \
    NEXUS_CONFIG_DIR="$SB/.config/nexus" \
    ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
    uv run --project "$REPO_ROOT" nx upgrade --skip-t3 >"$OUT" 2>&1
RC=$?
set -e
sed 's/^/  | /' "$OUT" | tail -15
[ "$RC" = 0 ] || _fail "nx upgrade exited $RC"

echo "── 4/5 assert the registry moved and the output named it ──"
[ "$(_ver conexus)" = "$NEW" ] || _fail "conexus still at $(_ver conexus) after nx upgrade, expected $NEW"
[ "$(_ver sn)" = "$NEW" ] || _fail "sn still at $(_ver sn) after nx upgrade, expected $NEW"
grep -q "^Plugin update: conexus@nexus-plugins $PREV -> $NEW$" "$OUT" || _fail "no conexus lockstep line in output"
grep -q "^Plugin update: sn@nexus-plugins $PREV -> $NEW$" "$OUT" || _fail "no sn lockstep line in output"
grep -q "^Plugin update: restart the Claude Code session" "$OUT" || _fail "no restart line in output"
NOW_MTIME="$( [ -f "$REAL_REGISTRY" ] && stat -f %m "$REAL_REGISTRY" 2>/dev/null || stat -c %Y "$REAL_REGISTRY" 2>/dev/null || echo none )"
[ "$NOW_MTIME" = "$REAL_MTIME" ] || _fail "the REAL registry $REAL_REGISTRY changed during the gate (isolation broken)"

echo "── 5/5 skew: the $NEW plugin's lockstep hook against a $PREV CLI ──"
VENV="$WORK/prev-venv"
env -i HOME="$SB" PATH="$PATH" TERM=dumb NX_NO_TELEMETRY=1 ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
    uv venv -q --python 3.12 "$VENV" || _unverified "uv venv failed"
env -i HOME="$SB" PATH="$PATH" TERM=dumb NX_NO_TELEMETRY=1 ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
    uv pip install -q --python "$VENV/bin/python" "conexus==$PREV" >"$WORK/prev-install.log" 2>&1 \
    || _unverified "uv pip install conexus==$PREV failed (network/PyPI): $(tail -2 "$WORK/prev-install.log" | tr '\n' ' ')"
PREV_NX_V="$("$VENV/bin/nx" --version 2>/dev/null | awk '{print $NF}')"
[ "$PREV_NX_V" = "$PREV" ] || _fail "previous CLI in the venv reports $PREV_NX_V, expected $PREV"
NEW_PLUGIN_ROOT="$(python3 -c "import json; print(json.load(open('$SB_REGISTRY'))['plugins']['conexus@nexus-plugins'][0]['installPath'])")"
[ -f "$NEW_PLUGIN_ROOT/hooks/scripts/version_lockstep_hook.py" ] || _fail "no lockstep hook under $NEW_PLUGIN_ROOT"
HOOK_OUT="$WORK/hook.out"
set +e
env -i HOME="$SB" PATH="$VENV/bin:$PATH" TERM=dumb NX_NO_TELEMETRY=1 \
    NEXUS_CONFIG_DIR="$SB/.config/nexus" CLAUDE_PLUGIN_ROOT="$NEW_PLUGIN_ROOT" \
    NX_LOCKSTEP_MARKER="$SB/.config/nexus/cli_lockstep_marker" NX_LOCKSTEP_LOG="$SB/.config/nexus/lockstep.log" \
    bash "$NEW_PLUGIN_ROOT/hooks/scripts/_run_python_hook.sh" "$NEW_PLUGIN_ROOT/hooks/scripts/version_lockstep_hook.py" \
    </dev/null >"$HOOK_OUT" 2>"$WORK/hook.err"
HRC=$?
set -e
[ "$HRC" = 0 ] || _fail "the $NEW lockstep hook exited $HRC against a $PREV CLI: $(tail -3 "$WORK/hook.err" | tr '\n' ' ')"
grep -q '"additionalContext"' "$HOOK_OUT" || _fail "hook printed no additionalContext nudge; stdout: $(head -c 300 "$HOOK_OUT")"
grep -q "$NEW" "$HOOK_OUT" || _fail "the nudge does not name plugin version $NEW: $(head -c 300 "$HOOK_OUT")"
sleep 3   # the detached action: no generation, no uv-tool receipt -> it must skip, never touch the venv
[ "$("$VENV/bin/nx" --version | awk '{print $NF}')" = "$PREV" ] || _fail "the detached lockstep action modified the previous-CLI venv (expected it to skip on a bare venv)"
echo "  $NEW hook + $PREV CLI: exit 0, nudge emitted, action skipped"

echo "PLUGIN-LOCKSTEP GATE PASSED — conexus + sn $PREV -> $NEW via nx upgrade against the real claude CLI; $NEW hook tolerates a $PREV CLI; sandbox HOME, real registry untouched"
