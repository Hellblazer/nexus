#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-208 local-mode MVV (beads nexus-galkv.19, nexus-kdxyv): session-id mail
# addressing on a virgin LOCAL-mode box, in a container, through REAL Claude
# Code sessions launched with the development-channel flag, the real hooks of
# the version under test, and mailbox_send against the bundled engine. See
# mvv_in_container.sh. Sessions are real and billed (about five launches).
#
#   tests/e2e/rdr208-mvv/run.sh                     # wheel and plugin from this checkout
#   tests/e2e/rdr208-mvv/run.sh --published 7.54.0  # the published wheel, that tag's plugin
#
# Step 6 (/branch) follows the plugin under test: one whose SessionStart
# matcher names `fork` must hand the MCP server off to the fork; one without
# it must reproduce the pre-fix behaviour. Ends "RDR-208 LOCAL-MODE MVV
# PASSED" or FAILED; exits 2 (UNVERIFIED) with no usable oauth credential.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
CRED_TOOL="$ROOT/tests/e2e/lib/claude_credentials.py"
PUBLISHED=""
while [ $# -gt 0 ]; do
    case "$1" in
        --published) PUBLISHED="${2:?--published needs a version}"; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
command -v docker > /dev/null || { echo "docker is required" >&2; exit 2; }

# The sessions are real: a usable oauth credential, picked by CONTENT from the
# keychain (never a bare keychain lookup; tests/e2e/lib/claude_credentials.py),
# with the on-disk file as the fallback only when it passes the same check.
# Without one this run is UNVERIFIED (exit 2), never a skip-pass.
FRESHCREDS="$(python3 "$CRED_TOOL" pick 2>/dev/null || true)"
if [ -z "$FRESHCREDS" ] && [ -f "$HOME/.claude/.credentials.json" ] \
   && python3 "$CRED_TOOL" check "$HOME/.claude/.credentials.json" > /dev/null 2>&1; then
    echo "(keychain miss: falling back to ~/.claude/.credentials.json, may be stale)" >&2
    FRESHCREDS="$(cat "$HOME/.claude/.credentials.json")"
fi
if [ -z "$FRESHCREDS" ]; then
    echo "RDR-208 LOCAL-MODE MVV UNVERIFIED: no usable Claude oauth credential (run tests/e2e/auth-login.sh)" >&2
    exit 2
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/rdr208-mvv.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
umask 077
printf '%s' "$FRESHCREDS" > "$STAGE/.claude-credentials.json"
umask 022
cp "$HERE/Dockerfile" "$HERE/mvv_in_container.sh" "$HERE/send.py" "$HERE/assistant_said.py" "$HERE/turn_end.py" "$HERE/channel_wakes.py" "$STAGE/"
mkdir -p "$STAGE/wheel" "$STAGE/plugin/.claude-plugin" "$STAGE/plugin/hooks/scripts"
if [ -n "$PUBLISHED" ]; then
    LABEL="published-$PUBLISHED"
    git -C "$ROOT" rev-parse --verify -q "v$PUBLISHED^{commit}" > /dev/null \
        || { echo "no tag v$PUBLISHED in $ROOT" >&2; exit 2; }
    git -C "$ROOT" archive "v$PUBLISHED" conexus/.claude-plugin conexus/hooks | tar -x -C "$STAGE"
    SRC="$STAGE/conexus"
    printf 'conexus==%s\n' "$PUBLISHED" > "$STAGE/wheel/SPEC"
