#!/usr/bin/env bash
# Scenario 16 — PermissionRequest hook in `auto` mode (without --dangerously-skip-permissions).
# Tests whether the existing auto-approve-nx-mcp.sh would fire when Hal uses defaultMode:auto
# in his actual ~/.claude/settings.json. Also tests wildcard preemption interaction in auto mode.

# Custom claude launcher for this scenario — uses --permission-mode=auto, no bypass flag
claude_start_auto() {
    send_keys "claude --permission-mode=auto" Enter
    sleep 8
    local deadline=$(( $(date +%s) + 60 ))
    local _trust_done=0
    while [[ $(date +%s) -lt $deadline ]]; do
        local pane; pane=$(capture)
        if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
            echo "    [auth] trust — accept"
            tmux send-keys -t "${TMUX_SESSION}" Enter
            _trust_done=1; sleep 2
        elif echo "$pane" | grep -qiE "custom API key"; then
            tmux send-keys -t "${TMUX_SESSION}" Enter; sleep 5
        elif echo "$pane" | grep -qiE "Type a message|auto.*on|❯ "; then
            break
        fi
        sleep 1
    done
    sleep 5
}

# Simple PermissionRequest hook that logs and approves
cat > "$TEST_HOME/.claude/perm_hook.sh" <<'BASH_EOF'
#!/usr/bin/env bash
INPUT=$(cat)
echo "[$(date +%s)] PERM_HOOK: $INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}))'
BASH_EOF
chmod +x "$TEST_HOME/.claude/perm_hook.sh"

# Need .mcp.json at workspace root for stub MCP to load
cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOF

# ── 16a: auto mode with allow wildcard + PermissionRequest hook → does hook fire?
scenario "16a auto_with_allow: defaultMode=auto, allow wildcard PRESENT"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "auto" },
  "hooks": {
    "PermissionRequest": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/perm_hook.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
send_keys "cd $TEST_HOME" Enter; sleep 0.3
claude_start_auto
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

hook_fired_a=0
[[ -s "$HOOK_LOG" ]] && hook_fired_a=1
tool_ran_a=0
[[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_a=1

echo "    16a: hook_fired=$hook_fired_a  tool_ran=$tool_ran_a"
claude_exit

# ── 16b: auto mode WITHOUT allow wildcard → hook should fire on permission request
scenario "16b auto_without_allow: defaultMode=auto, NO allow rule"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "permissions": { "allow": [], "defaultMode": "auto" },
  "hooks": {
    "PermissionRequest": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/perm_hook.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
claude_start_auto
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

hook_fired_b=0
[[ -s "$HOOK_LOG" ]] && hook_fired_b=1
tool_ran_b=0
[[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_b=1

echo "    16b: hook_fired=$hook_fired_b  tool_ran=$tool_ran_b"
claude_exit
send_keys "cd $REPO_ROOT" Enter; sleep 0.3

# ── verdict
echo ""
echo "    ──────────── 16 verdict (auto mode) ────────────"
echo "    16a (with allow):    hook_fired=$hook_fired_a  tool_ran=$tool_ran_a"
echo "    16b (without allow): hook_fired=$hook_fired_b  tool_ran=$tool_ran_b"
if [[ $hook_fired_a -eq 0 && $hook_fired_b -eq 0 ]]; then
    pass "auto mode auto-approves MCP tools without consulting PermissionRequest hook either way"
elif [[ $hook_fired_a -eq 0 && $hook_fired_b -eq 1 ]]; then
    pass "wildcard PREEMPTS hook in auto mode (hook fires only when no rule matches)"
elif [[ $hook_fired_a -eq 1 && $hook_fired_b -eq 1 ]]; then
    pass "hook fires regardless in auto mode — auto-approve hook still executes redundantly with wildcard"
else
    fail "unexpected: hook fires WITH allow but not WITHOUT"
fi

rm -f "$TEST_HOME/.mcp.json"
scenario_end
