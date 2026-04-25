#!/usr/bin/env bash
# Scenario 06 — infix wildcard mcp__plugin_*__* should NOT match (across __ boundary).

scenario "06 perms_wildcard_infix: mcp__plugin_*__* should FAIL to match"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__plugin_*__*"], "defaultMode": "default" },
  "mcpServers": {
    "plugin_xxx_yyy": { "type": "stdio", "command": "python3",
                        "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
                        "env": { "STUB_LOG": "$STUB_LOG", "STUB_NAME": "plugin_xxx_yyy" } }
  }
}
EOF

claude_start
claude_prompt "Call mcp__plugin_xxx_yyy__record with payload='infix-test'. Reply DONE when complete or REJECTED if you cannot."
claude_wait 90

if [[ -s "$STUB_LOG" ]] && grep -q "infix-test" "$STUB_LOG"; then
    fail "infix wildcard MATCHED (contradicts -p finding) — tool ran"
else
    pass "infix wildcard did NOT match — tool was rejected (confirms -p finding)"
fi

claude_exit
scenario_end
