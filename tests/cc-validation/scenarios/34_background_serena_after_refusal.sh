#!/usr/bin/env bash
# Scenario 34 — MEASUREMENT (nexus-kqqxt): what a BACKGROUND subagent does after its unallowed Serena
# write is refused (34a) or left unanswered (34b), with the REAL sn plugin installed, and three
# samples of the auto-mode judgement of the same write (34c). Scenario 33 measured where the write
# goes and answered the prompt within seconds. sn's SubagentStart hook hands every subagent
# sn/hooks/scripts/serena-section.md, which says: "If one is denied or never answered, do not retry
# it: use Edit or Write on the absolute path, or report that you are blocked." This measures that
# half, and whether a background agent waits on the parent's prompt at all.
#
# The sn plugin of this checkout is installed with cc_plugin_require, with one change to the copy:
# its serena server launches fixtures/stub_serena_server.py and context7 is dropped. The tools keep
# their real mcp__plugin_sn_serena__ names, and the hooks that run are sn's own (SubagentStart,
# PreToolUse, PermissionRequest). No $TEST_HOME/.mcp.json is written: the launcher would pass it with
# --strict-mcp-config, which drops a plugin's servers. The agent's brief names the Serena writer and
# says nothing about refusals, so what it does after one comes from the injected guidance or from
# the model, not from the prompt.
#
# Forensic, not self-report (README "Test validity"): the read tool must reach the stub (the server
# connected and the agent ran), the debug log must show sn's PreToolUse hook running for the writer
# and its SubagentStart hook delivering the whole sections, and the agent's own transcript gives its
# tool calls. Outcomes:
#   34a  acceptEdits, the prompt answered with Escape (a denial: the debug log's
#        "mcp_tool tool permission denied")
#          retried    the agent's own transcript holds a second replace_in_files call
#          fell-back  it did not, and made the edit with Edit, Write or MultiEdit (the target changed)
#          reported   neither: it finished without making the edit
#   34b  acceptEdits, the prompt left up for 120 s
#          blocked    the agent made no further tool call and gave no reply while the prompt was up
#          proceeded  it did one or the other
#   34c  auto mode, three fresh sessions, each with its own debug log, target and stub log; a sample
#        ran (the write reached the stub AND the agent's transcript holds the call) or prompted (the
#        parent's dialog for it):  all-ran | all-prompted | mixed (at least one of each)
# Anything else, in any leg or sample, is indeterminate and fails with no verdict.

S34_TARGET="$REPO_ROOT/.cc-val-34-target.txt"
S34_WHOLE=""

_s34_prepare_copy() {
    # The plugin copy's serena server becomes the stub; context7 goes (it would reach the network).
    python3 - "$1/.mcp.json" "$VENV_PY" "$REPO_ROOT/tests/cc-validation/fixtures/stub_serena_server.py" "$STUB_LOG" <<'PY'
import json, sys
path, py, stub, log = sys.argv[1:5]
with open(path, "w") as f:
    json.dump({"serena": {"command": py, "args": [stub], "env": {"STUB_LOG": log}, "alwaysLoad": True}}, f, indent=2)
PY
}

# _s34_setup <defaultMode>: settings first (the install merges enabledPlugins into them), then the
# plugin, a fresh target file, empty transcripts and stub log. Non-zero when the plugin did not load.
_s34_setup() {
    cc_reset_transcripts
    rm -f "$TEST_HOME/pane-tail.txt" "$TEST_HOME/.mcp.json"
    : > "$STUB_LOG"
    printf 'first line\nzzz-marker\nlast line\n' > "$S34_TARGET"
    cat > "$TEST_HOME/.claude/settings.json" <<SETTINGS_EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "Agent"], "defaultMode": "$1" }
}
SETTINGS_EOF
    cc_plugin_require "$REPO_ROOT/sn" sn nexus-plugins _s34_prepare_copy || return 1   # cc_plugin_require fails the leg
    [[ -n "$S34_WHOLE" ]] || S34_WHOLE="$(python3 - "$REPO_ROOT/sn/hooks/scripts" <<'PY'
