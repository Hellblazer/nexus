#!/usr/bin/env bash
# Scenario 05 — suffix wildcard mcp__server__* should grant permission interactively.
#
# nexus-qsmc5: this scenario pins defaultMode "default" and used to launch through
# claude_start, which passes --dangerously-skip-permissions, so "the tool ran"
# proved nothing about the rule. It now launches with claude_start_enforced (no
# bypass flag) and runs two legs in the same mode:
#   control  allow = ["mcp__other__*"]  (matches nothing): the call must stop at
#            a permission prompt and the tool must NOT run. That is the proof
#            permissions are enforced in these sessions.
#   allowed  allow = ["mcp__stub__*"]: the tool must run with no prompt pending.
# cc_allow_rule_verdict (runner.sh) fails the scenario unless both hold.

scenario "05 perms_wildcard_suffix: mcp__stub__* should auto-allow (permissions enforced, control prompts)"

cat > "$TEST_HOME/.mcp.json" <<EOT
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOT
send_keys "cd $TEST_HOME" Enter
sleep 0.3

# _s05_leg <allow-rule> <payload> <expect-prompt 0|1>: one enforced session.
# Sets LEG_PROMPT (a permission prompt was pending when the call settled) and
# LEG_RAN (the stub logged the call).
_s05_leg() {
    local rule="$1" payload="$2" expect_prompt="$3"
    cat > "$TEST_HOME/.claude/settings.json" <<EOT
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["$rule"], "defaultMode": "default" }
}
EOT
    : > "$STUB_LOG"
    claude_start_enforced
    # Deferred-tool warmup (see README "Deferred MCP tools"): force schema load before
    # the measured call so it doesn't race discovery (the scenario-16 root cause).
    claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
    claude_wait 30
    claude_prompt "Call mcp__stub__record with payload='${payload}'. Reply DONE when complete."
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

_s05_leg "mcp__stub__*" "suffix-wildcard-test" 0
ALLOWED_PROMPT=$LEG_PROMPT; ALLOWED_RAN=$LEG_RAN
_s05_leg "mcp__other__*" "suffix-control-test" 1
cc_allow_rule_verdict "05" "mcp__stub__*" "$LEG_PROMPT" "$LEG_RAN" "$ALLOWED_RAN" "$ALLOWED_PROMPT"

send_keys "cd $REPO_ROOT" Enter
sleep 0.3
scenario_end
