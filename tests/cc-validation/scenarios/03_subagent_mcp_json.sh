#!/usr/bin/env bash
# Scenario 03 — SubagentStart type:mcp_tool returning the additionalContext JSON
# should inject in interactive mode.

scenario "03 subagent_mcp_json: mcp_tool returning JSON contract should inject"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "mcp_tool", "server": "stub", "tool": "emit_inject_json",
                    "input": { "marker": "SUB-MCP-MARKER-W7K4" } }] }
    ]
  }
}
EOF
# MCP servers must live in .mcp.json at the workspace root, NOT in settings.json
cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG", "STUB_NAME": "stub" } }
} }
EOF
# Tell claude to cd to TEST_HOME so .mcp.json is at the workspace root for this scenario
send_keys "cd $TEST_HOME" Enter
sleep 0.3

claude_start
claude_prompt "Use Task to dispatch the general-purpose agent. Description='subagent mcp inject probe'. Prompt: 'Examine your context. Quote any line beginning with SUB-MCP-MARKER- exactly. If none, reply NO-MARKER.'"
claude_wait 90

if capture -300 | grep -qE "SUB-MCP-MARKER-W7K4"; then
    pass "mcp_tool returning additionalContext JSON injected into subagent"
else
    fail "mcp_tool injection did NOT work — contradicts -p finding"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter
sleep 0.3
scenario_end
