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
#
# nexus-qsmc5 (measured live, chas container, Claude Code 2.1.302, 2026-10-18): that
# 2026-05-31 "matches" reading was made under --dangerously-skip-permissions, where
# every call runs, so it was no reading at all. Without the flag the infix wildcard
# does NOT match: the call stops at a permission prompt exactly as the control does.
# This scenario reports whichever is true (`report` mode of cc_allow_rule_verdict).
#
# nexus-qsmc5: the scenario pins defaultMode "default" and used to launch through
# claude_start, which passes --dangerously-skip-permissions, so "the tool ran"
# held whether or not the wildcard matched. It now launches with
# claude_start_enforced (no bypass flag) and runs a control leg in the same mode
# whose rule has the same infix shape but does not match this server
# (mcp__zzz_*__*): that call must stop at a permission prompt without running the
# tool, which is the proof permissions are enforced. cc_allow_rule_verdict
# (runner.sh) fails the scenario unless the control prompts AND the real rule runs
# the tool with no prompt.

scenario "06 perms_wildcard_infix: does mcp__plugin_*__* match across the __ boundary? (permissions enforced, control prompts)"

cat > "$TEST_HOME/.mcp.json" <<EOT
{ "mcpServers": {
    "plugin_xxx_yyy": { "type": "stdio", "command": "python3",
        "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
        "env": { "STUB_LOG": "$STUB_LOG", "STUB_NAME": "plugin_xxx_yyy" } }
} }
EOT

# _s06_leg <allow-rule> <payload> <expect-prompt 0|1>: one enforced session.
# Sets LEG_PROMPT (a permission prompt was pending when the call settled) and
# LEG_RAN (the stub logged the call).
_s06_leg() {
    local rule="$1" payload="$2" expect_prompt="$3"
    cat > "$TEST_HOME/.claude/settings.json" <<EOT
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["$rule"], "defaultMode": "default" }
}
EOT
    : > "$STUB_LOG"
    claude_start_enforced
    # Deferred-tool warmup (see README "Deferred MCP tools"): load the schema before
    # the measured call so it does not race discovery.
    claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
    claude_wait 30
    claude_prompt "Call the mcp__plugin_xxx_yyy__record tool with payload='${payload}'. Reply DONE when complete, or REJECTED if the call is denied."
    if [[ "$expect_prompt" == "1" ]]; then
        cc_wait_permission_prompt 90 || true
    else
        claude_wait 90
    fi
    LEG_PROMPT=0; LEG_RAN=0
    cc_permission_prompt_up && LEG_PROMPT=1
    [[ -s "$STUB_LOG" ]] && grep -q "$payload" "$STUB_LOG" && LEG_RAN=1
    if [[ $LEG_PROMPT -eq 0 && $LEG_RAN -eq 0 ]]; then
        capture -30 | sed 's/^/    | /'
    fi
    cc_dismiss_permission_prompt
    claude_exit
}

send_keys "cd $TEST_HOME" Enter; sleep 0.3
_s06_leg "mcp__plugin_*__*" "infix-test" 0
ALLOWED_PROMPT=$LEG_PROMPT; ALLOWED_RAN=$LEG_RAN
_s06_leg "mcp__zzz_*__*" "infix-control-test" 1
cc_allow_rule_verdict "06" "mcp__plugin_*__*" "$LEG_PROMPT" "$LEG_RAN" "$ALLOWED_RAN" "$ALLOWED_PROMPT" report

send_keys "cd $REPO_ROOT" Enter; sleep 0.3
rm -f "$TEST_HOME/.mcp.json"
scenario_end
