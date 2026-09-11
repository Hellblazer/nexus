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
#   7. SAME-VERSION REF MOVE (nexus-konsk): a plugin-only cut lands a new
#      commit under the clone's `conexus/` tree and moves the marketplace's
#      pinned `source.ref` to an anchored tag (`plugin-v$NEW-1`) WITHOUT
#      ever touching the `version` field -- the RDR-197 channel's exact
#      shape, and the case that shipped to nobody on plugin-v7.41.0-1
#      (2026-09-11) before the first nexus-konsk fix round.
#   8. SESSION-START HOOK DETECTS IT DIRECTLY (nexus-konsk follow-up
#      round, critique [25316]): the drift from step 7 is still live
#      (nothing has run `nx upgrade` yet); this leg invokes
#      `version_lockstep_hook.py` on its own, against the real sandbox
#      registry + known_marketplaces.json + local clone, and asserts it
#      detects the drift with no network and nudges naming the plugin id
#      and the sha move -- the piece the follow-up critique found was
#      still missing (the hook's version-mismatch check alone could never
#      see a same-version cut, so nothing dispatched automatically). Does
#      NOT prove the detached action completes the reinstall when fired
#      from a real SessionStart (the sandbox here is not a managed
#      install, so the action's own dev-tree gate skips it, same as step
#      6 above) -- see the leg's own comment for what still isn't proven.
#   9. `nx upgrade --skip-t3` (the always-reachable manual-trigger path)
#      resolves the SAME drift step 7 set up, and asserts: a "picked up a
#      plugin-only release" line, the registry's gitCommitSha moved, and
#      the cut's own content (a marker file) is present at the plugin's
#      real installPath -- proving delivery, not just a registry-field
#      update. This leg is RED on pre-fix code (the version-only
#      comparison reports "in lockstep" and touches nothing).
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

echo "── 1/9 register the marketplace at $PREV_TAG and install both plugins ──"
_claude plugin marketplace add "$CLONE" </dev/null || _fail "marketplace add failed"
_claude plugin install conexus@nexus-plugins -y </dev/null || _fail "install conexus@$PREV_TAG failed"
_claude plugin install sn@nexus-plugins -y </dev/null || _fail "install sn@$PREV_TAG failed"
SB_REGISTRY="$SB/.claude/plugins/installed_plugins.json"
[ -f "$SB_REGISTRY" ] || _fail "sandbox registry not written at $SB_REGISTRY (isolation broken?)"
_ver() { python3 -c "import json,sys; print(json.load(open('$SB_REGISTRY'))['plugins']['$1@nexus-plugins'][0]['version'])"; }
[ "$(_ver conexus)" = "$PREV" ] || _fail "conexus installed at $(_ver conexus), expected $PREV"
[ "$(_ver sn)" = "$PREV" ] || _fail "sn installed at $(_ver sn), expected $PREV"
echo "  installed conexus + sn at $PREV"

echo "── 2/9 advance the marketplace clone to $NEW_TAG ──"
git -C "$CLONE" checkout -q "$NEW_TAG"

echo "── 3/9 nx upgrade from this checkout under the sandbox HOME ──"
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

echo "── 4/9 assert the registry moved and the output named it ──"
[ "$(_ver conexus)" = "$NEW" ] || _fail "conexus still at $(_ver conexus) after nx upgrade, expected $NEW"
[ "$(_ver sn)" = "$NEW" ] || _fail "sn still at $(_ver sn) after nx upgrade, expected $NEW"
grep -q "^Plugin update: conexus@nexus-plugins $PREV -> $NEW$" "$OUT" || _fail "no conexus lockstep line in output"
grep -q "^Plugin update: sn@nexus-plugins $PREV -> $NEW$" "$OUT" || _fail "no sn lockstep line in output"
grep -q "^Plugin update: restart the Claude Code session" "$OUT" || _fail "no restart line in output"
NOW_MTIME="$( [ -f "$REAL_REGISTRY" ] && stat -f %m "$REAL_REGISTRY" 2>/dev/null || stat -c %Y "$REAL_REGISTRY" 2>/dev/null || echo none )"
[ "$NOW_MTIME" = "$REAL_MTIME" ] || _fail "the REAL registry $REAL_REGISTRY changed during the gate (isolation broken)"

echo "── 5/9 skew: the $NEW plugin's lockstep hook against a $PREV CLI ──"
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

