#!/usr/bin/env bash
# Claude Code feature-validation harness — interactive tmux sandbox without
# any plugin install. Each scenario writes its own settings.json/agents/skills
# into $TEST_HOME/.claude before claude_start. Reuses lib.sh helpers from
# tests/e2e for tmux/claude primitives.
#
# Usage:
#   ./tests/cc-validation/runner.sh
#   ./tests/cc-validation/runner.sh --scenario 03
#   tmux attach -t cc-val   # watch live in another terminal

set -euo pipefail

unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ONLY_SCENARIO=""

# RDR-219: the harness's own automation identity (`nexus-automation-oauth-token`
# in the keychain), never the operator's interactive `Claude Code-credentials`
# login. Thin wrapper around the shared helper (nexus-galkv.19,
# tests/e2e/lib/claude_credentials.py) so this file has one call site to
# change if the helper's path ever moves.
_cred_tool() {
    python3 "$REPO_ROOT/tests/e2e/lib/claude_credentials.py" "$@"
}

# Distinct from e2e harness — keeps state separate so concurrent runs don't collide.
TEST_HOME="${TMPDIR%/}/nexus-cc-val-home"
TMUX_SESSION="cc-val"
# Dedicated tmux socket: the harness runs on its OWN socket, invisible to the
# user's default socket. This is a hard isolation boundary — a kill-session
# (or even kill-server) here can never touch the interactive session the
# developer is working in. lib.sh's _tmux wrapper honours NX_TMUX_SOCKET.
NX_TMUX_SOCKET="cc-val-sock"
STUB_LOG="$TEST_HOME/stub_calls.log"
HOOK_LOG="$TEST_HOME/hook.log"
export TEST_HOME REPO_ROOT TMUX_SESSION STUB_LOG HOOK_LOG NX_TMUX_SOCKET

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) ONLY_SCENARIO="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# NOTE: there is deliberately NO early "do we have credentials?" check here.
# The one that used to live at this spot asked only whether a keychain lookup
# SUCCEEDED, which a token-less husk item satisfies -- see RDR-219
# nexus_rdr/219-research-9. The fail-loud gate now lives at the tmux
# server-start call below (`_cred_tool run -- tmux ... new-session`): `run`
# exits non-zero, naming the remedy, before it execs anything, if the
# automation token is absent or expired, and it runs before any scenario
# starts, so nothing expensive happens ahead of it.

source "$REPO_ROOT/tests/e2e/lib.sh"
TMUX_SESSION="cc-val"  # override the e2e default after sourcing

# ─── Deterministic launch: trust pre-seed + explicit MCP config ──────────────
# (Patterns ported from ~/git/recording-rig; see tests/cc-validation/README.md.)
#
# Two robustness upgrades applied at every claude_start:
#
#   1. TRUST PRE-SEED. Instead of polling the pane for the "trust this folder"
#      dialog and pressing Enter (fragile; the source of scenario 16's custom-
#      launcher class of bug), write hasTrustDialogAccepted into
#      $TEST_HOME/.claude.json for the project paths claude may resolve as cwd.
#      The dialog then never fires.
#
#   2. EXPLICIT MCP CONFIG. Project-scoped .mcp.json servers do NOT connect in
#      the interactive sandbox (approval gate #9189 non-functional; enable keys
#      only honored in ~/.claude.json, #24657). Launch with
#      `--mcp-config <file> --strict-mcp-config` to load the servers directly,
#      bypassing the gate. Also normalize the stub launcher from bare `python3`
#      (no `mcp` module) to the repo venv interpreter.
VENV_PY="$REPO_ROOT/.venv/bin/python"
# Fail fast with a clear message if the venv interpreter (which must have `mcp`)
# is missing — otherwise _prepare_mcp_args rewrites the stub command to a
# non-existent path and every MCP scenario fails as "server never spawned",
# which is far harder to diagnose than this up-front error.
if [[ ! -x "$VENV_PY" ]]; then
    echo "Error: venv interpreter not found at $VENV_PY — run 'uv sync' first." >&2
    echo "       (MCP scenarios launch the stub with this python; it needs the 'mcp' package.)" >&2
    exit 1
