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
#   - the automation token present in the keychain (RDR-219): run
#     `claude setup-token` and store the result in item
#     'nexus-automation-oauth-token' under your own account; verify with
#     `python3 tests/e2e/lib/claude_credentials.py status`

set -euo pipefail

# Claude Code sets CLAUDECODE in its environment; unset it so we can launch
# Claude subprocesses for testing without triggering the nested-session guard.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ONLY_SCENARIO=""
CRED_TOOL="$SCRIPT_DIR/lib/claude_credentials.py"

# Isolated home for this test run — Claude and nx configs go here, not ~/.claude
TEST_HOME="${TMPDIR%/}/nexus-e2e-home"
TEST_HOME="${TEST_HOME:-/tmp/nexus-e2e-home}"
export TEST_HOME REPO_ROOT

# Private tmux socket for THIS run only (RDR-219 tmux trap, nexus-wauo1.11
# item 6): a tmux session takes its environment from the tmux SERVER, not
# from the command that asks for the session. A fresh, PID-scoped socket
# guarantees the `new-session` call below actually spawns a new server —
# never adds this session to a server that's already running on the shared
# default socket, which would silently leave the automation token out of
# the pane. lib.sh's `_tmux()` wrapper (sourced below) honors this for
# every tmux call scenarios make; the calls in this script route through it
# too.
NX_TMUX_SOCKET="nexus-e2e-$$"
export NX_TMUX_SOCKET

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

# Force local mode for the whole harness unless the caller explicitly
# opts into cloud by setting NX_LOCAL=0. Without this, ``crun`` (used by
# scenarios for direct ``nx`` invocations) inherits the outer shell's
# ambient CHROMA_API_KEY / CHROMA_TENANT and hits production. The
# sandbox goal is "not production."
export NX_LOCAL="${NX_LOCAL:-1}"
if [[ "$NX_LOCAL" == "1" ]]; then
    unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
fi

# Accept either a real ``ANTHROPIC_API_KEY`` in the environment OR a
# present, unexpired automation token in the keychain (RDR-219: the
# harness's own identity, `nexus-automation-oauth-token`, never the
# operator's interactive login). A placeholder API key WITH a real
# automation token is worse than no key — Claude Code prefers an explicit
# env-var key and rejects the placeholder with "Invalid API key" on every
# request.
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    if ! CRED_STATUS="$(python3 "$CRED_TOOL" status)"; then
        echo "Error: neither ANTHROPIC_API_KEY nor the automation token is usable." >&2
        echo "  $CRED_STATUS" >&2
        echo "  Set ANTHROPIC_API_KEY in .env, or run \`claude setup-token\` and store the" >&2
        echo "  result in keychain item 'nexus-automation-oauth-token' (RDR-219)." >&2
        exit 1
    fi
fi
: "${VOYAGE_API_KEY:?'VOYAGE_API_KEY must be set'}"

# ─── Source helpers ───────────────────────────────────────────────────────────

source "$SCRIPT_DIR/lib.sh"

# ─── Cleanup on exit ──────────────────────────────────────────────────────────

cleanup() {
    echo ""
    echo "Cleaning up tmux session..."
    _tmux kill-session -t e2e 2>/dev/null || true
    # RDR-219 (nexus-wauo1.19): the private-socket tmux SERVER started above
    # (via `$CRED_TOOL run -- tmux -L "$NX_TMUX_SOCKET" new-session ...`)
    # carries CLAUDE_CODE_OAUTH_TOKEN in its own environment -- the tmux
    # trap: a server's environment is fixed at server start, not per
    # session. kill-session alone tears down only the pane; the server, and
    # the token inside its environment, would otherwise linger until an
    # unrelated reaper or reboot. Kill it too, via _tmux so this only ever
    # targets the private socket ($NX_TMUX_SOCKET is unconditionally
    # exported above) and never the shared default socket.
    _tmux kill-server 2>/dev/null || true
    rm -rf "$TEST_HOME"
}
trap cleanup EXIT

# ─── Set up isolated test home ────────────────────────────────────────────────

echo "Setting up isolated test home at $TEST_HOME..."
rm -rf "$TEST_HOME"
mkdir -p "$TEST_HOME/.claude/plugins"

# No credential file is written here (RDR-219 rule 1: a harness never
# touches ~/.claude/.credentials.json, the operator's own interactive
# login). Authentication happens via CLAUDE_CODE_OAUTH_TOKEN in the
# environment of the tmux server started below — the oauthAccount seed
# (tests/e2e/.claude-auth/claude.json) isn't needed either: Phase 0's A1
# spike (T2 nexus_rdr/219-research-9) confirmed this exact isolated-HOME
# interactive-tmux launch shape passes with a plain onboarding-only stub.
echo '{"hasCompletedOnboarding":true}' > "$TEST_HOME/.claude.json"

# Register plugins so Claude Code discovers and loads them.
# Claude Code uses two files:
#   ~/.claude/plugins/installed_plugins.json  — registry of installed plugins
#   ~/.claude/settings.json                  — enabledPlugins + permissions
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"

# Build installed_plugins.json (conexus only).
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" << PLUGINS_EOF
{
  "version": 2,
  "plugins": {
    "conexus@nexus-plugins": [
      {
        "scope": "user",
        "installPath": "$REPO_ROOT/conexus",
        "version": "dev",
        "installedAt": "$NOW",
        "lastUpdated": "$NOW"
      }
    ]
  }
}
PLUGINS_EOF

