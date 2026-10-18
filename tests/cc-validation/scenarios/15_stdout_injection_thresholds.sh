#!/usr/bin/env bash
# Scenario 15 — characterize when SubagentStart plain stdout injects vs not.
# Earlier finding: single-token echo doesn't visibly inject; multi-line bash does.
# Where's the threshold?

# nexus-qsmc5: the old prompt wrote $marker WHOLE ("Is the literal text $marker
# there?"), the pane shows the prompt, and the grep below matched the pane, so
# every variant read "visible" off its own prompt (and the always-pass branches hid
# it). The prompt now names the marker in two pieces, the reply tokens are spelled
# in pieces and tagged per probe (the sandbox keeps every earlier probe's
# transcript), and the verdict reads the SUBAGENT's own transcript
# (cc_subagent_replies): visible only if its reply carries the whole marker.
probe_inject() {
    local label="$1" command="$2" marker="$3"
    local tail15="${marker##*-}"
    local head15="${marker%"$tail15"}"
    local tag="${label}S"
    cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "$command" }] }
    ]
  }
}
EOF
    CC_DEBUG_FILE="$TEST_HOME/cc-debug-${label}.log"; rm -f "$CC_DEBUG_FILE"
    claude_start
    claude_prompt "Use Task to dispatch the general-purpose agent. Description='$label'. Prompt: 'Do not use any tools. Examine your context, apart from this message. Is the text ${head15} immediately followed by ${tail15} (no space) anywhere in it? If yes, reply with the word FOUND immediately followed by -${tag} (no space), then a space, then that whole text. If no, reply with the word NONE immediately followed by -${tag} (no space).'"
    cc_wait_subagent_reply "FOUND-${tag}|NONE-${tag}" 180 || true
    local replies found_line
    replies="$(cc_subagent_replies || true)"
    found_line="$(grep -F "FOUND-${tag}" <<<"$replies" || true)"
    if [[ "$found_line" == *"$marker"* ]]; then
        echo "    [$label] $marker VISIBLE"
        pass "[$label] visible — plain stdout DID inject"
        result_visible=1
    elif grep -qF "NONE-${tag}" <<<"$replies"; then
        echo "    [$label] $marker NOT visible"
        pass "[$label] not visible — this style does not inject"
        result_visible=0
    else
        echo "    [$label] $marker INDETERMINATE"
        fail "[$label] indeterminate — the subagent answered neither FOUND-${tag} with the whole marker nor NONE-${tag}"
        result_visible=-1
    fi
    cc_report_context_lines
    cc_keep_evidence "$label"
    claude_exit
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

echo ""
echo "    ──────────── 15 verdict ────────────"
echo "    15a single token visible:    $visible_15a"
echo "    15b single line prose:       $visible_15b"
echo "    15c multi-line printf:       $visible_15c"
echo "    15d two echoes:              $visible_15d"
