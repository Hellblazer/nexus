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
AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"
ONLY_SCENARIO=""

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

# Need a credential source: the live macOS keychain OR a captured snapshot.
# provision_credentials (below) prefers the keychain and falls back to the
# snapshot; only error here when neither is available.
if ! { command -v security >/dev/null 2>&1 \
       && security find-generic-password -s 'Claude Code-credentials' -w >/dev/null 2>&1; } \
   && [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
    echo "Error: no credentials — keychain miss and $AUTH_DIR/.credentials.json missing." >&2
    echo "       run tests/e2e/auth-login.sh first." >&2
    exit 1
fi

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

cleanup() {
    echo ""
    echo "Cleaning up..."
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

# Provision OAuth credentials into the isolated TEST_HOME.
#
# The sandbox session reads $TEST_HOME/.claude/.credentials.json (and
# .env.test unsets ANTHROPIC_API_KEY so this file is the auth source).
# A frozen snapshot file goes stale fast: OAuth access tokens are
# short-lived and the refresh token rotates out from under a frozen copy
# once the live CLI refreshes, so a stale snapshot 401s ("Invalid
# authentication credentials") and every scenario fails before the model
# runs anything. Prefer the live macOS keychain at runtime; refresh the
# on-disk snapshot from it so the Linux/CI fallback path stays usable.
provision_credentials() {
    local dest="$TEST_HOME/.claude/.credentials.json"
    local kc_json
    if command -v security >/dev/null 2>&1 \
       && kc_json="$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null)" \
       && [[ -n "$kc_json" ]] \
       && printf '%s' "$kc_json" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
        printf '%s' "$kc_json" > "$dest"
        cp "$dest" "$AUTH_DIR/.credentials.json" 2>/dev/null || true
        echo "  [auth] provisioned from macOS keychain (live)"
    elif [[ -f "$AUTH_DIR/.credentials.json" ]]; then
        cp "$AUTH_DIR/.credentials.json" "$dest"
        echo "  [auth] provisioned from snapshot file (keychain unavailable)"
    else
        echo "Error: no credentials available — keychain miss and no snapshot at $AUTH_DIR/.credentials.json" >&2
        echo "       run tests/e2e/auth-login.sh to capture credentials." >&2
        exit 1
    fi
    chmod 600 "$dest"
}
provision_credentials

if [[ -f "$AUTH_DIR/claude.json" ]]; then
    cp "$AUTH_DIR/claude.json" "$TEST_HOME/.claude.json"
else
    echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"
fi

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
unset ANTHROPIC_API_KEY  # OAuth from .credentials.json takes priority
export HOME="$TEST_HOME"
export PATH="\$HOME/.local/bin:\$PATH"
export STUB_LOG="$STUB_LOG"
export HOOK_LOG="$HOOK_LOG"
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

_tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
_tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50

_tmux send-keys -t "$TMUX_SESSION" "source $TEST_HOME/.env.test" Enter
sleep 1
touch "$TEST_HOME/.zshrc"

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