echo "── 6/9 same-version ref move: a plugin-only cut with no version bump (nexus-konsk) ──"
# The RDR-197 channel shape exactly: a new commit lands under conexus/ and
# the marketplace's pinned ref moves to an anchored tag, but `version`
# never changes. The marketplace here is a "directory" (local-path)
# source, so its resolved installLocation IS $CLONE itself (confirmed:
# known_marketplaces.json carries no separate clone for a local path) --
# committing directly in $CLONE is exactly what a refreshed clone would
# see, no network required for this leg.
MARKER="__nexus_konsk_gate_marker__"
echo "gate marker $(date +%s)" >"$CLONE/conexus/$MARKER"
python3 - "$CLONE" "$NEW" <<'PYEOF'
import json, sys
clone, new = sys.argv[1], sys.argv[2]
path = f"{clone}/.claude-plugin/marketplace.json"
with open(path) as fh:
    data = json.load(fh)
for p in data["plugins"]:
    if p["name"] == "conexus":
        # The marketplace LISTING (installLocation, a "directory" source)
        # is independent of each PLUGIN's own git-subdir source -- that
        # still names the real GitHub URL from the real tag this repo
        # started at. Repoint it at $CLONE too so the reinstall this leg
        # provokes resolves the synthetic tag locally, no network needed.
        p["source"]["url"] = f"file://{clone}"
        p["source"]["ref"] = f"plugin-v{new}-1"
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
PYEOF
git -C "$CLONE" add -A
git -C "$CLONE" -c user.email=gate@example.invalid -c user.name=gate commit -q -m "plugin-only cut (gate rehearsal, nexus-konsk)"
git -C "$CLONE" tag "plugin-v$NEW-1"

SHA_BEFORE="$(python3 -c "import json; print(json.load(open('$SB_REGISTRY'))['plugins']['conexus@nexus-plugins'][0].get('gitCommitSha',''))")"
[ -n "$SHA_BEFORE" ] || _fail "sandbox registry carries no gitCommitSha for conexus after step 3/7 (cannot exercise the ref-drift check)"

echo "── 7/9 SessionStart hook detects the same-version ref move directly (nexus-konsk) ──"
# The wiring gap the critique found (P0 [25316]): converge_plugins alone
# (leg 6/7 below) is reachable only from a human `nx upgrade` or the
# RDR-143 detached action, and that action fired ONLY on a CLI version
# mismatch -- never on a same-version ref move. This leg proves the FIX:
# the hook itself (no `nx upgrade` invoked yet -- the drift is still
# live) detects the drift with no network, against the REAL sandbox
# registry + known_marketplaces.json + the real local clone leg 6/7 just
# committed to, and emits the nudge naming the plugin and the sha move.
#
# THIS CHECKOUT'S hook, not the installed cache copy: $NEW_PLUGIN_ROOT's
# `hooks/scripts/version_lockstep_hook.py` is whatever the PUBLISHED
# $NEW_TAG shipped -- the fix under test here has not been tagged yet, so
# that copy can never see it (measured: pointed there first, this leg
# failed with an empty, no-drift nudge -- the pre-fix hook, correctly
# silent, not this fix). Run $REPO_ROOT's own copy instead, so `_ACTION`
# and `_LAUNCHER` (resolved relative to `__file__`) also resolve to this
# checkout's `version_lockstep_action.py`, exercising the actual diff
# under test against real CLI-produced registry/marketplace file shapes.
#
# What this does NOT prove: that the detached action's own `nx upgrade`
# call completes the reinstall when dispatched FROM the hook. The
# sandbox HOME here has neither a `<tools>/current` generation layout
# nor a `uv tool install` receipt for conexus (leg 5/7 only ever `uv pip
# install`s into a bare venv), so the detached action's own gate 1
# (editable/dev-tree check, version_lockstep_action.py) would skip it --
# the identical skip leg 5/7 already asserts deliberately for the
# CLI-skew case. Proving genuine end-to-end delivery through a real
# SessionStart dispatch would need this gate to also fake a managed
# install layout, which it does not build today; that half stays proven
# by (a) the unit suite's `TestDispatchRefDriftActionIsNonBlocking`
# (the Popen call itself, argv and detached-stdio contract) and (b) leg
# 9/9 below, which drives the SAME drift through `nx upgrade` directly
# (the manual-trigger path, always reachable) and confirms the reinstall
# actually happens.
HOOK_OUT2="$WORK/hook2.out"
MARKER_FOR_HOOK="$SB/.config/nexus/cli_lockstep_marker"
mkdir -p "$(dirname "$MARKER_FOR_HOOK")"
printf '%s' "$NEW" >"$MARKER_FOR_HOOK"  # already in CLI-version lockstep -> isolates the ref-drift nudge
set +e
env -i HOME="$SB" PATH="$VENV/bin:$PATH" TERM=dumb NX_NO_TELEMETRY=1 \
    NEXUS_CONFIG_DIR="$SB/.config/nexus" CLAUDE_PLUGIN_ROOT="$NEW_PLUGIN_ROOT" \
    NX_LOCKSTEP_MARKER="$MARKER_FOR_HOOK" NX_LOCKSTEP_LOG="$SB/.config/nexus/lockstep.log" \
    bash "$REPO_ROOT/conexus/hooks/scripts/_run_python_hook.sh" "$REPO_ROOT/conexus/hooks/scripts/version_lockstep_hook.py" \
    </dev/null >"$HOOK_OUT2" 2>"$WORK/hook2.err"