fi

_preseed_trust() {
    python3 - "$TEST_HOME/.claude.json" "$TEST_HOME" "$REPO_ROOT" <<'PY'
import json, os, pathlib, sys
cfg_p = pathlib.Path(sys.argv[1])
paths = sys.argv[2:]
try:
    data = json.loads(cfg_p.read_text())
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
projects = data.setdefault("projects", {})
seen = set()
for p in paths:
    for key in {p, os.path.realpath(p)}:
        if key in seen:
            continue
        seen.add(key)
        entry = projects.setdefault(key, {})
        entry["hasTrustDialogAccepted"] = True
        entry["hasCompletedProjectOnboarding"] = True
cfg_p.write_text(json.dumps(data, indent=2))
PY
}

# Echo the --mcp-config flags for the next launch (empty when no .mcp.json).
# Side effect: normalizes a python3/python launcher in the .mcp.json to $VENV_PY
# so the stub's `import mcp` resolves.
_prepare_mcp_args() {
    local mcp="$TEST_HOME/.mcp.json"
    [[ -f "$mcp" ]] || { printf ''; return 0; }
    python3 - "$mcp" "$VENV_PY" <<'PY'
import json, pathlib, sys
mcp_p, venv_py = pathlib.Path(sys.argv[1]), sys.argv[2]
data = json.loads(mcp_p.read_text())
changed = False
for spec in (data.get("mcpServers") or {}).values():
    if isinstance(spec, dict) and spec.get("command") in ("python3", "python"):
        spec["command"] = venv_py
        changed = True
if changed:
    mcp_p.write_text(json.dumps(data, indent=2))
PY
    printf -- '--mcp-config %s --strict-mcp-config' "$mcp"
}

# Wrap lib.sh's claude_start: pre-seed trust and compute MCP launch flags just
# before launch (the scenario writes settings/.mcp.json in its body, so this
# must run at claude_start time, not at runner setup).
eval "$(declare -f claude_start | sed '1s/claude_start/_lib_claude_start/')"
claude_start() {
    _preseed_trust
    CLAUDE_EXTRA_ARGS="$(_prepare_mcp_args)"
    export CLAUDE_EXTRA_ARGS
    _lib_claude_start "$@"
}

# ─── Live pane capture (CC_VAL_DEBUG_CAPTURE=1) ──────────────────────────────
# The README's standing lesson is that every cc-val mystery is settled by
# MEASURING the pane, and that a capture taken after the TUI exits is useless
# (the alternate screen is gone by then) — the pane has to be sampled DURING
# the run. This poller is that instrument, kept in-tree instead of being
# re-improvised each time it is needed. It is what showed "Login expired ·
# Please run /login" behind four scenarios that were all reporting
# "MCP connection issue" (2026-08-28, nexus-qs1g6).
#
#   CC_VAL_DEBUG_CAPTURE=1 ./tests/cc-validation/runner.sh --scenario 16
#
# Snapshots land OUTSIDE $TEST_HOME so the EXIT trap's rm -rf cannot eat them.
CC_VAL_DEBUG_DIR="${CC_VAL_DEBUG_DIR:-${TMPDIR%/}/cc-val-debug}"
_DEBUG_CAPTURE_PID=""

