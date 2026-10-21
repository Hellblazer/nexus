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

CC_DEBUG_FILE="$TEST_HOME/cc-debug-04.log"; rm -f "$CC_DEBUG_FILE"
claude_start
# nexus-qsmc5: the reply tokens are spelled in pieces and the marker's code is
# not in the prompt; the verdict reads the main session's own transcript
# (cc_session_replies), not the pane, which also prints the hook's output.
claude_prompt "Examine your context, apart from this message. Is the text SESS-PLAIN-MARKER-INTERACTIVE- immediately followed by a three-character code (no space) anywhere in it? If yes, reply with the word FOUND immediately followed by -04S (no space), then a space, then that whole text including its code. If no, reply with the word NONE immediately followed by -04S (no space)."
cc_wait_session_reply "FOUND-04S|NONE-04S" 120 || true
REPLIES04="$(cc_session_replies || true)"

if grep -qE "FOUND-04S.*SESS-PLAIN-MARKER-INTERACTIVE-Q9X" <<<"$REPLIES04"; then
    pass "SessionStart plain stdout DID inject (asymmetry confirmed)"
else
    fail "SessionStart plain stdout did NOT inject (asymmetry hypothesis wrong)"
    echo "$REPLIES04" | tail -5 | sed 's/^/    | /'
fi

cc_report_context_lines
cc_keep_evidence "04"
claude_exit
scenario_end
