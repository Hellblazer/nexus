#!/usr/bin/env bash
# Scenario 15 — characterize when SubagentStart plain stdout injects vs not.
# Earlier finding: single-token echo doesn't visibly inject; multi-line bash does.
# Where's the threshold?

# nexus-qsmc5: the old prompt wrote $marker WHOLE ("Is the literal text $marker
# there?"), the pane shows the prompt, and the grep below matched the pane, so
# every variant read "visible" off its own prompt (and the always-pass branches hid
# it). The reply tokens are spelled in pieces and tagged per probe (the sandbox
# keeps every earlier probe's transcript), and the verdict reads the SUBAGENT's own
# transcript (cc_subagent_replies).
#
# nexus-qsmc5 review: the marker's last three characters (its code) are NOT in the
# prompt, as in 01-04. The prompt names the head and asks for a three-character code
# after it; the reading compares the code the subagent reports with the known marker,
# so a subagent that was handed head and tail could not pass without an injection. The
# reading (cc_injection_reading, runner.sh) also needs the debug log to agree: VISIBLE
# needs the hook finished and its additionalContext accepted; NONE needs the hook
# finished and NO additionalContext accepted; a disagreement FAILS.
# _s15_session <label> <command> <marker> <matcher>: one session with a SubagentStart command
# hook registered under <matcher> (empty: every agent), a subagent asked for the marker's code,
# and the reading of the answer against the debug log in S15_READING.
_s15_session() {
    local label="$1" command="$2" marker="$3" matcher="${4:-}"
    local head15="${marker%???}"
    local tag="${label}S"
    cc_reset_transcripts
    cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "$matcher",
        "hooks": [{ "type": "command", "command": "$command" }] }
    ]
  }
}
EOF
    CC_DEBUG_FILE="$TEST_HOME/cc-debug-${label}.log"; rm -f "$CC_DEBUG_FILE"
    claude_start
    claude_prompt "Use Task to dispatch the general-purpose agent. Description='$label'. Prompt: 'Do not use any tools. Examine your context, apart from this message. Is the text ${head15} immediately followed by a three-character code (no space) anywhere in it? If yes, reply with the word FOUND immediately followed by -${tag} (no space), then a space, then that whole text including its code. If no, reply with the word NONE immediately followed by -${tag} (no space).'"
    cc_wait_subagent_reply "FOUND-${tag}|NONE-${tag}" 180 || true
    local replies
    replies="$(cc_subagent_replies || true)"
    S15_READING="$(cc_injection_reading SubagentStart "$marker" "$tag" "$replies")"
    cc_report_context_lines
    cc_keep_evidence "$label"
    claude_exit
}

probe_inject() {
    local label="$1" command="$2" marker="$3"
    _s15_session "$label" "$command" "$marker" ""
    local reading="$S15_READING"
    case "$reading" in
        VISIBLE)
            echo "    [$label] $marker VISIBLE"
            pass "[$label] visible: plain stdout DID inject (the subagent reported the whole marker; the debug log shows the hook finished and its context accepted)"
            result_visible=1 ;;
        NOT-VISIBLE)
            echo "    [$label] $marker NOT visible"
            pass "[$label] not visible: this style does not inject (the subagent answered NONE; the debug log shows the hook finished and no context accepted)"
            result_visible=0 ;;
        MISMATCH*)
            echo "    [$label] $marker MISMATCH"
            fail "[$label] ${reading#MISMATCH }"
            result_visible=-1 ;;
        *)
            echo "    [$label] $marker INDETERMINATE"
            fail "[$label] ${reading#INDETERMINATE }"
            result_visible=-1 ;;
    esac
}

# Negative control (nexus-qsmc5 review): an injection that delivers nothing because the hook
# NEVER RAN must not read as "plain stdout does not inject". The hook below is registered under
# a matcher no agent has, so the subagent still answers NONE; the reading must refuse that NONE
# for want of the hook's finished line. The control passes only on that refusal.
control_dead_hook() {
    local label="15e" marker="STDOUT-15E-MARKER-K4M"
    _s15_session "$label" "echo '$marker'" "$marker" "zzz-no-such-agent"
    case "$S15_READING" in
        MISMATCH*"no finished-with-status-0 line"*)
            echo "    [$label] $marker DEAD-HOOK-REFUSED"
            pass "[$label] negative control: the subagent answered NONE but the hook never ran, and the reading refused it (a NONE alone is what a dead hook looks like)" ;;
        NOT-VISIBLE)
            echo "    [$label] $marker NONE-ACCEPTED"
            fail "[$label] negative control failed: a NONE from a hook that never ran was accepted as 'plain stdout does not inject'" ;;
        *)
            echo "    [$label] $marker ${S15_READING%% *}"
            fail "[$label] negative control: expected the reading to refuse a NONE with no finished hook, got: ${S15_READING}" ;;
    esac
}

# ── 15a: single-line echo, single token (matches scenario 01 — control)
scenario "15a single_token: echo 'TOKEN' (single line, single token)"
probe_inject "15a" "echo 'STDOUT-15A-MARKER-Q9X'" "STDOUT-15A-MARKER-Q9X"
visible_15a=$result_visible
scenario_end

# ── 15b: single-line echo with structured prose around the marker
scenario "15b single_line_prose: echo with full-sentence prose"
probe_inject "15b" "echo 'A test marker has been placed in your context: STDOUT-15B-MARKER-T7K'" "STDOUT-15B-MARKER-T7K"
visible_15b=$result_visible
scenario_end

# ── 15c: multi-line via shell printf (true newlines)
scenario "15c multiline_printf: printf with \\n newlines"
probe_inject "15c" "printf 'Line one of context.\nLine two with marker STDOUT-15C-MARKER-W3J.\nLine three.\n'" "STDOUT-15C-MARKER-W3J"
visible_15c=$result_visible
scenario_end

# ── 15d: multi-line via two echoes
scenario "15d two_echoes: two echo statements joined with ;"
probe_inject "15d" "echo 'header'; echo 'STDOUT-15D-MARKER-R2P'" "STDOUT-15D-MARKER-R2P"
visible_15d=$result_visible
scenario_end

# ── 15e: negative control, a hook that never runs
scenario "15e dead_hook_control: a NONE from a hook that never ran must be refused"
control_dead_hook
scenario_end

echo ""
echo "    ──────────── 15 verdict ────────────"
echo "    15a single token visible:    $visible_15a"
echo "    15b single line prose:       $visible_15b"
echo "    15c multi-line printf:       $visible_15c"
echo "    15d two echoes:              $visible_15d"
