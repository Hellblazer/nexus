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
claude_prompt "Use Task to dispatch the general-purpose agent. Description='13a probe'. Prompt: 'Examine your context. Is the literal text UNIQUE-PROJECT-A-MARKER-KQX73 anywhere in your context? Reply YES with the marker or NO.'"
claude_wait 90

if capture -300 | grep -qE "UNIQUE-PROJECT-A-MARKER-KQX73"; then
    pass "13a: project settings.json + bash multi-line plain stdout DID inject"
    PROJECT_MULTI_INJECTS=1
else
    pass "13a: project settings.json + bash multi-line plain stdout did NOT inject"
    PROJECT_MULTI_INJECTS=0
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

NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<EOF
{ "version": 2, "plugins": {
  "test-plugin@local-marketplace": [
    { "scope": "user", "installPath": "$TEST_HOME/.claude/test-plugin", "version": "dev",
      "installedAt": "$NOW", "lastUpdated": "$NOW" }
  ]
}}
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "enabledPlugins": { "test-plugin@local-marketplace": true },
  "permissions": { "allow": ["Task"], "defaultMode": "acceptEdits" }
}
EOF

claude_start
claude_prompt "Use Task to dispatch the general-purpose agent. Description='13b probe'. Prompt: 'Examine your context. Is the literal text UNIQUE-PLUGIN-B-MARKER-WYZ91 anywhere in your context? Reply YES with the marker or NO.'"
claude_wait 90

if capture -300 | grep -qE "UNIQUE-PLUGIN-B-MARKER-WYZ91"; then
    pass "13b: plugin-level SubagentStart plain stdout DID inject (plugin hooks differ from project)"
    PLUGIN_INJECTS=1
else
    pass "13b: plugin-level SubagentStart plain stdout did NOT inject (no plugin/project diff)"
    PLUGIN_INJECTS=0
fi
claude_exit

# Reset plugins
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF
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
claude_prompt "Use Task to dispatch the general-purpose agent. Description='13c probe'. Prompt: 'Examine your context. Is the literal text UNIQUE-CONTROL-C-MARKER-RJF44 in your context? Reply YES or NO.'"
claude_wait 90

if capture -300 | grep -qE "UNIQUE-CONTROL-C-MARKER-RJF44"; then
    pass "13c: additionalContext JSON DID inject (positive control)"
else
    fail "13c: additionalContext JSON did NOT inject — control failed, suspect harness"
fi
claude_exit
scenario_end

# ── Verdict
echo ""
echo "    ──────────── 13 verdict ────────────"
echo "    project bash multi-line: inject=$PROJECT_MULTI_INJECTS"
echo "    plugin bash multi-line:  inject=$PLUGIN_INJECTS"
if [[ $PROJECT_MULTI_INJECTS -eq 0 && $PLUGIN_INJECTS -eq 0 ]]; then
    echo "    → Plain stdout never injects on SubagentStart (project OR plugin). Nexus subagent-start.sh is silently broken."
elif [[ $PROJECT_MULTI_INJECTS -eq 0 && $PLUGIN_INJECTS -eq 1 ]]; then
    echo "    → Plugin-level SubagentStart hooks inject plain stdout; project-level do NOT. Nexus is fine."
elif [[ $PROJECT_MULTI_INJECTS -eq 1 && $PLUGIN_INJECTS -eq 1 ]]; then
    echo "    → Plain stdout injects in both — scenario 01's single-line probe was a bad test."
else
    echo "    → Unexpected mix — investigate further."
fi
