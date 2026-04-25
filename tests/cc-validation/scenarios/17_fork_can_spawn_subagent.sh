#!/usr/bin/env bash
# Scenario 17 — can a context:fork skill's body invoke the Agent tool?
# If yes, the original plan B (rdr-gate / substantive-critique with context:fork)
# is viable. If no, B is structurally impossible and must be dropped.

scenario "17 fork_can_spawn_subagent: does a forked subagent itself dispatch via Agent?"

write_skill "fork-spawn-test" /dev/stdin <<'EOF'
---
name: fork-spawn-test
description: Validation skill — tests whether a forked subagent can dispatch another agent via Task tool
context: fork
agent: general-purpose
---

You are validating that a forked subagent can itself use the Task tool to dispatch a nested agent.

Do exactly this:
1. Use the Task tool to dispatch the general-purpose agent. Description: 'nested-spawn-test'. Prompt: 'Reply with the literal token NESTED-AGENT-OK.'
2. After the nested agent returns, reply with the literal token FORK-PARENT-OK followed by whatever the nested agent told you.
3. If the Task tool is unavailable to you OR the dispatch fails, reply NESTED-SPAWN-BLOCKED.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<'EOF'
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Skill", "Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
claude_prompt "Invoke the fork-spawn-test skill via the Skill tool."
claude_wait 120

if capture -300 | grep -qE "NESTED-AGENT-OK"; then
    pass "forked subagent CAN spawn nested subagents → context:fork viable for orchestrator skills"
elif capture -300 | grep -qE "NESTED-SPAWN-BLOCKED"; then
    pass "forked subagent CANNOT spawn nested subagents → context:fork unsuitable for skills that use Agent tool"
else
    fail "indeterminate — neither marker visible"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
scenario_end