# Write settings.json: enable conexus plugin and skip the "dangerous mode" confirmation
# dialog so claude_start doesn't need to navigate it.
cat > "$TEST_HOME/.claude/settings.json" << SETTINGS_EOF
{
  "enabledPlugins": {
    "conexus@nexus-plugins": true
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
HOME="$TEST_HOME" "$REAL_UV" tool install "$REPO_ROOT" --force --python 3.12 2>&1 | tail -5
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
# ANTHROPIC_API_KEY (RDR-219 Phase 2 Step 2, mechanism 9): never written to
# this file. When a CI caller sets it, it is already in run.sh's own
# process environment and so already reaches the tmux SERVER's environment
# (and every pane) through the \`\$CRED_TOOL run -- tmux ... new-session\`
# call above — \`run\` execs with a copy of the caller's own os.environ plus
# the automation token, so the pane already has the key before .env.test is
# sourced. Writing the value here too would put a plaintext credential on
# disk for no operational reason. When it's NOT set, explicitly unset it so
# Claude Code falls through to the automation token (CLAUDE_CODE_OAUTH_TOKEN)
# already in the tmux server's own environment — a stray export here would
# make Claude prefer a bogus/inherited key over the token and reject every
# request with "Invalid API key."
EOF
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "unset ANTHROPIC_API_KEY" >> "$TEST_HOME/.env.test"
fi
cat >> "$TEST_HOME/.env.test" << EOF
# NX_LOCAL=1 by default so the sandbox uses \`\`chromadb.PersistentClient\`\`
# + local ONNX embeddings instead of the cloud tenant configured in the
# real .env. Otherwise an ambient CHROMA_API_KEY / CHROMA_TENANT bleeds
# into the harness and ``nx index`` hits production. Override by
# exporting NX_LOCAL=0 before invoking run.sh.
export NX_LOCAL="\${NX_LOCAL:-1}"
export VOYAGE_API_KEY="${VOYAGE_API_KEY:-}"
# Only forward cloud credentials when explicitly NOT in local mode, so
# NX_LOCAL=1 stays cleanly offline from production.
if [[ "\$NX_LOCAL" != "1" ]]; then
    export CHROMA_API_KEY="${CHROMA_API_KEY:-}"
    export CHROMA_TENANT="${CHROMA_TENANT:-}"
    export CHROMA_DATABASE="${CHROMA_DATABASE:-default_database}"
else
    unset CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE
fi
export NEXUS_SESSION_ID="$NEXUS_SESSION_ID"
cd "$REPO_ROOT"
EOF
chmod 600 "$TEST_HOME/.env.test"

# ─── Start tmux session ───────────────────────────────────────────────────────

echo "Starting tmux session 'e2e' on private socket '$NX_TMUX_SOCKET'..."
echo "  (Run 'tmux -L $NX_TMUX_SOCKET attach -t e2e' in another terminal to watch)"

_tmux kill-session -t e2e 2>/dev/null || true
# RDR-219 tmux trap (item 6): this new-session call is what actually spawns
# the server on a fresh private socket, so it — and only it — needs to run
# under claude_credentials.py `run --`: that's what gets
# CLAUDE_CODE_OAUTH_TOKEN into the SERVER's own environment, which every
# pane's shell then inherits. Every later tmux call in this harness targets
# the same already-running server via _tmux/NX_TMUX_SOCKET and needs no
# further wrapping.
python3 "$CRED_TOOL" run -- tmux -L "$NX_TMUX_SOCKET" new-session -d -s e2e -x 220 -y 50

# Source the env file in the pane so subsequent commands use TEST_HOME
_tmux send-keys -t "e2e" "source $TEST_HOME/.env.test" Enter
sleep 1

# Suppress zsh new-user wizard (would absorb keystrokes before Claude starts)
touch "$TEST_HOME/.zshrc"

# ─── Run scenarios ────────────────────────────────────────────────────────────

SCENARIOS_RUN=0

run_scenario() {
    local file="$1"
    local num
    num=$(basename "$file" | cut -d_ -f1)
    if [[ -n "$ONLY_SCENARIO" && "$num" != "$ONLY_SCENARIO" ]]; then
        return 0
    fi
    SCENARIOS_RUN=$((SCENARIOS_RUN + 1))
    echo ""
    echo "══════════════════════════════════════════════"
    # shellcheck source=/dev/null
    source "$file"
}

for scenario_file in "$SCRIPT_DIR"/scenarios/[0-9]*.sh; do
    run_scenario "$scenario_file"
done

# nexus-poplq: executed-work floor. A typo'd/renamed/deleted --scenario used
# to match nothing, source nothing, and exit 0 on "Results: 0 passed,
# 0 failed" — a run that executed nothing was indistinguishable from a run
# that passed everything (success-shaped emptiness).
if [[ $SCENARIOS_RUN -eq 0 ]]; then
    echo ""
    echo "ERROR: no scenario matched${ONLY_SCENARIO:+ --scenario '$ONLY_SCENARIO'} — nothing ran, so this run proves nothing." >&2
    echo "Available scenarios:" >&2
    ls "$SCRIPT_DIR"/scenarios/[0-9]*.sh 2>/dev/null | sed 's/^/  /' >&2
    exit 1
fi
if [[ $((PASS + FAIL + SKIP)) -eq 0 ]]; then
    echo ""
    echo "ERROR: $SCENARIOS_RUN scenario(s) ran but made ZERO assertions — a green verdict over no checks is not a pass (nexus-poplq)." >&2
    exit 1
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

summary