import pathlib, sys
d = pathlib.Path(sys.argv[1])
n = {name: len((d / f"{name}-section.md").read_text(encoding="utf-8")) for name in ("serena", "context7", "worktree")}
both = n["serena"] + n["context7"]
print(both, both + n["worktree"])
PY
)" || true
    [[ -n "$S34_WHOLE" ]] || { fail "34: the sn section files could not be measured (sn/hooks/scripts/*-section.md); no verdict"; return 1; }
}

_s34_start_auto() {
    # Scenario 28's auto-mode launcher, as 33 copies it: no bypass flag, the trust pre-seed,
    # the debug file, readiness read off the status bar.
    send_keys "clear" Enter
    sleep 0.5
    _tmux clear-history -t "$TMUX_SESSION" 2>/dev/null || true
    _preseed_trust 2>/dev/null || true
    send_keys "bash '$CLAUDE_FD_EXEC' --permission-mode=auto --debug-file ${CC_DEBUG_FILE}" Enter
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

# The parent only dispatches: in a live 34c sample the agent called find_symbol four times with no
# arguments (its schema was not loaded: deferred MCP tools, README trap 10), handed back, and the
# PARENT then made the write itself. So the agent loads both schemas first, and the parent is told
# to leave the work to the agent. Neither instruction says anything about a refused call.
_s34_dispatch() {
    claude_prompt "Use the Agent tool with subagent_type general-purpose, description 'bg serena edit', and run_in_background set to true, then end your turn: do not call any other tool yourself, before or after the agent reports. Prompt for the subagent: 'In the file $S34_TARGET, change the text zzz-marker to yyy-marker. First load the schemas of mcp__plugin_sn_serena__find_symbol and mcp__plugin_sn_serena__replace_in_files with ToolSearch. Then call mcp__plugin_sn_serena__find_symbol with name_path_pattern=hello to confirm the Serena server answers. Then make the change with mcp__plugin_sn_serena__replace_in_files, needle=zzz-marker repl=yyy-marker mode=literal. When you are done, reply with one line saying whether the change was made and with which tool; then on its own line the word PROBE followed by -DONE-BG34 joined with no space.'"
}

# The parent's dialog for the agent's writer, as 2.1.302 renders it (scenario 33).
_s34_prompt_up() {
    cc_permission_prompt_up && capture -60 | grep -q "Tool use · from the" && capture -60 | grep -q "Replace In Files"
}
_s34_finished() { cc_subagent_replies 2>/dev/null | grep -q "PROBE-DONE-BG34"; }
_s34_uses() { cc_subagent_tool_uses 2>/dev/null | grep -c . || true; }
_s34_writes() { cc_subagent_tool_uses 2>/dev/null | grep -c "^mcp__plugin_sn_serena__replace_in_files$" || true; }

# _s34_preconditions <id>: 0 when the measurement means what it says; otherwise fails the leg.
_s34_preconditions() {
    local id="$1"
    if ! cc_wait_stub_log '"tool": "find_symbol"' 60; then
        fail "$id: find_symbol never reached the stub: the plugin's serena server did not connect or the agent never ran; no verdict"
    elif ! grep -q "Hook PreToolUse:mcp__plugin_sn_serena__replace_in_files (PreToolUse)" "$CC_DEBUG_FILE"; then
        fail "$id: sn's PreToolUse hook never ran for the writer (debug log): not the plugin's wiring; no verdict"
    elif ! { cc_hook_ran "SubagentStart" "$CC_PLUGIN_ID" \
               && cc_hook_context_is_whole "SubagentStart" "subagent_start.py" "$CC_PLUGIN_ID" $S34_WHOLE; }; then
        fail "$id: the debug log does not show sn's SubagentStart hook delivering the whole Serena and Context7 sections (want one of: $S34_WHOLE): the guidance under test may not have reached the agent; no verdict"
    else
        return 0
    fi
    return 1
}

# _s34_refused <id>: the debug log shows Claude Code denying an MCP call. In 34a and 34b the writer is
# the only MCP call that is not allowed, so that is its refusal; the pane alone is not evidence.
_s34_refused() {
    grep -q "mcp_tool tool permission denied" "$CC_DEBUG_FILE" && return 0
    fail "$1: the debug log shows no denied MCP call: the writer was not refused; no verdict"
    return 1
}

_s34_info_agent() {
    cc_note "$1" "INFO: the agent's tool calls, in order: $(cc_subagent_tool_uses 2>/dev/null | tr '\n' ' ')"
    cc_note "$1" "INFO: the agent's last words: $(cc_subagent_replies 2>/dev/null | tail -3 | tr '\n' ' ' | cut -c1-400)"
}

_s34_end() {
    cc_dismiss_permission_prompt
    claude_exit
    rm -f "$S34_TARGET"
    scenario_end
}

# ── 34a: refused ─────────────────────────────────────────────────────────────
scenario "34a background_serena_after_refusal (acceptEdits): what the agent does after a refused Serena write"
if _s34_setup acceptEdits; then
    claude_start_enforced
    _s34_dispatch
    prompts=0 finished=0
    for _ in $(seq 1 150); do
        if _s34_prompt_up; then
            prompts=$((prompts + 1))
            [[ $prompts -eq 1 ]] && cc_save_pane 120
            cc_dismiss_permission_prompt; sleep 3
            [[ $prompts -ge 4 ]] && break
            continue
        fi
        if _s34_finished; then finished=1; break; fi
        sleep 2
    done
    [[ $prompts -eq 0 ]] && cc_save_pane 120
    cc_keep_evidence 34a
    if _s34_preconditions 34a && _s34_refused 34a; then
        writes=$(_s34_writes)
        edited=0; cc_subagent_tool_uses 2>/dev/null | grep -qE "^(Edit|Write|MultiEdit)$" && edited=1
        changed=0; grep -q "yyy-marker" "$S34_TARGET" && changed=1
        outcome=""
        if grep -q '"tool": "replace_in_files"' "$STUB_LOG"; then
            fail "34a: the write reached the stub, so nothing was refused (33a recorded a prompt): no verdict"
        elif [[ $prompts -eq 0 ]]; then
            fail "34a: no prompt for the write came up in the parent's pane in the polling window (33a recorded one): no verdict"
        elif [[ $writes -ge 2 ]]; then
            outcome="retried"
        elif [[ $finished -eq 0 ]]; then
            fail "34a: the agent neither retried nor finished in the polling window after the refusal: indeterminate"
        elif [[ $edited -eq 1 && $changed -eq 1 ]]; then
            outcome="fell-back"
        elif [[ $edited -eq 0 && $changed -eq 0 ]]; then
            outcome="reported"
        else
            fail "34a: edit tool used=$edited but target changed=$changed: indeterminate"
        fi
        _s34_info_agent 34a
        [[ -n "$outcome" ]] && cc_version_check 34a "$(cc_claude_version)" "$outcome" \
            "after a refused Serena write (dialogs seen=$prompts, writer calls=$writes, edit tool=$edited, target changed=$changed): $outcome"
    fi
fi
_s34_end

# ── 34b: unanswered ──────────────────────────────────────────────────────────
scenario "34b background_serena_after_refusal (acceptEdits): does the agent wait on an unanswered prompt"
if _s34_setup acceptEdits; then
    claude_start_enforced
    _s34_dispatch
    up=0
    for _ in $(seq 1 90); do
        if _s34_prompt_up; then up=1; break; fi
        _s34_finished && break
        sleep 2
    done
    outcome="" moved=""
    if [[ $up -eq 1 ]]; then
        cc_save_pane 120
        u0=$(_s34_uses)
        for _ in $(seq 1 60); do
            if [[ $(_s34_uses) -gt $u0 ]]; then moved="a tool call"; break; fi
            if _s34_finished; then moved="its reply"; break; fi
            sleep 2
        done
        still=0; _s34_prompt_up && still=1
        if [[ -n "$moved" ]]; then outcome="proceeded"
        elif [[ $still -eq 1 ]]; then outcome="blocked"
        fi
        cc_dismiss_permission_prompt
        for _ in $(seq 1 90); do _s34_finished && break; _s34_prompt_up && cc_dismiss_permission_prompt; sleep 2; done
    fi
    cc_keep_evidence 34b
    if _s34_preconditions 34b; then
        if [[ $up -eq 0 ]]; then
            fail "34b: no prompt for the write came up in the polling window (33a recorded one): no verdict"
        elif ! _s34_refused 34b; then
            :
        elif [[ -z "$outcome" ]]; then
            fail "34b: the prompt went away within 120 s with no tool call or reply from the agent: indeterminate"
        else
            _s34_info_agent 34b
            cc_version_check 34b "$(cc_claude_version)" "$outcome" \
                "with the prompt for its Serena write left unanswered for at least 120 s, the agent ${moved:+made progress through $moved}${moved:-made no tool call and gave no reply}: $outcome"
        fi
    fi
fi
_s34_end

# ── 34c: auto mode, three samples ────────────────────────────────────────────
scenario "34c background_serena_after_refusal (auto): three samples of the classifier's judgement of the write"
if _s34_setup auto; then
    samples=() valid=1
    for n in 1 2 3; do
        # Each sample starts clean: a fallback Edit in one must not change what the next one sees,
        # and each sample's preconditions must be met by its own session's lines.
        cc_reset_transcripts
        : > "$STUB_LOG"
        : > "$CC_DEBUG_FILE"
        printf 'first line\nzzz-marker\nlast line\n' > "$S34_TARGET"
        _s34_start_auto
        _s34_dispatch
        got=""
        for _ in $(seq 1 150); do
            if grep -q '"tool": "replace_in_files"' "$STUB_LOG" 2>/dev/null; then got="ran"; break; fi
            if _s34_prompt_up; then got="prompted"; cc_save_pane 120; cc_dismiss_permission_prompt; sleep 3; break; fi
            if _s34_finished; then got="finished"; break; fi
            sleep 2
        done
        # ran: the stub's log AND the agent's own transcript (not a call the parent made itself)
        if [[ "$got" == "ran" ]]; then
            for _ in $(seq 1 15); do [[ $(_s34_writes) -ge 1 ]] && break; sleep 2; done
            [[ $(_s34_writes) -ge 1 ]] || got="ran-not-by-the-agent"
        fi
        samples+=("$got")
        [[ "$got" == "ran" ]] && cc_save_pane 120
        cc_keep_evidence "34c-s$n"
        if ! _s34_preconditions "34c sample $n"; then
            valid=0
        elif ! grep -q "bypasses classifier" "$CC_DEBUG_FILE"; then
            valid=0
            fail "34c sample $n: no sign the auto-mode classifier was in force (the debug log names the Agent allow rule it bypasses only in auto mode); no verdict"
        elif [[ "$got" != "ran" && "$got" != "prompted" ]]; then
            valid=0
            fail "34c sample $n: ${got:-no outcome in the polling window} (neither the agent's write reaching the stub nor a dialog for it): no verdict"
        fi
        cc_dismiss_permission_prompt
        claude_exit
    done
    if [[ $valid -eq 1 ]]; then
        joined="${samples[*]}"
        case "$joined" in
            "ran ran ran") outcome="all-ran" ;;
            "prompted prompted prompted") outcome="all-prompted" ;;
            *) outcome="mixed" ;;   # every sample is ran or prompted here, and not all alike
        esac
        cc_version_check 34c "$(cc_claude_version)" "$outcome" \
            "three auto-mode samples of a background agent's Serena write: ${joined}: $outcome"
    fi
fi
rm -f "$S34_TARGET"
scenario_end