else
    DIRTY=""
    if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
        DIRTY="-dirty"
        # This checkout is shared with other sessions, so a dirty tree here is
        # usually a PEER's in-flight work, not this caller's: the wheel under
        # test would then carry someone else's half-finished edit, and these
        # are billed sessions. Refuse by default and name what is dirty.
        # NX_MVV_ALLOW_DIRTY=1 is the deliberate opt-in for testing your own
        # uncommitted change.
        if [ "${NX_MVV_ALLOW_DIRTY:-}" != "1" ]; then
            echo "RDR-208 LOCAL-MODE MVV UNVERIFIED: the checkout is dirty, so the wheel under" >&2
            echo "test would carry uncommitted work (this tree is shared with other sessions)." >&2
            git -C "$ROOT" status --porcelain >&2
            echo "Commit, stash, or set NX_MVV_ALLOW_DIRTY=1 if the changes are yours and intended." >&2
            exit 2
        fi
        echo "WARNING: running billed sessions against a DIRTY tree (NX_MVV_ALLOW_DIRTY=1)" >&2
    fi
    LABEL="tree-$(git -C "$ROOT" rev-parse --short HEAD)$DIRTY"
    uv build --wheel --out-dir "$STAGE/wheel" "$ROOT" > "$STAGE/build.log" 2>&1 \
        || { cat "$STAGE/build.log" >&2; exit 1; }
    SRC="$ROOT/conexus"
fi
# The plugin under test, loaded with --plugin-dir (the configuration every
# measurement of the hook-delivers-at-the-wake path used), with hooks.json
# trimmed to the two hooks under test: the rest of the battery (nx upgrade,
# nx self gc, preflight, rdr, lockstep, stop verification) reads a checkout
# the container lacks, and nx upgrade would replace the wheel under test.
# The SessionStart matcher is copied through UNCHANGED from the version
# under test: it is what step 6's expectation is derived from.
cp "$SRC/.claude-plugin/plugin.json" "$STAGE/plugin/.claude-plugin/"
# The WHOLE scripts directory, not just the two files the trimmed hooks.json
# names: mailbox_drain.py imports sibling modules at import time
# (_endpoint_resolve, _tuple_size_limits), and a missing sibling is a
# ModuleNotFoundError BEFORE the hook's own never-raise contract applies, so
# the hook would exit 1 on every prompt. Staging the directory costs nothing
# and cannot fire anything: only hooks.json decides what runs.
cp -R "$SRC/hooks/scripts/." "$STAGE/plugin/hooks/scripts/"
# Located by the HYPHENATED verb, so both spellings work: the shell form
# ("nx hook session-start") and the exec form ("nx-hook" + args). The
# neighbouring session_start_hook.py spells it with an underscore and
# cannot collide. No block, or more than one, is a hard failure here.
MATCHER="$(python3 -c '
import json, sys
blocks = [b for b in json.load(open(sys.argv[1]))["hooks"]["SessionStart"]
          if "session-start" in json.dumps(b.get("hooks", []))]
if len(blocks) != 1:
    raise SystemExit(f"expected one SessionStart block running session-start, found {len(blocks)}")
