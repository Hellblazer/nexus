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
STUB_LOG="$TEST_HOME/stub_calls.log"
HOOK_LOG="$TEST_HOME/hook.log"
export TEST_HOME REPO_ROOT TMUX_SESSION STUB_LOG HOOK_LOG

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) ONLY_SCENARIO="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
    echo "Error: $AUTH_DIR/.credentials.json missing — run tests/e2e/auth-login.sh first" >&2
    exit 1
fi

source "$REPO_ROOT/tests/e2e/lib.sh"
TMUX_SESSION="cc-val"  # override the e2e default after sourcing

cleanup() {
    echo ""
    echo "Cleaning up..."
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    rm -rf "$TEST_HOME"
}
trap cleanup EXIT

# ─── Set up isolated test home (no plugin install) ────────────────────────────

echo "Setting up isolated test home at $TEST_HOME..."
rm -rf "$TEST_HOME"
mkdir -p "$TEST_HOME/.claude/plugins" "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills"

cp "$AUTH_DIR/.credentials.json" "$TEST_HOME/.claude/.credentials.json"
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
    rm -f "$TEST_HOME/.claude/settings.json" \
          "$TEST_HOME/.claude/.mcp.json" \
          "$STUB_LOG" "$HOOK_LOG"
    rm -rf "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills"
    mkdir -p "$TEST_HOME/.claude/agents" "$TEST_HOME/.claude/skills"
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

# ─── Start tmux ───────────────────────────────────────────────────────────────

echo "Starting tmux session '$TMUX_SESSION'..."
echo "  (run 'tmux attach -t $TMUX_SESSION' to watch live)"

tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50

tmux send-keys -t "$TMUX_SESSION" "source $TEST_HOME/.env.test" Enter
sleep 1
touch "$TEST_HOME/.zshrc"

# ─── Run scenarios ────────────────────────────────────────────────────────────

run_scenario() {
    local file="$1"
    local num
    num=$(basename "$file" | cut -d_ -f1)
    if [[ -n "$ONLY_SCENARIO" && "$num" != "$ONLY_SCENARIO" ]]; then
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
