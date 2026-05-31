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

# VALIDITY NOTE (reworked 2026-05-31): the prior version detected parent leakage
# by asking the parent to LIST its MCP tools and grepping the reply — a model
# self-report, which is unreliable (the parent claimed mcp__stub__ even with no
# such tool, producing the "scoping result mixed" non-result). Test the parent's
# ACTUAL access forensically: ask the parent to CALL the stub with a parent-
# specific payload and check STUB_LOG. A landed payload is hard proof the parent
# holds the tool; its absence is proof it does not.
claude_prompt "Attempt to call the mcp__stub__record tool with payload='PARENT-CALL-PROBE'. If that tool is not available to you, reply PARENT-NO-STUB."
claude_wait 30
parent_called_stub=0
[[ -s "$STUB_LOG" ]] && grep -q "PARENT-CALL-PROBE" "$STUB_LOG" && parent_called_stub=1

claude_prompt "Use the Task tool to dispatch the scoped-tool-agent. Description='scope test'. Prompt: 'Run your instructions exactly.'"
claude_wait 90

# The agent's actual use is the deterministic precondition (STUB_LOG, not self-report).
agent_called_stub=0
[[ -s "$STUB_LOG" ]] && grep -q "agent-scoped-XYZ-PROOF" "$STUB_LOG" && agent_called_stub=1

echo "    parent_called_stub=$parent_called_stub (forensic: parent's own call landed in STUB_LOG)"
echo "    agent_called_stub=$agent_called_stub  (forensic: agent's call landed in STUB_LOG)"
if [[ -f "$TEST_HOME/agent_tools.txt" ]]; then
    echo "    --- agent's tool inventory (first 20 lines) ---"
    head -20 "$TEST_HOME/agent_tools.txt" | sed 's/^/    | /'
fi

if [[ $agent_called_stub -eq 0 ]]; then
    fail "agent did NOT use the stub — inline mcpServers did not load for the subagent (precondition failed)"
elif [[ $parent_called_stub -eq 0 ]]; then
    pass "inline mcpServers SCOPED to subagent — agent used the stub, parent forensically could not"
else
    pass "inline mcpServers NOT scoped — both parent and subagent call the stub (forensic; documents real CC behavior)"
fi

claude_exit
scenario_end
