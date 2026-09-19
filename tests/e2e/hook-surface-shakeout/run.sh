#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-215 hook-surface shakeout: drive EVERY populated hook event of the
# version under test through a REAL Claude Code session, in a container, on a
# virgin local-mode box, with the plugin loaded from this checkout.
#
#   tests/e2e/hook-surface-shakeout/run.sh
#   tests/e2e/hook-surface-shakeout/run.sh --keep    # leave the container up
#
# WHY THIS EXISTS, and what it covers that nothing else does. RDR-215 replaced
# the bash hook layer with twelve `hook_*` MCP tools and ten `nx-hook` verbs.
# Every gate in the repo calls those handlers DIRECTLY -- pytest imports
# `nexus.hooks.*` and calls `run(payload)`. Nothing proved that Claude Code
# INVOKES them. The two harnesses that come closest each miss on one axis:
# `tests/cc-validation` drives real Claude Code but, in its own words, "with
# no plugin install" (fixtures, not our manifest); `tests/e2e/rdr208-mvv`
# loads our real plugin but deliberately trims hooks.json to the two hooks
# its journey needs.
#
# THE FAILURE THIS IS AIMED AT IS SILENCE, not a crash. Claude Code treats an
# unavailable mcp_tool hook as a NON-BLOCKING error: it logs
# "mcp_tool hook skipped" and proceeds. So a hooks.json naming a tool the
# wheel does not register does not fail -- twelve guards, including the
# bd-close gate, the RDR-184 EXPECT writer and the orchestrator guard, simply
# stop running. That is the exposure `conexus/PENDING_RELEASE.md` names as
# the wheel floor, and it is invisible to every green suite.
#
# AUTH IS NOT RE-DERIVED HERE. `tests/e2e/lib/claude_credentials.py pick` is
# the one shared picker (a bare keychain lookup returns an empty husk: two
# items share the service name). This script uses it exactly as
# rdr208-mvv/run.sh does and adds nothing of its own.
#
# Sessions are REAL and BILLED. Ends "HOOK-SURFACE SHAKEOUT PASSED" or
# FAILED; exits 2 (UNVERIFIED) with no usable credential, which is never a
# skip-pass.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
CRED_TOOL="$ROOT/tests/e2e/lib/claude_credentials.py"
MVV="$ROOT/tests/e2e/rdr208-mvv"
KEEP=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
command -v docker > /dev/null || { echo "docker is required" >&2; exit 2; }

# --- credential: the shared picker, never a bare keychain read -------------
FRESHCREDS="$(python3 "$CRED_TOOL" pick 2>/dev/null || true)"
if [ -z "$FRESHCREDS" ] && [ -f "$HOME/.claude/.credentials.json" ] \
   && python3 "$CRED_TOOL" check "$HOME/.claude/.credentials.json" > /dev/null 2>&1; then
    echo "(keychain miss: falling back to ~/.claude/.credentials.json, may be stale)" >&2
    FRESHCREDS="$(cat "$HOME/.claude/.credentials.json")"
fi
if [ -z "$FRESHCREDS" ]; then
    echo "HOOK-SURFACE SHAKEOUT UNVERIFIED: no usable Claude oauth credential" >&2
    echo "(run tests/e2e/auth-login.sh -- it is interactive, a human must do it)" >&2
    exit 2
fi

if [ -n "$(git -C "$ROOT" status --porcelain)" ] && [ "${NX_SHAKEOUT_ALLOW_DIRTY:-}" != "1" ]; then
    echo "HOOK-SURFACE SHAKEOUT UNVERIFIED: dirty checkout, and these are billed" >&2
    echo "sessions -- the wheel under test would carry uncommitted work. This tree" >&2
    echo "is shared with other sessions, so it is usually a peer's." >&2
    git -C "$ROOT" status --porcelain >&2
    echo "Set NX_SHAKEOUT_ALLOW_DIRTY=1 if the changes are yours and intended." >&2
    exit 2
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/hook-shakeout.XXXXXX")"
[ -n "$KEEP" ] || trap 'rm -rf "$STAGE"' EXIT
umask 077
printf '%s' "$FRESHCREDS" > "$STAGE/.claude-credentials.json"
umask 022

SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
echo "[stage] wheel + plugin + checkout from $SHA"
mkdir -p "$STAGE/wheel" "$STAGE/plugin/.claude-plugin" "$STAGE/plugin/hooks" "$STAGE/checkout"
uv build --wheel --out-dir "$STAGE/wheel" "$ROOT" > "$STAGE/build.log" 2>&1 \
    || { cat "$STAGE/build.log" >&2; exit 1; }

