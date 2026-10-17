#!/usr/bin/env bash
# Scenario 13 — disambiguate SubagentStart plain-stdout injection.
# Prior tests confounded by SessionStart-injected content & skill/agent content.
#
# This test runs THREE variations with distinct unique markers (impossible to find
# elsewhere). Each variation shows whether plain stdout from a SubagentStart hook
# in that configuration actually reaches the subagent's context.
#
#   13a: SubagentStart hook in settings.json — plain echo, multi-line bash script (mimics nexus)
#   13b: SubagentStart hook in a fake plugin — plain echo, multi-line bash script
#   13c: SubagentStart hook with proper additionalContext JSON (positive control)

# ── 13a: project settings.json with multi-line bash script
scenario "13a project_hook_multiline: SubagentStart in settings.json with bash multi-line script"

cat > "$TEST_HOME/.claude/multiline_hook_a.sh" <<'BASH_EOF'
#!/usr/bin/env bash
echo "## Test Section A"
echo ""
echo "T1 scratch — UNIQUE-PROJECT-A-MARKER-KQX73"
echo "Use this marker to verify injection."
BASH_EOF
chmod +x "$TEST_HOME/.claude/multiline_hook_a.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/multiline_hook_a.sh", "timeout": 10 }] }
    ]
  }
}
EOF

claude_start
# nexus-spwp2: the marker and the reply tokens are spelled in the prompt as
# pieces to be joined, never whole. The pane shows the prompt itself, so a whole
# marker written into it matches the grep below whether or not the hook
# injected anything (13a read "DID inject" off its own prompt).
claude_prompt "Use Task to dispatch the general-purpose agent. Description='13a probe'. Prompt: 'Examine your context. Is the text UNIQUE-PROJECT-A-MARKER- immediately followed by KQX73 (no space) anywhere in your context? If yes, reply with the word FOUND immediately followed by -13A (no space). If no, reply with the word NONE immediately followed by -13A (no space).'"
poll_for "FOUND-13A|NONE-13A" 180 "subagent reply token" || true

if capture -300 | grep -qE "FOUND-13A"; then
    pass "13a: project settings.json + bash multi-line plain stdout DID inject"
    PROJECT_MULTI_INJECTS=1
elif capture -300 | grep -qE "NONE-13A"; then
    pass "13a: project settings.json + bash multi-line plain stdout did NOT inject"
    PROJECT_MULTI_INJECTS=0
else
    fail "13a: indeterminate -- neither FOUND-13A nor NONE-13A seen"
    PROJECT_MULTI_INJECTS=-1
fi
claude_exit
scenario_end

# ── 13b: fake plugin with SubagentStart hook (mirrors nexus plugin structure)
scenario "13b plugin_hook_multiline: SubagentStart in fake plugin's hooks.json"

mkdir -p "$TEST_HOME/.claude/test-plugin/.claude-plugin"
mkdir -p "$TEST_HOME/.claude/test-plugin/hooks/scripts"

cat > "$TEST_HOME/.claude/test-plugin/.claude-plugin/plugin.json" <<'EOF'
{ "name": "test-plugin", "version": "0.0.1", "description": "fake plugin for hook injection test" }
EOF

cat > "$TEST_HOME/.claude/test-plugin/hooks/scripts/sub_hook.sh" <<'BASH_EOF'
#!/usr/bin/env bash
echo "## Test Section B (plugin)"
echo ""
echo "T1 scratch — UNIQUE-PLUGIN-B-MARKER-WYZ91"
echo "Use this marker to verify plugin-level injection."
BASH_EOF
chmod +x "$TEST_HOME/.claude/test-plugin/hooks/scripts/sub_hook.sh"

cat > "$TEST_HOME/.claude/test-plugin/hooks/hooks.json" <<'EOF'
{
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command",
                    "command": "bash $CLAUDE_PLUGIN_ROOT/hooks/scripts/sub_hook.sh",
                    "timeout": 10 }] }
    ]
  }
}
EOF

# nexus-spwp2: the fake plugin is installed through cc_plugin_require (runner.sh),
# not a hand-written installed_plugins.json, which Claude Code 2.1.302 does not
# load -- the old 13b ran with NO plugin, so "did NOT inject" was the only
# outcome it could ever see, and the verdict below read it as a fact about
# plugins. Settings first: the install merges enabledPlugins into them.
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" }
}
EOF

