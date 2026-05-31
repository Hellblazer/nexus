#!/usr/bin/env bash
# Scenario 06 — does the infix wildcard mcp__plugin_*__* match a tool whose
# name has a literal '__' where the '*' sits (mcp__plugin_xxx_yyy__record)?
#
# VALIDITY NOTE (reworked 2026-05-31): the prior version declared the server in
# settings.json.mcpServers and "passed" when the tool was REJECTED — but the
# tool was rejected because the server never connected, not because the
# wildcard failed to match. That was a vacuous pass (it would pass with the MCP
# stack completely broken). This version declares the server in .mcp.json so
# the runner's --mcp-config wrapper connects it reliably; the tool is therefore
# genuinely AVAILABLE, and whether it runs reflects ONLY the permission match.
# Empirically (interactive CC, 2026-05-31) the infix wildcard DOES match, which
# contradicts the older -p finding. We assert the real behavior.

scenario "06 perms_wildcard_infix: mcp__plugin_*__* matches across the __ boundary"

cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "plugin_xxx_yyy": { "type": "stdio", "command": "python3",
        "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
        "env": { "STUB_LOG": "$STUB_LOG", "STUB_NAME": "plugin_xxx_yyy" } }
} }
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__plugin_*__*"], "defaultMode": "default" }
}
EOF

: > "$STUB_LOG"
send_keys "cd $TEST_HOME" Enter; sleep 0.3
claude_start
claude_prompt "Call the mcp__plugin_xxx_yyy__record tool with payload='infix-test'. Reply DONE when complete, or REJECTED if the call is denied."
claude_wait 90

tool_ran=0
[[ -s "$STUB_LOG" ]] && grep -q "infix-test" "$STUB_LOG" && tool_ran=1

if [[ $tool_ran -eq 1 ]]; then
    pass "infix wildcard mcp__plugin_*__* MATCHED across __ — tool ran (real interactive CC behavior)"
else
    # With the server connected via --mcp-config, a non-run means the permission
    # genuinely did not match. That is the only other valid outcome; flag it so
    # a real behavior change is visible rather than silently green.
    fail "infix wildcard did NOT grant — tool did not run though server is connected"
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter; sleep 0.3
rm -f "$TEST_HOME/.mcp.json"
scenario_end
