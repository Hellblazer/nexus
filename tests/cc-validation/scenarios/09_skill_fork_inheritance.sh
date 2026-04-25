#!/usr/bin/env bash
# Scenario 09 — does context:fork inherit prior conversation context?
# Multi-turn: parent first sets a token via prompt, then invokes the skill
# in a SECOND prompt. The forked skill should see the prior turn if it inherits.

scenario "09 skill_fork_inheritance: does context:fork inherit prior conversation?"

write_skill "inherit-probe" /dev/stdin <<'EOF'
---
name: inherit-probe
description: Validation skill — checks if forked subagent sees prior parent conversation
context: fork
agent: general-purpose
---

Examine all of your context, including any prior conversation messages.
If you see a token of the form INHERIT-TOKEN-XXXXX (5 hex digits), reply with the EXACT token.
If you see no such token, reply NO-INHERIT.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Skill", "Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
# Turn 1: plant the token
claude_prompt "Please remember this session token from our conversation: INHERIT-TOKEN-A1B2C. Acknowledge with the single word ACK."
claude_wait 30
# Turn 2: invoke the skill
claude_prompt "Now invoke the inherit-probe skill via the Skill tool."
claude_wait 90

if capture -300 | grep -qE "INHERIT-TOKEN-A1B2C" && ! capture -100 | grep -qE "NO-INHERIT"; then
    pass "fork DID inherit conversation context (skill saw the token from turn 1)"
elif capture -300 | grep -qE "NO-INHERIT"; then
    pass "fork did NOT inherit conversation context (confirms -p finding)"
else
    fail "indeterminate"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
scenario_end
