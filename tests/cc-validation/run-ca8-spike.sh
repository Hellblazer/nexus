#!/usr/bin/env bash
# run-ca8-spike.sh — interactive runner for the RDR-111 CA-8 spike
# (nexus-yi08) using the release-sandbox tmux pattern.
#
# Each invocation runs ONE sub-run and prints its result. Invoke three
# times back-to-back to gather A / B / C:
#
#   ./tests/cc-validation/run-ca8-spike.sh A   # allow first, block second
#   ./tests/cc-validation/run-ca8-spike.sh B   # block first, allow second
#   ./tests/cc-validation/run-ca8-spike.sh C   # allow only (baseline)
#
# Each invocation:
#   - reuses $HOME/nexus-sandbox (or creates it on first run via
#     ``release-sandbox.sh tmux`` setup); the wheel-install matters
#     because Claude Code resolves MCP server registration through the
#     installed plugin paths, not the source tree
#   - writes the sub-run-specific settings.json + .mcp.json + hooks
#   - launches a fresh tmux session, sends a prompt that triggers
#     ``mcp__stub__ping``, waits for completion, then captures the
#     hook + stub logs
#
# On completion paste the printed result block into the conversation
# so it can be persisted to T2 (``nexus_rdr/111-research-CA-8-spike``).

set -euo pipefail

SUB_RUN="${1:-}"
case "$SUB_RUN" in
    A|B|C) ;;
    *) echo "usage: $0 {A|B|C}" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SANDBOX="$HOME/nexus-sandbox"
TMUX_SESSION="ca8-spike-$SUB_RUN"
AUTH_DIR="$REPO_ROOT/tests/e2e/.claude-auth"

if [[ ! -f "$AUTH_DIR/.credentials.json" ]]; then
    echo "ERROR: missing $AUTH_DIR/.credentials.json — run tests/e2e/auth-login.sh first" >&2
    exit 1
fi

# ─── First-time install via release-sandbox.sh ────────────────────────────────
# Reuse the installed sandbox across sub-runs so we don't reinstall the
# wheel three times. Mark the install with a marker file.
INSTALL_MARKER="$SANDBOX/.ca8-spike-installed"
if [[ ! -f "$INSTALL_MARKER" ]]; then
    echo "[setup] First sub-run: running release-sandbox.sh smoke to install ..."
    # smoke mode installs without launching tmux; tmux mode would block.
    "$REPO_ROOT/tests/e2e/release-sandbox.sh" smoke >/dev/null 2>&1 || true
    # smoke leaves SANDBOX populated; if it didn't, fall back to sandbox.sh.
    if [[ ! -d "$SANDBOX/.claude" ]]; then
        echo "[setup] smoke didn't populate sandbox; falling back to sandbox.sh ..."
        "$REPO_ROOT/tests/e2e/sandbox.sh" >/dev/null 2>&1
    fi
    touch "$INSTALL_MARKER"
    cp "$AUTH_DIR/.credentials.json" "$SANDBOX/.claude/.credentials.json"
    echo "[setup] sandbox ready at $SANDBOX"
fi

mkdir -p "$SANDBOX/.claude"

# ─── Hook scripts ─────────────────────────────────────────────────────────────
HOOK_LOG="$SANDBOX/hook.log"
STUB_LOG="$SANDBOX/stub_calls.log"
: > "$HOOK_LOG"
: > "$STUB_LOG"

# Bake HOOK_LOG path directly into the hook scripts — Claude Code does
# not propagate the harness's environment to PreToolUse command hooks,
# so a ``$HOOK_LOG`` env-var lookup silently fails the redirect.
cat > "$SANDBOX/.claude/hook_allow.sh" <<EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s%N)] HOOK_ALLOW_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}))'
EOF
chmod +x "$SANDBOX/.claude/hook_allow.sh"

cat > "$SANDBOX/.claude/hook_block.sh" <<EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s%N)] HOOK_BLOCK_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"block","permissionDecisionReason":"CA-8 spike: block hook"}}))'
EOF
chmod +x "$SANDBOX/.claude/hook_block.sh"

# ─── settings.json per sub-run ───────────────────────────────────────────────
case "$SUB_RUN" in
    A)
        HOOKS_BLOCK=$(cat <<EOF
"PreToolUse": [
  { "matcher": "mcp__stub__.*",
    "hooks": [{ "type": "command", "command": "bash $SANDBOX/.claude/hook_allow.sh" }] },
  { "matcher": "mcp__stub__.*",
    "hooks": [{ "type": "command", "command": "bash $SANDBOX/.claude/hook_block.sh" }] }
]
EOF
)
        ;;
    B)
        HOOKS_BLOCK=$(cat <<EOF
"PreToolUse": [
  { "matcher": "mcp__stub__.*",
    "hooks": [{ "type": "command", "command": "bash $SANDBOX/.claude/hook_block.sh" }] },
  { "matcher": "mcp__stub__.*",
    "hooks": [{ "type": "command", "command": "bash $SANDBOX/.claude/hook_allow.sh" }] }
]
EOF
)
        ;;
    C)
        HOOKS_BLOCK=$(cat <<EOF
"PreToolUse": [
  { "matcher": "mcp__stub__.*",
    "hooks": [{ "type": "command", "command": "bash $SANDBOX/.claude/hook_allow.sh" }] }
]
EOF
)
        ;;
