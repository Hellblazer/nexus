#!/usr/bin/env bash
# Scenario 01 — does SubagentStart plain-stdout inject context in interactive mode?
# CRITICAL: this is the test that decides whether the existing nexus subagent-start.sh
# is silently broken or works in real interactive use (vs -p mode which we know does NOT inject).

scenario "01 subagent_plain_stdout: marker should NOT appear if -p finding holds"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "echo 'SUB-PLAIN-MARKER-K7Q3'" }] }
    ]
  }
}
EOF

CC_DEBUG_FILE="$TEST_HOME/cc-debug-01.log"; rm -f "$CC_DEBUG_FILE"
claude_start
# nexus-qsmc5: the old probe put "NO-MARKER" whole in the prompt and matched the
# pane, so the pane's own copy of the prompt satisfied the "did NOT inject" branch
# every time (and the "DID inject" branch required NO-MARKER to be absent, so it
# could never be taken). The reply tokens are now spelled in pieces, the marker's
# code is not in the prompt at all, and the verdict reads the SUBAGENT's own
# transcript (cc_subagent_replies): the pane also prints hook output and the main
# agent's paraphrase, neither of which is the subagent's context.
claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='subagent plain stdout probe'. Prompt for the subagent: 'Do not use any tools. Examine your context, apart from this message. Is the text SUB-PLAIN-MARKER- immediately followed by a four-character code (no space) anywhere in it? If yes, reply with the word FOUND immediately followed by -01S (no space), then a space, then that whole text including its code. If no, reply with the word NONE immediately followed by -01S (no space).'"
cc_wait_subagent_reply "FOUND-01S|NONE-01S" 180 || true
REPLIES01="$(cc_subagent_replies || true)"

if grep -qE "FOUND-01S.*SUB-PLAIN-MARKER-K7Q3" <<<"$REPLIES01"; then
    pass "plain stdout DID inject in interactive (contradicts -p finding — bug-fix unnecessary)"
elif grep -qE "NONE-01S" <<<"$REPLIES01"; then
    pass "plain stdout did NOT inject in interactive (confirms -p finding — existing subagent-start.sh is silently broken)"
else
    fail "indeterminate — the subagent answered neither FOUND-01S with the marker line nor NONE-01S"
    echo "$REPLIES01" | tail -5 | sed 's/^/    | /'
fi

cc_report_context_lines
cc_keep_evidence "01"
claude_exit
scenario_end
