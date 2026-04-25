#!/usr/bin/env bash
# Scenario 08 — skill with context: fork should dispatch as a forked subagent
# (proven in -p; confirm interactively).

scenario "08 skill_fork_dispatch: context:fork should dispatch subagent"

write_skill "fork-dispatch" /dev/stdin <<'EOF'
---
name: fork-dispatch
description: Validation skill — should dispatch as forked subagent
context: fork
agent: general-purpose
---

You are validating that context: fork dispatched you as a subagent.
Reply with the literal token: FORK-DISPATCH-OK-T7K
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Skill", "Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
claude_prompt "Invoke the fork-dispatch skill via the Skill tool (skill name: fork-dispatch)."
claude_wait 90

if capture -300 | grep -qE "forked execution|FORK-DISPATCH-OK-T7K"; then
    pass "skill context:fork dispatched as forked subagent"
else
    fail "no fork dispatch evidence"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
scenario_end