print(blocks[0]["matcher"])
' "$SRC/hooks/hooks.json")"
# The generated declaration uses the SHELL spelling of the verb, at an
# ABSOLUTE path: a hook runs under `/bin/sh` with the environment Claude Code
# inherited, which inside the container is a tmux login shell whose PATH does
# NOT carry /home/nexus/nxenv/bin -- a bare `nx` there is "sh: 1: nx: not
# found" on every SessionStart, and the session marker is never written
# (measured 2026-09-18, one billed run; the design memo had said to bake
# absolute paths in for exactly this reason).
# because it drives the WHEEL under test through the nx console script. That
# Click verb is this generator's only dependency on the CLI surface, and this
# generator is its only consumer that nothing else names (RDR-215 moves the
# PLUGIN's own declaration to the `nx-hook` console script for startup cost,
# and keeps the Click verb for a human at a terminal, so the port does not
# affect this). tests/test_rdr208_mvv_wiring.py pins the verb's existence, so
# a future bead retiring it fails there naming this file, rather than leaving
# a container whose SessionStart hook silently never runs.
python3 - "$STAGE/plugin/hooks/hooks.json" "$MATCHER" <<'PY'
import json, sys
json.dump({"hooks": {
    "SessionStart": [{"matcher": sys.argv[2], "hooks": [
        {"type": "command", "command": "/home/nexus/nxenv/bin/nx hook session-start", "timeout": 10}]}],
    # RDR-215 nexus-q02nx.22: exec form, matching the real hooks.json --
    # the retired `_run_python_hook.sh` bash launcher is gone, and the
    # interpreter resolution it used to perform now runs in Python,
    # inside mailbox_drain.py itself (_interpreter.reexec_if_needed()).
    #
    # BRACED, like the real manifest: the shell-string form this replaced
    # was expanded by the bash Claude Code ran it under, which takes either
    # spelling. Exec form has no shell, so whatever expansion happens is
    # Claude Code's own -- and the one spelling known to work there is the
    # one conexus/hooks/hooks.json ships. Guessing the other costs a billed
    # container run to find out.
    "UserPromptSubmit": [{"matcher": "", "hooks": [
        {"type": "command",
         "command": "python3",
         "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/mailbox_drain.py"],
         "timeout": 10}]}],
    # The turn-end sentinel (~/git/recording-rig lib/sentinels.sh): the driver
    # waits for a hook signal that the turn ENDED instead of scraping the pane.
    # Per-session with no templating, since the hook reads its own session id.
    "Stop": [{"matcher": "", "hooks": [
        {"type": "command",
         "command": "/home/nexus/nxenv/bin/python /home/nexus/turn_end.py",
         "timeout": 5}]}],
}}, open(sys.argv[1], "w"), indent=2)
PY
rm -rf "$STAGE/conexus"
# The expectation follows the plugin, never a flag: a SessionStart matcher
# naming `fork` (nexus-kdxyv) hands the MCP server off on /branch.
if grep -qE '\bfork\b' <<<"$MATCHER"; then EXPECT=1; else EXPECT=0; fi
printf '{"skipDangerousModePermissionPrompt": true}\n' > "$STAGE/settings.json"
# ~/.claude.json pre-seed: onboarding done, the work dir trusted (never the
# poll-and-press-Enter path), the oauthAccount block when the login snapshot
# carries one. Mounted read-only; the container copies it into place.
python3 - "$STAGE/claude.json" "$ROOT/tests/e2e/.claude-auth/claude.json" <<'PY'
import json, pathlib, sys
out, snap = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
data = {}
if snap.is_file():
    try:
        data = json.loads(snap.read_text() or "{}")
    except ValueError:
        data = {}
data = {k: v for k, v in data.items() if k in ("oauthAccount",)}
data["hasCompletedOnboarding"] = True
data["projects"] = {"/home/nexus/work": {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}
out.write_text(json.dumps(data, indent=2))
PY

IMAGE="nexus-rdr208-mvv:$(printf '%s' "$LABEL" | tr -c 'a-z0-9_.-' '-')"
echo "building $IMAGE (expect_branch_fix=$EXPECT); the sessions inside are real and billed"
docker build -q -t "$IMAGE" "$STAGE" > /dev/null
LOG="${TMPDIR:-/tmp}/rdr208-mvv-$LABEL.log"
ART="${TMPDIR:-/tmp}/rdr208-mvv-$LABEL.artifacts"
rm -rf "$ART"
mkdir -p "$ART"
chmod 777 "$ART"
set +e
docker run --rm -v "$ART:/home/nexus/artifacts" -e MVV_ARTIFACTS=/home/nexus/artifacts \
    -v "$STAGE/.claude-credentials.json":/home/nexus/.claude/.credentials.json:ro \
    -v "$STAGE/claude.json":/home/nexus/seed/claude.json:ro \
    -e EXPECT_BRANCH_FIX="$EXPECT" -e MVV_LABEL="$LABEL" "$IMAGE" 2>&1 | tee "$LOG"
set -e
echo "log: $LOG"
echo "artifacts: $ART"
grep -q '^RDR-208 LOCAL-MODE MVV PASSED' "$LOG"