PLUGIN_INJECTS=-1   # -1: the plugin never loaded or its hook never ran, so no reading
if cc_plugin_require "$TEST_HOME/.claude/test-plugin" test-plugin local-marketplace; then
    claude_start
    # The reply tokens are spelled in the prompt as pieces to be joined, never
    # whole: the pane shows the prompt itself, so a whole marker written into it
    # would match the grep below whether or not the hook injected anything.
    claude_prompt "Use Task to dispatch the general-purpose agent. Description='13b probe'. Prompt: 'Examine your context. Is the text UNIQUE-PLUGIN-B-MARKER- immediately followed by WYZ91 (no space) anywhere in your context? If yes, reply with the word FOUND immediately followed by -13B (no space). If no, reply with the word NONE immediately followed by -13B (no space).'"
    poll_for "FOUND-13B|NONE-13B" 180 "subagent reply token" || true

    # "Did NOT inject" says something about plain-stdout plugin hooks only if the
    # hook ran, and only the debug log can say so (the transcript omits a hook
    # whose output was not injected).
    if ! cc_hook_ran "SubagentStart" "$CC_PLUGIN_ID" "sub_hook.sh"; then
        fail "13b: the debug log ($CC_DEBUG_FILE) shows no SubagentStart hook of $CC_PLUGIN_ID finishing -- the plugin loaded but its hook did not run, so neither reading below would mean anything"
    elif capture -300 | grep -qE "FOUND-13B"; then
        pass "13b: plugin-level SubagentStart plain stdout DID inject (plugin hooks differ from project)"
        PLUGIN_INJECTS=1
    elif capture -300 | grep -qE "NONE-13B"; then
        pass "13b: plugin-level SubagentStart plain stdout did NOT inject (no plugin/project diff)"
        PLUGIN_INJECTS=0
    else
        fail "13b: indeterminate -- neither FOUND-13B nor NONE-13B seen"
        capture -60 | sed 's/^/    | /'
    fi
    claude_exit
fi

cc_plugin_uninstall_all
scenario_end

# ── 13c: positive control with additionalContext JSON
scenario "13c positive_control_json: SubagentStart with additionalContext JSON (should inject)"

cat > "$TEST_HOME/.claude/settings.json" <<'EOF'
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" },
  "hooks": {
    "SubagentStart": [
      { "matcher": "",
        "hooks": [{ "type": "command",
                    "command": "python3 -c 'import json; print(json.dumps({\"hookSpecificOutput\":{\"hookEventName\":\"SubagentStart\",\"additionalContext\":\"UNIQUE-CONTROL-C-MARKER-RJF44\"}}))'" }] }
    ]
  }
}
EOF

claude_start
claude_prompt "Use Task to dispatch the general-purpose agent. Description='13c probe'. Prompt: 'Examine your context. Is the text UNIQUE-CONTROL-C-MARKER- immediately followed by RJF44 (no space) anywhere in your context? If yes, reply with the word FOUND immediately followed by -13C (no space). If no, reply with the word NONE immediately followed by -13C (no space).'"
poll_for "FOUND-13C|NONE-13C" 180 "subagent reply token" || true

if capture -300 | grep -qE "FOUND-13C"; then
    pass "13c: additionalContext JSON DID inject (positive control)"
else
    fail "13c: additionalContext JSON did NOT inject (or no reply token) — control failed, suspect harness"
fi
claude_exit
scenario_end

# ── Verdict
echo ""
echo "    ──────────── 13 verdict ────────────"
echo "    project bash multi-line: inject=$PROJECT_MULTI_INJECTS"
echo "    plugin bash multi-line:  inject=$PLUGIN_INJECTS"
if [[ $PLUGIN_INJECTS -lt 0 || $PROJECT_MULTI_INJECTS -lt 0 ]]; then
    echo "    → No verdict: 13a or 13b did not get a reading (see its failure above)."
elif [[ $PROJECT_MULTI_INJECTS -eq 0 && $PLUGIN_INJECTS -eq 0 ]]; then
    echo "    → Plain stdout never injects on SubagentStart (project OR plugin). Nexus subagent-start.sh is silently broken."
elif [[ $PROJECT_MULTI_INJECTS -eq 0 && $PLUGIN_INJECTS -eq 1 ]]; then
    echo "    → Plugin-level SubagentStart hooks inject plain stdout; project-level do NOT. Nexus is fine."
elif [[ $PROJECT_MULTI_INJECTS -eq 1 && $PLUGIN_INJECTS -eq 0 ]]; then
    echo "    → Project-level SubagentStart plain stdout injects; plugin-level does NOT (hook ran, per the debug log). A plugin hook must use the JSON envelope."
elif [[ $PROJECT_MULTI_INJECTS -eq 1 && $PLUGIN_INJECTS -eq 1 ]]; then
    echo "    → Plain stdout injects in both — scenario 01's single-line probe was a bad test."
else
    echo "    → Unexpected mix — investigate further."
fi
