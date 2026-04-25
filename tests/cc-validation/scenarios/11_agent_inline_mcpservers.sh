#!/usr/bin/env bash
# Scenario 11 — agent frontmatter inline mcpServers should scope server to subagent.
# Forensic version: ask agent to report its actual tool inventory + actually
# write proof to a file (not just claim a tool was called).

scenario "11 agent_inline_mcpservers: inline mcpServers scopes to subagent"

# Agent reports tool list AND writes proof to a file we can inspect
write_agent "scoped-tool-agent" /dev/stdin <<EOF
---
name: scoped-tool-agent
description: Validation agent — has inline mcpServers, reports forensic evidence
mcpServers:
  - stub:
      type: stdio
      command: python3
      args: ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"]
      env:
        STUB_LOG: "$STUB_LOG"
        STUB_NAME: "stub"
tools: [mcp__stub__ping, mcp__stub__record, Write]
---

You are validating mcpServers scoping. Do EXACTLY this:

1. Use the Write tool to save your full available tool inventory to ${TEST_HOME}/agent_tools.txt — one tool name per line, including any mcp__ tools.
2. Then call mcp__stub__record with payload='agent-scoped-XYZ-PROOF'.
3. Reply with the literal text AGENT-PROOF-DONE.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/agent_tools.txt"

# Verify the agent file was written with substituted paths
echo "    --- agent file path expansion check ---"
grep -E "STUB_LOG|args" "$TEST_HOME/.claude/agents/scoped-tool-agent.md" | head -3 | sed 's/^/    | /'

claude_start

# Parent should NOT have stub server. Capture parent's tool view first.
claude_prompt "List all available MCP tools (any starting with mcp__). Reply with the list, one tool per line. If none, reply NO-MCP-TOOLS."
claude_wait 30
parent_capture=$(capture -50)

claude_prompt "Use the Task tool to dispatch the scoped-tool-agent. Description='scope test'. Prompt: 'Run your instructions exactly.'"
claude_wait 90

# Forensic checks
parent_saw_stub=0
echo "$parent_capture" | grep -qE "mcp__stub__" && parent_saw_stub=1

agent_listed_stub=0
[[ -f "$TEST_HOME/agent_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/agent_tools.txt" && agent_listed_stub=1

agent_actually_called_stub=0
[[ -s "$STUB_LOG" ]] && grep -q "agent-scoped-XYZ-PROOF" "$STUB_LOG" && agent_actually_called_stub=1

echo "    parent_saw_stub=$parent_saw_stub"
echo "    agent_tools.txt_exists=$([[ -f $TEST_HOME/agent_tools.txt ]] && echo 1 || echo 0)"
echo "    agent_listed_stub=$agent_listed_stub"
echo "    agent_actually_called_stub=$agent_actually_called_stub"

if [[ -f "$TEST_HOME/agent_tools.txt" ]]; then
    echo "    --- agent's tool inventory (first 20 lines) ---"
    head -20 "$TEST_HOME/agent_tools.txt" | sed 's/^/    | /'
fi

if [[ $parent_saw_stub -eq 0 && $agent_listed_stub -eq 1 && $agent_actually_called_stub -eq 1 ]]; then
    pass "inline mcpServers fully scoped — parent invisible, agent sees+uses"
elif [[ $parent_saw_stub -eq 0 && $agent_listed_stub -eq 0 ]]; then
    fail "inline mcpServers did NOT load for agent — server not actually started"
elif [[ $agent_listed_stub -eq 1 && $agent_actually_called_stub -eq 0 ]]; then
    fail "agent saw the tool but call did not register — permission or runtime issue"
else
    fail "scoping result mixed — investigate"
fi

claude_exit
scenario_end