esac

cat > "$SANDBOX/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "default" },
  "hooks": { $HOOKS_BLOCK }
}
EOF

# ─── .mcp.json at sandbox root ────────────────────────────────────────────────
# The stub server imports ``mcp.server.fastmcp`` — needs the conexus
# venv python where that package lives. System python3 lacks it.
STUB_PYTHON="$HOME/.local/share/uv/tools/conexus/bin/python3"
if [[ ! -x "$STUB_PYTHON" ]]; then
    # Fallback to the repo's editable venv if the tool venv isn't installed.
    STUB_PYTHON="$REPO_ROOT/.venv/bin/python"
fi
if [[ ! -x "$STUB_PYTHON" ]]; then
    echo "ERROR: no Python with the 'mcp' package found at" >&2
    echo "       $HOME/.local/share/uv/tools/conexus/bin/python3" >&2
    echo "    or $REPO_ROOT/.venv/bin/python" >&2
    echo "Run scripts/reinstall-tool.sh first." >&2
    exit 1
fi
cat > "$SANDBOX/.mcp.json" <<EOF
{
  "mcpServers": {
    "stub": { "type": "stdio", "command": "$STUB_PYTHON",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
  }
}
EOF

# ─── Launch tmux + claude ────────────────────────────────────────────────────
# Use cc-validation lib primitives for claude_start / claude_prompt / etc.
export HOOK_LOG STUB_LOG TMUX_SESSION
export TEST_HOME="$SANDBOX"
# shellcheck source=/dev/null
. "$REPO_ROOT/tests/e2e/lib.sh"

tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
echo "[run $SUB_RUN] starting tmux session '$TMUX_SESSION' (HOME=$SANDBOX) ..."
tmux new-session -d -s "$TMUX_SESSION" -x 220 -y 50 \
    "env HOME='$SANDBOX' PATH='$SANDBOX/.local/bin:$PATH' bash -i"
sleep 1

# cd to SANDBOX so .mcp.json at workspace root is picked up
send_keys "cd $SANDBOX" Enter
sleep 0.3

# Run claude with --mcp-config + --strict-mcp-config to bypass the
# per-project MCP-approval dance.
send_keys "claude --dangerously-skip-permissions --mcp-config $SANDBOX/.mcp.json --strict-mcp-config" Enter

# Claude startup state machine — copy of claude_start's body since we
# already sent the launch line ourselves.
sleep 8
deadline=$(( $(date +%s) + 60 ))
_trust_done=0 _bypass_done=0 _login_done=0
while [[ $(date +%s) -lt $deadline ]]; do
    pane=$(capture)
    if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
        echo "    [auth] Workspace trust — accepting..."
        tmux send-keys -t "$TMUX_SESSION" Enter; _trust_done=1; sleep 2
    elif [[ $_bypass_done -eq 0 ]] && echo "$pane" | grep -qiE "Bypass Permissions|Yes, I accept"; then
        echo "    [auth] Bypass permissions — accepting..."
        tmux send-keys -t "$TMUX_SESSION" Down; sleep 0.5
        tmux send-keys -t "$TMUX_SESSION" Enter; _bypass_done=1; sleep 5
    elif echo "$pane" | grep -qiE "custom API key|Do you want to use this API key"; then
        tmux send-keys -t "$TMUX_SESSION" Enter; sleep 5
    elif [[ $_login_done -eq 0 ]] && echo "$pane" | grep -qiE "Select login|login method|How would you like"; then
        tmux send-keys -t "$TMUX_SESSION" Down; sleep 0.5
        tmux send-keys -t "$TMUX_SESSION" Enter; _login_done=1; sleep 6
    elif echo "$pane" | grep -qiE "bypass permissions on|Type a message"; then
        break
    fi
    sleep 1
done
sleep 5

# Issue the spike prompt.
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

claude_exit
sleep 2
tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true

# ─── Report ──────────────────────────────────────────────────────────────────
ALLOW_FIRED=0; grep -q HOOK_ALLOW_FIRED "$HOOK_LOG" && ALLOW_FIRED=1
BLOCK_FIRED=0; grep -q HOOK_BLOCK_FIRED "$HOOK_LOG" && BLOCK_FIRED=1
TOOL_RAN=0;    [[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && TOOL_RAN=1
FIRED_COUNT=0; [[ -s "$HOOK_LOG" ]] && FIRED_COUNT=$(grep -c FIRED "$HOOK_LOG")

echo ""
echo "═══════════════════════════════════════════════════"
echo "  CA-8 spike sub-run $SUB_RUN result"
echo "═══════════════════════════════════════════════════"
echo "  allow_fired = $ALLOW_FIRED"
echo "  block_fired = $BLOCK_FIRED"
echo "  tool_ran    = $TOOL_RAN"
echo "  fired_count = $FIRED_COUNT"
echo ""
if [[ "$FIRED_COUNT" -eq 0 ]]; then
    echo "WARN: zero hook firings. Likely the MCP server didn't register"
    echo "      or the prompt timed out. Inspect:"
    echo "        $HOOK_LOG"
    echo "        $STUB_LOG"
    echo "        last claude session JSONL in $SANDBOX/.claude/projects/"
fi
