#!/usr/bin/env bash
# Scenario 06 — does the infix wildcard mcp__plugin_*__* match a tool whose
# name has a literal '__' where the '*' sits (mcp__plugin_xxx_yyy__record)?
#
# THIS IS AN OBSERVATION, NOT A CONTRACT. The answer has changed between Claude Code
# versions (and between the bypass-flag and enforced launches of this very scenario), so
# the scenario reports what the build under test did and compares it with the answer
# recorded for the same build in docs/tables/cc-version-answers.toml: the same answer
# passes, a different one FAILS "answer changed since <version>", a version with no row
# prints INFO "record this version's answer". Nothing may be deleted or relied on from
# either answer; re-measure on every Claude Code pin bump.
#
# VALIDITY NOTE (reworked 2026-05-31): the prior version declared the server in
# settings.json.mcpServers and "passed" when the tool was REJECTED because the server never
# connected, which was a vacuous pass. This version declares the server in .mcp.json so the
# runner's --mcp-config wrapper connects it; the tool is therefore genuinely AVAILABLE, and
# whether it runs reflects ONLY the permission match. The 2026-05-31 reading ("the wildcard
# matches") was made under --dangerously-skip-permissions, where every call runs, and was no
# reading at all.
#
# nexus-qsmc5: the scenario pins defaultMode "default" and launches with
# claude_start_enforced (no bypass flag), and runs a control leg in the same mode whose rule
# has the same infix shape but does not match this server (mcp__zzz_*__*): that call must stop
# at a permission prompt without running the tool, which is the proof permissions are
# enforced. cc_allow_rule_verdict (runner.sh) fails the scenario unless the control prompts,
# then reports the real rule's observation (verdict lines say which build) and keeps the
# evidence of each leg (cc_keep_evidence: stub log, pane tail, transcripts, settings).

scenario "06 perms_wildcard_infix: does mcp__plugin_*__* match across the __ boundary? (permissions enforced, control prompts)"

cat > "$TEST_HOME/.mcp.json" <<EOT
{ "mcpServers": {
    "plugin_xxx_yyy": { "type": "stdio", "command": "python3",
        "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
        "env": { "STUB_LOG": "$STUB_LOG", "STUB_NAME": "plugin_xxx_yyy" } }
} }
EOT

# _s06_leg <allow-rule> <payload> <expect-prompt 0|1> <evidence-label>: one enforced session.
# Sets LEG_PROMPT (a permission prompt was pending when the call settled) and
# LEG_RAN (the stub logged the call).
_s06_leg() {
    local rule="$1" payload="$2" expect_prompt="$3" label="$4"
    cat > "$TEST_HOME/.claude/settings.json" <<EOT
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["$rule"], "defaultMode": "default" }
}
EOT
    : > "$STUB_LOG"
    cc_reset_transcripts
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
    cc_save_pane 60
    cc_keep_evidence "$label"
    cc_dismiss_permission_prompt
    claude_exit
}

send_keys "cd $TEST_HOME" Enter; sleep 0.3
_s06_leg "mcp__plugin_*__*" "infix-test" 0 "06-allowed"
ALLOWED_PROMPT=$LEG_PROMPT; ALLOWED_RAN=$LEG_RAN
_s06_leg "mcp__zzz_*__*" "infix-control-test" 1 "06-control"
cc_allow_rule_verdict "06" "mcp__plugin_*__*" "$LEG_PROMPT" "$LEG_RAN" "$ALLOWED_RAN" "$ALLOWED_PROMPT" report

send_keys "cd $REPO_ROOT" Enter; sleep 0.3
rm -f "$TEST_HOME/.mcp.json"
scenario_end