start_debug_capture() {
    [[ "${CC_VAL_DEBUG_CAPTURE:-0}" == "1" ]] || return 0
    rm -rf "$CC_VAL_DEBUG_DIR"
    mkdir -p "$CC_VAL_DEBUG_DIR/panes"
    (
        i=0
        while :; do
            i=$(( i + 1 ))
            n=$(printf '%04d' "$i")
            {
                echo "### tick=$n epoch=$(date +%s)"
                _tmux capture-pane -t "$TMUX_SESSION" -p -S -200 2>&1
            } > "$CC_VAL_DEBUG_DIR/panes/pane-$n.txt"
            if [[ -f "$STUB_LOG" ]]; then cp "$STUB_LOG" "$CC_VAL_DEBUG_DIR/stub-$n.log" 2>/dev/null || true; fi
            if [[ -f "$HOOK_LOG" ]]; then cp "$HOOK_LOG" "$CC_VAL_DEBUG_DIR/hook-$n.log" 2>/dev/null || true; fi
            sleep 2
        done
    ) &
    _DEBUG_CAPTURE_PID=$!
    echo "  [debug] pane capture every 2s -> $CC_VAL_DEBUG_DIR"
}

cleanup() {
    echo ""
    echo "Cleaning up..."
    if [[ -n "${_DEBUG_CAPTURE_PID:-}" ]]; then
        kill "$_DEBUG_CAPTURE_PID" 2>/dev/null || true
        echo "  [debug] pane capture kept at $CC_VAL_DEBUG_DIR"
    fi
    # Tear down the whole private socket — safe precisely because it is ours
    # alone (NX_TMUX_SOCKET). Falls back to a scoped kill-session if the
    # server is already gone.
    _tmux kill-server 2>/dev/null || _tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    rm -rf "$TEST_HOME"
}
trap cleanup EXIT

# ─── Set up isolated test home (no plugin install) ────────────────────────────

echo "Setting up isolated test home at $TEST_HOME..."
rm -rf "$TEST_HOME"
mkdir -p "$TEST_HOME/.claude/plugins" "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills" "$TEST_HOME/.claude/commands"

# RDR-219: no credential is provisioned into $TEST_HOME at all. The
# harness's `claude` sessions authenticate from CLAUDE_CODE_OAUTH_TOKEN,
# which reaches them via the private tmux SERVER's own environment (see the
# tmux-start section below) -- there is nothing to write, refresh or fall
# back to here. The Phase 0 spike (T2 nexus_rdr/219-research-14) verified
# that a `.claude.json` holding only `hasCompletedOnboarding` authenticates
# in this launch shape; the `oauthAccount` seed this harness used to copy
# from tests/e2e/.claude-auth/claude.json is not needed.
echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"

# Empty plugin registry — no plugins loaded by default.
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF

# Default settings: bypass dangerous-mode dialog. Each scenario overwrites this.
cat > "$TEST_HOME/.claude/settings.json" <<'EOF'
{
  "skipDangerousModePermissionPrompt": true
}
EOF

# Env file the tmux pane sources before launching claude.
cat > "$TEST_HOME/.env.test" <<EOF
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
# RDR-219: an API key outranks CLAUDE_CODE_OAUTH_TOKEN in Claude Code's auth
# precedence, so a stray key in the pane's own environment would make the
# session bill it instead of the automation token this harness provisions
# via the tmux server's environment (see the tmux-start section) -- unset
# it here so nothing but the automation token can be picked up.
unset ANTHROPIC_API_KEY
export HOME="$TEST_HOME"
export PATH="\$HOME/.local/bin:\$PATH"
export STUB_LOG="$STUB_LOG"
export HOOK_LOG="$HOOK_LOG"
# nexus-cf3p2: scenario 12 installs the REAL conexus plugin into this fresh
# HOME and dispatches a real subagent, which boots nx-mcp resolved off PATH
# -- a uv-tool-installed wheel, never this dev checkout, so install_ping's
# dev-checkout auto-suppression does not cover it. Every scenario inherits
# this, not just the ones that install the plugin: the flag is free and the
# judgement of which scenario needs it is not.
export NX_NO_TELEMETRY=1
cd "$REPO_ROOT"
EOF
chmod 600 "$TEST_HOME/.env.test"

# ─── Scenario helpers ─────────────────────────────────────────────────────────