HRC2=$?
set -e
[ "$HRC2" = 0 ] || _fail "the ref-drift hook exited $HRC2: $(tail -3 "$WORK/hook2.err" | tr '\n' ' ')"
grep -q '"additionalContext"' "$HOOK_OUT2" || _fail "ref-drift hook printed no additionalContext nudge; stdout: $(head -c 300 "$HOOK_OUT2")"
grep -q "conexus@nexus-plugins" "$HOOK_OUT2" || _fail "the nudge does not name conexus@nexus-plugins: $(head -c 300 "$HOOK_OUT2")"
grep -q "${SHA_BEFORE:0:7}" "$HOOK_OUT2" || _fail "the nudge does not name the pre-drift sha ${SHA_BEFORE:0:7}: $(head -c 300 "$HOOK_OUT2")"
echo "  hook alone (no nx upgrade yet) detected the drift and named it: $(head -c 200 "$HOOK_OUT2")"

echo "── 9/9 nx upgrade (the always-reachable manual trigger) delivers the SAME drift ──"
OUT2="$WORK/upgrade2.out"
set +e
env -i HOME="$SB" PATH="$PATH" TERM=dumb NX_NO_TELEMETRY=1 NX_LOCAL=1 \
    NEXUS_CONFIG_DIR="$SB/.config/nexus" \
    ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
    uv run --project "$REPO_ROOT" nx upgrade --skip-t3 >"$OUT2" 2>&1
RC2=$?
set -e
sed 's/^/  | /' "$OUT2" | tail -15
[ "$RC2" = 0 ] || _fail "nx upgrade (same-version ref-move leg) exited $RC2"

grep -q "^Plugin update: conexus@nexus-plugins $NEW: picked up a plugin-only release" "$OUT2" \
    || _fail "no ref-move pickup line in output (nexus-konsk regression): $(tail -5 "$OUT2")"

SHA_AFTER="$(python3 -c "import json; print(json.load(open('$SB_REGISTRY'))['plugins']['conexus@nexus-plugins'][0].get('gitCommitSha',''))")"
[ "$SHA_AFTER" != "$SHA_BEFORE" ] || _fail "registry gitCommitSha for conexus did not move (still $SHA_BEFORE): the plugin-only cut was not picked up"
[ "$(_ver conexus)" = "$NEW" ] || _fail "conexus version moved from $NEW during the ref-move leg (should never move)"

NEW_INSTALL_PATH="$(python3 -c "import json; print(json.load(open('$SB_REGISTRY'))['plugins']['conexus@nexus-plugins'][0]['installPath'])")"
[ -f "$NEW_INSTALL_PATH/$MARKER" ] || _fail "the plugin-only cut's content (marker file) never reached the install at $NEW_INSTALL_PATH -- registry sha moved but the files did not"
echo "  conexus@nexus-plugins $NEW: ref moved $SHA_BEFORE -> $SHA_AFTER, content delivered, version unchanged"

echo "PLUGIN-LOCKSTEP GATE PASSED — conexus + sn $PREV -> $NEW via nx upgrade against the real claude CLI; $NEW hook tolerates a $PREV CLI; the SessionStart hook detects a same-version plugin-only cut on its own (nexus-konsk) and nx upgrade delivers it; sandbox HOME, real registry untouched"
