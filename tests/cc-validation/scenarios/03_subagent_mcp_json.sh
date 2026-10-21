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

CC_DEBUG_FILE="$TEST_HOME/cc-debug-03.log"; rm -f "$CC_DEBUG_FILE"
claude_start
# nexus-qsmc5: reply tokens in pieces, the marker's code absent from the prompt,
# and the verdict read from the SUBAGENT's own transcript (see scenario 01).
claude_prompt "Use Task to dispatch the general-purpose agent. Description='subagent mcp inject probe'. Prompt for the subagent: 'Do not use any tools. Examine your context, apart from this message. Is the text SUB-MCP-MARKER- immediately followed by a four-character code (no space) anywhere in it? If yes, reply with the word FOUND immediately followed by -03S (no space), then a space, then that whole text including its code. If no, reply with the word NONE immediately followed by -03S (no space).'"
cc_wait_subagent_reply "FOUND-03S|NONE-03S" 180 || true
REPLIES03="$(cc_subagent_replies || true)"

if grep -qE "FOUND-03S.*SUB-MCP-MARKER-W7K4" <<<"$REPLIES03"; then
    pass "mcp_tool returning additionalContext JSON injected into subagent"
else
    fail "mcp_tool injection did NOT work — contradicts -p finding (the subagent did not report the marker line)"
    echo "$REPLIES03" | tail -5 | sed 's/^/    | /'
fi

cc_report_context_lines
cc_keep_evidence "03"
claude_exit
send_keys "cd $REPO_ROOT" Enter
sleep 0.3
scenario_end
