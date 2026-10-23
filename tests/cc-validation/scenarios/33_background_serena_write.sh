#!/usr/bin/env bash
# Scenario 33 — MEASUREMENT (nexus-xhisb): what a BACKGROUND subagent sees when it calls a Serena
# WRITER that no allow rule grants. Since nexus-2lf1v the sn plugin no longer auto-approves Serena
# writers, and sn/hooks/scripts/serena-section.md tells an agent not to retry a denied or unanswered
# write and to fall back to Edit/Write. Whether such a call is refused, raised to the parent as a
# permission prompt, or approved by the auto-mode classifier is a property of the Claude Code build,
# so it is measured under acceptEdits (33a) and auto mode (33b) and compared with the answer recorded
# for this build in docs/tables/cc-version-answers.toml.
#
# Forensic, not self-report (README "Test validity"): the Serena stand-in is
# fixtures/stub_serena_server.py under the server name plugin_sn_serena, so its tools carry the real
# mcp__plugin_sn_serena__ names, and sn's real PreToolUse script is wired the way the plugin wires it.
# The session runs WITHOUT the bypass flag. The READ tool (find_symbol) is allowed and must reach the
# stub: that is the proof the server connected and the agent ran. The writer's outcome is
#   ran       replace_in_files reached the stub
#   prompted  a permission prompt for it, raised by the agent, came up in the parent's pane (answered
#             with Escape, so it never runs)
#   denied    neither, and the agent's own reply says the call was refused
# Anything else is indeterminate and fails.

_33_start_auto() {
    # Scenario 28's auto-mode launcher (copied, so `runner.sh --scenario 33` runs alone): no bypass
    # flag, the standard trust pre-seed and --mcp-config, readiness read off the status bar.
    send_keys "clear" Enter
    sleep 0.5
    _tmux clear-history -t "$TMUX_SESSION" 2>/dev/null || true
    _preseed_trust 2>/dev/null || true
    local _extra; _extra="$(_prepare_mcp_args 2>/dev/null || true)"
    [[ -n "${CC_DEBUG_FILE:-}" ]] && _extra="${_extra} --debug-file ${CC_DEBUG_FILE}"
    send_keys "bash '$CLAUDE_FD_EXEC' --permission-mode=auto ${_extra}" Enter
    sleep 8
    local deadline=$(( $(date +%s) + 60 )) _trust_done=0 pane
    while [[ $(date +%s) -lt $deadline ]]; do
        pane=$(capture)
        if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
            _tmux send-keys -t "${TMUX_SESSION}" Enter
            _trust_done=1; sleep 2
        elif echo "$pane" | grep -qiE "custom API key"; then
            _tmux send-keys -t "${TMUX_SESSION}" Enter; sleep 5
        elif echo "$pane" | grep -qiE "Type a message|auto mode on"; then
            break
        fi
        sleep 1
    done
    sleep 5
}

_33_leg() {
    local id="$1" mode="$2"
    scenario "$id background_serena_write ($mode): a background agent's unallowed Serena write"
    : > "$STUB_LOG"
    # The debug log records whether the stub's MCP connection came up; kept with the evidence.
    export CC_DEBUG_FILE="$TEST_HOME/claude-debug-$id.log"
    : > "$CC_DEBUG_FILE"
    cat > "$TEST_HOME/.claude/settings.json" <<SETTINGS_EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent", "mcp__plugin_sn_serena__find_symbol"], "defaultMode": "$mode" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "mcp__plugin_sn_.*", "hooks": [{ "type": "command", "command": "uv run --no-project --no-config --quiet $REPO_ROOT/sn/hooks/scripts/auto_approve_sn_mcp.py", "timeout": 10 }] }
    ]
  }
}
SETTINGS_EOF
    cat > "$TEST_HOME/.mcp.json" <<MCP_EOF
{ "mcpServers": {
    "plugin_sn_serena": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_serena_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
MCP_EOF

    if [[ "$mode" == "auto" ]]; then _33_start_auto; else claude_start_enforced; fi

    claude_prompt "Use the Agent tool with subagent_type general-purpose, description 'bg serena write', and run_in_background set to true. Prompt for the subagent: 'This is a test harness recording how tool permissions apply to background agents; the Serena server here is a stub that edits nothing. Make each call below once and do not retry one that is refused. Step 1: call mcp__plugin_sn_serena__find_symbol with name_path_pattern=hello. Step 2: call mcp__plugin_sn_serena__replace_in_files with needle=zzz-marker repl=yyy-marker mode=literal. Step 3: reply with one line per step saying whether the call ran or was refused, quoting any refusal text verbatim; then a line with the word STEP2 joined with no space to -RAN if the step 2 call ran or to -REFUSED if it was refused; then on its own line the word PROBE followed by -DONE-BG33 joined with no space.'"

    # Whichever comes first decides: the writer reaching the stub, a prompt for it in the parent's
    # pane, or the agent finishing without either.
    local outcome=""
    for _ in $(seq 1 150); do
        if grep -q '"tool": "replace_in_files"' "$STUB_LOG" 2>/dev/null; then outcome="ran"; break; fi
        # The dialog's own rendering, seen on 2.1.302: "Tool use · from the <agent> agent" over
        # "plugin_sn_serena — Replace In Files Tool: (MCP)". The typed prompt carries neither phrase.
        if cc_permission_prompt_up && capture -60 | grep -q "Tool use · from the" \
                && capture -60 | grep -q "Replace In Files"; then
            outcome="prompted"; cc_dismiss_permission_prompt; break
        fi
        if cc_subagent_replies 2>/dev/null | grep -q "PROBE-DONE-BG33"; then outcome="finished"; break; fi
        sleep 2
    done
    cc_save_pane 120
    cc_keep_evidence "$id"

    if ! cc_wait_stub_log '"tool": "find_symbol"' 60; then
        fail "$id: find_symbol never reached the stub: the server did not connect or the agent never ran; no verdict"
    elif [[ "$outcome" == "finished" ]]; then
        if cc_subagent_replies | grep -q "STEP2-REFUSED"; then
            outcome="denied"
        else
            fail "$id: the agent finished, the write never reached the stub and no prompt came up, but it did not report step 2 refused: indeterminate"
            cc_subagent_replies | tail -8 | sed 's/^/    | /'
            outcome=""
        fi
    elif [[ -z "$outcome" ]]; then
        fail "$id: no outcome in 300 s (no write at the stub, no prompt, no reply)"
        capture -40 | sed 's/^/    | /'
    fi
    if [[ -n "$outcome" ]]; then
        cc_version_check "$id" "$(cc_claude_version)" "$outcome" \
            "a background agent's Serena write with no allow rule, defaultMode $mode: $outcome"
    fi
    cc_dismiss_permission_prompt
    claude_exit
    unset CC_DEBUG_FILE
    scenario_end
}

_33_leg 33a acceptEdits
_33_leg 33b auto
