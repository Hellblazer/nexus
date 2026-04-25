#!/usr/bin/env bash
# Scenario 05 — suffix wildcard mcp__server__* should grant permission interactively.

scenario "05 perms_wildcard_suffix: mcp__stub__* should auto-allow"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "default" }
}
EOF
cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOF
send_keys "cd $TEST_HOME" Enter
sleep 0.3

claude_start
claude_prompt "Call mcp__stub__record with payload='suffix-wildcard-test'. Reply DONE when complete."
claude_wait 90

if [[ -s "$STUB_LOG" ]] && grep -q "suffix-wildcard-test" "$STUB_LOG"; then
    pass "suffix wildcard granted permission, tool ran (stub log entry present)"
else
    fail "tool did NOT run — wildcard may not have granted permission"
    [[ -s "$STUB_LOG" ]] && cat "$STUB_LOG" | sed 's/^/    | /'
    capture -30 | sed 's/^/    | /'
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter
sleep 0.3
scenario_end