cp "$ROOT/conexus/.claude-plugin/plugin.json" "$STAGE/plugin/.claude-plugin/"
cp -R "$ROOT/conexus/hooks/scripts" "$STAGE/plugin/hooks/scripts"
# THE PLUGIN'S OWN MCP REGISTRATION, and it is load-bearing for the tool tier.
# hooks.json addresses `"server": "plugin:conexus:nexus"`; that name is what
# Claude Code namespaces the plugin's own `.mcp.json` key `nexus` to when it
# loads the plugin. The first cut of this harness did not stage this file and
# instead forced a separate server with `--mcp-config --strict-mcp-config`
# keyed `nexus`, so every one of the twelve tool-tier entries addressed a
# server that did not exist under that name. Measured: "Stop hook error: MCP
# server 'plugin:conexus:nexus' not connected", and the session carried on
# regardless -- which is the fail-open this shakeout exists to find, reproduced
# by the harness instead of by the product.
# The sequential-thinking entry is dropped: it shells out to npx, which this
# image has no node for, and nothing under test needs it.
python3 - "$ROOT/conexus/.mcp.json" "$STAGE/plugin/.mcp.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d.pop("sequential-thinking", None)
json.dump(d, open(sys.argv[2], "w"), indent=2)
print(f"[stage] plugin .mcp.json: {', '.join(sorted(d))}")
PY

# hooks.json UNTRIMMED except for the two entries that would mutate the thing
# under test. `upgrade-auto` installs a generation and flips <tools>/current,
# which would replace the very wheel this run is measuring; `self-gc` reaps
# generations. Both are SessionStart command-tier entries, so dropping them
# leaves every tool-tier entry and every other verb in place -- which is the
# surface this shakeout exists to exercise. Anything else removed here would
# be the trim that made rdr208-mvv unable to answer this question.
python3 - "$ROOT/conexus/hooks/hooks.json" "$STAGE/plugin/hooks/hooks.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src))
DROP = {"upgrade-auto", "self-gc"}
kept = dropped = 0
for event, groups in d["hooks"].items():
    for g in groups:
        out = []
        for h in g.get("hooks", []):
            args = h.get("args") or []
            if h.get("command") == "nx-hook" and args and args[0] in DROP:
                dropped += 1
                continue
            out.append(h)
            kept += 1
        g["hooks"] = out
json.dump(d, open(dst, "w"), indent=2)
print(f"[stage] hooks.json: {kept} entries kept, {dropped} dropped (mutate-the-wheel only)")
PY

# A checkout, because several hooks read one (rdr reads docs/rdr, the routing
# rules resolve a repo, session-context wants beads). rdr208-mvv's trim exists
# precisely because its container lacks this.
git -C "$ROOT" archive HEAD | tar -x -C "$STAGE/checkout"
printf '%s\n' "$SHA" > "$STAGE/checkout/.shakeout-sha"

cp "$MVV/send.py" "$MVV/assistant_said.py" "$MVV/turn_end.py" "$STAGE/"
cp "$HERE/shakeout_in_container.sh" "$HERE/hook_census.py" "$STAGE/"
cp "$HERE/Dockerfile" "$STAGE/"
cat > "$STAGE/settings.json" <<'JSON'
{
  "hooks": {
    "Stop": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "python3 /home/nexus/turn_end.py", "timeout": 5}
      ]}
    ]
  }
}
JSON

IMAGE="nexus-hook-shakeout:$SHA"
echo "[build] $IMAGE"
docker build -q -t "$IMAGE" "$STAGE" > "$STAGE/docker-build.log" 2>&1 \
    || { tail -40 "$STAGE/docker-build.log" >&2; exit 1; }

# Artifacts ALWAYS come out, whatever the verdict. The first two runs each
# ended with --rm and took their logs with them, so working out why the census
# saw nothing would have needed another billed session to reproduce what had
# already been written to disk once.
ART="${NX_SHAKEOUT_ARTIFACTS:-${TMPDIR:-/tmp}/hook-shakeout-$SHA.artifacts}"
rm -rf "$ART"; mkdir -p "$ART"; chmod 777 "$ART"

echo "[run] real Claude Code sessions, plugin from $SHA"
echo "[run] artifacts -> $ART"
set +e
docker run --rm \
    -v "$STAGE/.claude-credentials.json:/creds/.credentials.json:ro" \
    -v "$ART:/artifacts" \
    -e SHAKEOUT_SHA="$SHA" \
    -e SHAKEOUT_PROBE="${SHAKEOUT_PROBE:-}" \
    "$IMAGE"
rc=$?
set -e
echo "[run] artifacts:"
find "$ART" -type f 2>/dev/null | head -40 | while read -r f; do
    printf '  %-60s %s bytes\n' "${f#$ART/}" "$(wc -c < "$f")"
done
exit $rc