# Wipe per-scenario state without disturbing the OAuth/credentials/plugin bits.
reset_scenario_state() {
    # NOTE (2026-05-31): scenarios write `.mcp.json` to the WORKSPACE ROOT
    # ($TEST_HOME/.mcp.json), not under .claude/. Cleaning only
    # .claude/.mcp.json left a stale project .mcp.json across scenarios — the
    # claude_start wrapper then fed it via --mcp-config to a LATER scenario's
    # parent, manufacturing a false "inline mcpServers leaked to parent" result
    # in scenario 11. Remove BOTH paths so scenarios are isolated.
    rm -f "$TEST_HOME/.claude/settings.json" \
          "$TEST_HOME/.claude/.mcp.json" \
          "$TEST_HOME/.mcp.json" \
          "$STUB_LOG" "$HOOK_LOG"
    rm -rf "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills" "$TEST_HOME/.claude/commands"
    mkdir -p "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills" "$TEST_HOME/.claude/commands"
    # Restore the dangerous-mode bypass — every scenario needs it.
    echo '{"skipDangerousModePermissionPrompt": true}' > "$TEST_HOME/.claude/settings.json"
}
export -f reset_scenario_state

# write_settings <path-to-fixture-json>: install settings.json for the next claude_start
write_settings() {
    cp "$1" "$TEST_HOME/.claude/settings.json"
}
export -f write_settings

write_mcp_config() {
    cp "$1" "$TEST_HOME/.claude/.mcp.json"
}
export -f write_mcp_config

write_agent() {
    local name="$1" src="$2"
    cp "$src" "$TEST_HOME/.claude/agents/$name.md"
}
export -f write_agent

write_skill() {
    local name="$1" src="$2"
    mkdir -p "$TEST_HOME/.claude/skills/$name"
    cp "$src" "$TEST_HOME/.claude/skills/$name/SKILL.md"
}
export -f write_skill

# write_command <name> <src>: install a slash command (.claude/commands/<name>.md)
# for the next claude_start. Used by scenario 19 (nexus-ln9y5) to validate that a
# command's ```! bash-injection block actually renders.
write_command() {
    local name="$1" src="$2"
    cp "$src" "$TEST_HOME/.claude/commands/$name.md"
}
export -f write_command

# ─── Start tmux ───────────────────────────────────────────────────────────────

echo "Starting tmux session '$TMUX_SESSION' on private socket '$NX_TMUX_SOCKET'..."
echo "  (run 'tmux -L $NX_TMUX_SOCKET attach -t $TMUX_SESSION' to watch live)"

# RDR-219 transport rule: a tmux session takes its environment from the tmux
# SERVER, not from the command that asks for the session -- so kill-server
# (not just kill-session) first, ensuring a stale server from a killed prior
# run is never reused, then start the private server fresh under
# `_cred_tool run --`. That exec puts CLAUDE_CODE_OAUTH_TOKEN in the
# environment of the process that starts the server, which every later
# pane and session on this socket inherits; it is also this harness's
# fail-loud credential gate (see the NOTE near the top of this file) and
# runs before anything else in the harness, tmux included.
_tmux kill-server 2>/dev/null || true
_cred_tool run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s "$TMUX_SESSION" -x 220 -y 50

_tmux send-keys -t "$TMUX_SESSION" "source $TEST_HOME/.env.test" Enter
sleep 1
touch "$TEST_HOME/.zshrc"

start_debug_capture

# ─── Run scenarios ────────────────────────────────────────────────────────────

run_scenario() {
    local file="$1"
    local num
    num=$(basename "$file" | cut -d_ -f1)
    if [[ -n "$ONLY_SCENARIO" && ",$ONLY_SCENARIO," != *",$num,"* ]]; then
        return 0
    fi
    echo ""
    echo "════════════════════════════════════════════════════"
    reset_scenario_state
    # shellcheck source=/dev/null
    source "$file"
}

for scenario_file in "$SCRIPT_DIR"/scenarios/[0-9]*.sh; do
    run_scenario "$scenario_file"
done

summary
