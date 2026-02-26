#!/usr/bin/env bash
# Nexus E2E test suite — runs Claude Code locally via tmux with an isolated config.
#
# Usage:
#   ./tests/e2e/run.sh                   # run all scenarios
#   ./tests/e2e/run.sh --scenario 02     # run a single scenario by number
#   tmux attach -t e2e                   # watch the Claude session live
#
# Prerequisites:
#   - tmux, claude (Claude Code CLI) on PATH
#   - .env file at repo root with ANTHROPIC_API_KEY, VOYAGE_API_KEY, CHROMA_* set
#   - tests/e2e/.claude-auth/ populated (run auth-login.sh once)

set -euo pipefail

# Claude Code sets CLAUDECODE in its environment; unset it so we can launch
# Claude subprocesses for testing without triggering the nested-session guard.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ONLY_SCENARIO=""
AUTH_DIR="$SCRIPT_DIR/.claude-auth"

# Isolated home for this test run — Claude and nx configs go here, not ~/.claude
TEST_HOME="${TMPDIR%/}/nexus-e2e-home"
TEST_HOME="${TEST_HOME:-/tmp/nexus-e2e-home}"
export TEST_HOME REPO_ROOT

# ─── Argument parsing ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario)   ONLY_SCENARIO="$2"; shift 2 ;;
        *)            echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ─── Load credentials ─────────────────────────────────────────────────────────

if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a; source "$REPO_ROOT/.env"; set +a
fi

: "${ANTHROPIC_API_KEY:?'ANTHROPIC_API_KEY must be set (in .env or environment)'}"
: "${VOYAGE_API_KEY:?'VOYAGE_API_KEY must be set'}"

# ─── Source helpers ───────────────────────────────────────────────────────────

source "$SCRIPT_DIR/lib.sh"

# ─── Cleanup on exit ──────────────────────────────────────────────────────────

cleanup() {
    echo ""
    echo "Cleaning up tmux session..."
    tmux kill-session -t e2e 2>/dev/null || true
    rm -rf "$TEST_HOME"
}
trap cleanup EXIT

# ─── Set up isolated test home ────────────────────────────────────────────────

echo "Setting up isolated test home at $TEST_HOME..."
rm -rf "$TEST_HOME"
mkdir -p "$TEST_HOME/.claude/plugins"

# Inject Claude credentials so interactive mode works without OAuth browser flow.
if [[ -f "$AUTH_DIR/.credentials.json" ]]; then
    cp "$AUTH_DIR/.credentials.json" "$TEST_HOME/.claude/.credentials.json"
    if [[ -f "$AUTH_DIR/claude.json" ]]; then
        cp "$AUTH_DIR/claude.json" "$TEST_HOME/.claude.json"
        echo "Claude credentials + account config injected."
    else
        echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"
        echo "Claude credentials injected (no claude.json — run auth-login.sh again)."
    fi
else
    echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"
    echo "No credentials cached — run ./tests/e2e/auth-login.sh first."
    echo "  Interactive Claude tests may fail without pre-cached credentials."
fi

# Register the nx plugin so Claude Code discovers and loads it.
# Claude Code uses two files:
#   ~/.claude/plugins/installed_plugins.json  — registry of installed plugins
#   ~/.claude/settings.json                  — enabledPlugins + permissions
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" << PLUGINS_EOF
{
  "version": 2,
  "plugins": {
    "nx@nexus-plugins": [
      {
        "scope": "user",
        "installPath": "$REPO_ROOT/nx",
        "version": "dev",
        "installedAt": "$NOW",
        "lastUpdated": "$NOW"
      }
    ]
  }
}
PLUGINS_EOF

# Write settings.json: enable nx plugin and skip the "dangerous mode" confirmation
# dialog so claude_start doesn't need to navigate it.
cat > "$TEST_HOME/.claude/settings.json" << SETTINGS_EOF
{
  "enabledPlugins": {
    "nx@nexus-plugins": true
  },
  "skipDangerousModePermissionPrompt": true
}
SETTINGS_EOF

# ─── Install conexus from source ──────────────────────────────────────────────
# Install from local workspace into TEST_HOME so we test our dev code,
# not whatever version uv has globally.

echo "Installing conexus from source into test home..."
REAL_UV="${HOME}/.local/bin/uv"
if [[ ! -x "$REAL_UV" ]]; then
    REAL_UV="$(command -v uv)"
fi
HOME="$TEST_HOME" "$REAL_UV" tool install "$REPO_ROOT" --force 2>&1 | tail -5
echo "nx installed at $TEST_HOME/.local/bin/nx"

# ─── Write test-home env file ─────────────────────────────────────────────────
# The tmux pane will source this to pick up the isolated HOME and all env vars.

# Shared session key so nx thought add (inside Claude's Bash tool) and
# nx thought show (via crun in run.sh) address the same T2 project,
# regardless of their differing process session IDs (os.getsid).
NEXUS_SESSION_ID="e2e-test-$(date +%s)"
export NEXUS_SESSION_ID

cat > "$TEST_HOME/.env.test" << EOF
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
export HOME="$TEST_HOME"
export PATH="$TEST_HOME/.local/bin:\$PATH"
export ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
export VOYAGE_API_KEY="${VOYAGE_API_KEY:-}"
export CHROMA_API_KEY="${CHROMA_API_KEY:-}"
export CHROMA_TENANT="${CHROMA_TENANT:-}"
export CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}"
export NEXUS_SESSION_ID="$NEXUS_SESSION_ID"
cd "$REPO_ROOT"
EOF
chmod 600 "$TEST_HOME/.env.test"

# ─── Start tmux session ───────────────────────────────────────────────────────

echo "Starting tmux session 'e2e'..."
echo "  (Run 'tmux attach -t e2e' in another terminal to watch)"

tmux kill-session -t e2e 2>/dev/null || true
tmux new-session -d -s e2e -x 220 -y 50

# Source the env file in the pane so subsequent commands use TEST_HOME
tmux send-keys -t "e2e" "source $TEST_HOME/.env.test" Enter
sleep 1

# Suppress zsh new-user wizard (would absorb keystrokes before Claude starts)
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
    echo "══════════════════════════════════════════════"
    # shellcheck source=/dev/null
    source "$file"
}

for scenario_file in "$SCRIPT_DIR"/scenarios/[0-9]*.sh; do
    run_scenario "$scenario_file"
done

# ─── Summary ──────────────────────────────────────────────────────────────────

summary
