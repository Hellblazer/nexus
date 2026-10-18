#!/usr/bin/env bash
# Scenario 17 — can a context:fork skill's body invoke the Agent tool?
# If yes, the original plan B (rdr-gate / substantive-critique with context:fork)
# is viable. If no, B is structurally impossible and must be dropped.

scenario "17 fork_can_spawn_subagent: does a forked subagent itself dispatch via Agent?"

# nexus-qsmc5: the old skill body told the nested agent "Reply with the literal
# token NESTED-AGENT-OK" and the verdict grepped the pane for that token. The
# pane shows the fork's Task call with its prompt, so the token was on screen as
# soon as the fork TRIED to dispatch, whatever the nested agent did. The tokens are
# now spelled in pieces, and the verdict reads the subagents' own transcripts (see below).
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
# Wait for the FORK's closing reply, not the nested agent's: the Task/Agent call's result is
# written into the fork's transcript after the nested agent has answered, and the facts
# below read that result.
cc_wait_subagent_reply "FORK-PARENT-OK-17|NESTED-SPAWN-BLOCKED-17" 240 || true
# nexus-qsmc5 review: what a subagent WROTE proves nothing about what it DID. The facts
# come from the subagents' transcripts (lib/agent_facts.py): a Task/Agent call that came
# back without is_error, the nested token in a transcript that made no such call (the
# leaf agent's own, never the fork's), and for BLOCKED whether the fork tried ToolSearch
# and never called the tool. Only this scenario's transcripts are read (reset_scenario_state
# clears the earlier scenarios').
REPLIES17="$(cc_subagent_replies || true)"
FACTS17="$(cc_nested_dispatch_facts "NESTED-AGENT-OK-17" "NESTED-SPAWN-BLOCKED-17" || true)"
fact17() { sed -n "s/^$1=//p" <<<"$FACTS17"; }
DISPATCHED17="$(fact17 dispatched)"; NESTED17="$(fact17 nested_elsewhere)"
BLOCKED17="$(fact17 blocked)"; SEARCHED17="$(fact17 blocked_searched)"; BLOCKEDCALL17="$(fact17 blocked_dispatched)"
TOOLS17="$(fact17 tools)"

if [[ "$DISPATCHED17" == "1" && "$NESTED17" == "1" ]]; then
    pass "forked subagent CAN spawn nested subagents (a subagent's Task/Agent call came back without error, and the nested token is in another subagent's own transcript) → context:fork viable for orchestrator skills"
elif [[ "$BLOCKED17" == "1" ]]; then
    if [[ "$SEARCHED17" == "1" && "$BLOCKEDCALL17" != "1" ]]; then
        pass "forked subagent CANNOT spawn nested subagents (it tried ToolSearch and never called the Task/Agent tool) → context:fork unsuitable for skills that use Agent tool"
    elif [[ "$BLOCKEDCALL17" == "1" ]]; then
        fail "a subagent answered NESTED-SPAWN-BLOCKED-17 although it called the Task/Agent tool (tools called by subagents: ${TOOLS17:-none}): not a block, and its dispatch did not produce the nested token"
    else
        fail "a subagent answered NESTED-SPAWN-BLOCKED-17 without trying ToolSearch to load the deferred Task/Agent tool (tools called by subagents: ${TOOLS17:-none}): the model giving up, not the tool being unavailable"
    fi
elif grep -qE "NESTED-AGENT-OK-17" <<<"$REPLIES17"; then
    fail "a subagent reply carries the nested token but there is no successful Task/Agent call in a subagent transcript plus the token in a different one (dispatched=${DISPATCHED17:-?}, nested_elsewhere=${NESTED17:-?}; tools called by subagents: ${TOOLS17:-none}): not evidence of a nested spawn"
else
    fail "indeterminate — neither NESTED-AGENT-OK-17 nor NESTED-SPAWN-BLOCKED-17 in any subagent reply"
    echo "$REPLIES17" | tail -5 | sed 's/^/    | /'
fi

cc_keep_evidence "17"
claude_exit
scenario_end
