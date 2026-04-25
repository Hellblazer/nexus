#!/usr/bin/env bash
# Scenario 10 — same as 09 but with CLAUDE_CODE_FORK_SUBAGENT=1 set in the
# tmux pane env BEFORE claude_start. Tests whether the env var (interactive-only
# per docs) actually enables conversation-context inheritance.

scenario "10 envvar_fork_inheritance: with CLAUDE_CODE_FORK_SUBAGENT=1, does fork inherit?"

write_skill "inherit-probe-env" /dev/stdin <<'EOF'
---
name: inherit-probe-env
description: Validation skill — checks fork inheritance with env var enabled
context: fork
agent: general-purpose
---

Examine all of your context, including any prior conversation messages.
If you see a token of the form ENVV-TOKEN-XXXXX (5 hex digits), reply with the EXACT token.
If none, reply NO-INHERIT.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Skill", "Task"], "defaultMode": "acceptEdits" }
}
EOF

# Inject env var into tmux pane env BEFORE claude_start
send_keys "export CLAUDE_CODE_FORK_SUBAGENT=1" Enter
sleep 0.5
claude_start
claude_prompt "Please remember this session token: ENVV-TOKEN-A1B2C. Acknowledge with ACK."
claude_wait 30
claude_prompt "Now invoke the inherit-probe-env skill via the Skill tool."
claude_wait 90

if capture -300 | grep -qE "ENVV-TOKEN-A1B2C" && ! capture -100 | grep -qE "NO-INHERIT"; then
    pass "WITH env var, fork DID inherit context (env var works as docs claim)"
elif capture -300 | grep -qE "NO-INHERIT"; then
    pass "WITH env var, fork still did NOT inherit (env var has no effect on per-skill fork)"
else
    fail "indeterminate"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
# Clean the env var so subsequent scenarios are unaffected
send_keys "unset CLAUDE_CODE_FORK_SUBAGENT" Enter
sleep 0.5
scenario_end
