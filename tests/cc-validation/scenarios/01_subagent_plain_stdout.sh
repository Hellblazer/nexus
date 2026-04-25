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

claude_start
claude_prompt "Use the Task tool to dispatch the general-purpose agent. Description='subagent plain stdout probe'. Prompt for the subagent: 'Examine your context window. Quote any line beginning with SUB-PLAIN-MARKER- exactly. If none, reply NO-MARKER.'"
claude_wait 90

# Capture more pane history than default (-150 lines may miss the marker emitted earlier)
if capture -300 | grep -qE "SUB-PLAIN-MARKER-K7Q3" && ! capture -300 | grep -qE "NO-MARKER"; then
    pass "plain stdout DID inject in interactive (contradicts -p finding — bug-fix unnecessary)"
elif capture -300 | grep -qE "NO-MARKER"; then
    pass "plain stdout did NOT inject in interactive (confirms -p finding — existing subagent-start.sh is silently broken)"
else
    fail "indeterminate — neither marker nor NO-MARKER seen"
    capture -50 | sed 's/^/    | /'
fi

claude_exit
scenario_end
