#!/usr/bin/env bash
# Scenario 17 — can a context:fork skill's body invoke the Agent tool?
# If yes, the original plan B (rdr-gate / substantive-critique with context:fork)
# is viable. If no, B is structurally impossible and must be dropped.

scenario "17 fork_can_spawn_subagent: does a forked subagent itself dispatch via Agent?"

# nexus-qsmc5: the old skill body told the nested agent "Reply with the literal
# token NESTED-AGENT-OK" and the verdict grepped the pane for that token. The
# pane shows the fork's Task call with its prompt, so the token was on screen as
# soon as the fork TRIED to dispatch, whatever the nested agent did. The tokens are
# now spelled in pieces, and the verdict reads the subagents' own transcripts: the
# nested agent's reply must be there AND a subagent must have called the Task/Agent
# tool (cc_subagent_tool_uses), which the fork's reply text cannot fake.
#
# The Task/Agent tool is DEFERRED for a fork (its transcript shows "ToolSearch
# select:Task" loading it): a run whose fork did not load it first answered
# NESTED-SPAWN-BLOCKED, the next run loaded it and spawned, so the verdict flipped
# on whether the model thought of ToolSearch. Step 0 of the skill body now says to
# load it, and BLOCKED only after that (README "Deferred MCP tools" regime).
write_skill "fork-spawn-test" /dev/stdin <<'EOF'
---
name: fork-spawn-test
description: Validation skill — tests whether a forked subagent can dispatch another agent via Task tool
context: fork
agent: general-purpose
---

You are validating that a forked subagent can itself use the Task tool to dispatch a nested agent.

Do exactly this:
0. The Task tool (it may be listed as Agent) is a DEFERRED tool: if it is not directly callable, first use ToolSearch with the query select:Agent to load its schema.
1. Use the Task tool to dispatch the general-purpose agent. Description: 'nested-spawn-test'. Prompt: 'Do not use any tools. Reply with the word NESTED immediately followed by -AGENT-OK-17 (no space).'
2. After the nested agent returns, reply with the word FORK immediately followed by -PARENT-OK-17 (no space), then a space, then whatever the nested agent told you.
3. If the Task tool is still unavailable to you after step 0 OR the dispatch fails, reply with the word NESTED immediately followed by -SPAWN-BLOCKED-17 (no space).
EOF

cat > "$TEST_HOME/.claude/settings.json" <<'EOF'
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Skill", "Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
claude_prompt "Invoke the fork-spawn-test skill via the Skill tool."
cc_wait_subagent_reply "NESTED-AGENT-OK-17|NESTED-SPAWN-BLOCKED-17" 240 || true
REPLIES17="$(cc_subagent_replies || true)"
TOOLS17="$(cc_subagent_tool_uses || true)"
DISPATCHED17=0
grep -qE "^(Task|Agent)$" <<<"$TOOLS17" && DISPATCHED17=1

if grep -qE "NESTED-AGENT-OK-17" <<<"$REPLIES17" && [[ $DISPATCHED17 -eq 1 ]]; then
    pass "forked subagent CAN spawn nested subagents (a subagent called the Task/Agent tool and the nested agent replied) → context:fork viable for orchestrator skills"
elif grep -qE "NESTED-SPAWN-BLOCKED-17" <<<"$REPLIES17"; then
    pass "forked subagent CANNOT spawn nested subagents → context:fork unsuitable for skills that use Agent tool"
elif grep -qE "NESTED-AGENT-OK-17" <<<"$REPLIES17"; then
    fail "a subagent reply carries the nested token but no subagent called the Task/Agent tool (tools called by subagents: ${TOOLS17:-none}) — not evidence of a nested spawn"
else
    fail "indeterminate — neither NESTED-AGENT-OK-17 nor NESTED-SPAWN-BLOCKED-17 in any subagent reply"
    echo "$REPLIES17" | tail -5 | sed 's/^/    | /'
fi

cc_keep_evidence "17"
claude_exit
scenario_end
