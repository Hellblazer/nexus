#!/usr/bin/env bash
# Scenario 04 — SessionStart plain stdout should inject (asymmetry vs SubagentStart).

scenario "04 session_plain_stdout: SessionStart plain echo should inject"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": [], "defaultMode": "acceptEdits" },
  "hooks": {
    "SessionStart": [
      { "matcher": "startup",
        "hooks": [{ "type": "command",
                    "command": "echo 'SESS-PLAIN-MARKER-INTERACTIVE-Q9X'" }] }
    ]
  }
}
EOF

claude_start
claude_prompt "Examine your context. Quote any line beginning with SESS-PLAIN-MARKER- exactly. If none, reply NO-MARKER."
claude_wait 60

if capture -300 | grep -qE "SESS-PLAIN-MARKER-INTERACTIVE-Q9X"; then
    pass "SessionStart plain stdout DID inject (asymmetry confirmed)"
else
    fail "SessionStart plain stdout did NOT inject (asymmetry hypothesis wrong)"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
scenario_end
