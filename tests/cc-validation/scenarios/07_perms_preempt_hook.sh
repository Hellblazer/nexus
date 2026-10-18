#!/usr/bin/env bash
# Scenario 07 — with an allow rule for an MCP tool present, does the PermissionRequest
# hook for it still fire? Two sub-runs: with the allow rule (A), without (B).
#
# THIS IS AN OBSERVATION, NOT A CONTRACT. Whether an allow rule pre-empts a
# PermissionRequest hook has flipped across Claude Code versions, so the scenario reports
# what the build under test did and compares it with the answer recorded for the same build
# in docs/tables/cc-version-answers.toml (cc_preempt_verdict, runner.sh): the same answer
# passes, a different one FAILS "answer changed since <version>", a version with no row prints
# INFO "record this version's answer". Nothing may be deleted or relied on from either answer;
# re-measure on every Claude Code pin bump. In particular the nx_hook_shim auto-approve verb
# (registered on PreToolUse and PermissionRequest) stays as a hedge whichever way this lands.
#
# nexus-qsmc5: this scenario pins defaultMode "default" and used to launch through
# claude_start, which passes --dangerously-skip-permissions: under that flag the permission
# gate is bypassed entirely, so the tool ran with AND without an allow rule and the
# PermissionRequest hook never fired, and the old verdict reported that as an answer. It now
# launches with claude_start_enforced (no bypass flag), so the gate is real, and
# cc_preempt_verdict treats sub-run B (no allow rule) as the control: the call must reach a
# permission gate there, either the hook firing or the prompt appearing. A pending prompt is
# dismissed before exit. Each sub-run's evidence (hook log, stub log, pane tail, transcripts,
# settings) and the verdict lines are kept (cc_keep_evidence, cc_note).

cat > "$TEST_HOME/.claude/perm_hook.sh" <<'EOF'
#!/usr/bin/env bash
INPUT=$(cat)
echo "[$(date +%s)] PERM_HOOK_FIRED: $INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}))'
EOF
chmod +x "$TEST_HOME/.claude/perm_hook.sh"

# Stub MCP server in .mcp.json (NOT settings.json.mcpServers, which CC silently
# ignores — see README trap #2). The runner's claude_start wrapper normalizes
# the python3 launcher to the repo venv and launches with --mcp-config so the
# server actually connects; otherwise the mcp__stub__ping call never executes
# and the PermissionRequest hook has nothing to gate (the old confounded fail).
# Written once here; it persists across both sub-runs (no reset between them).
cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOF

# ── Sub-run A: allow wildcard PRESENT — does hook fire?
scenario "07a perms_preempt: with allow wildcard, does PermissionRequest hook still fire?"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "default" },
  "hooks": {
    "PermissionRequest": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/perm_hook.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
cc_reset_transcripts
claude_start_enforced
# Deferred-tool warmup (see README "Deferred MCP tools"): load the schema before
# the measured call so it doesn't race discovery.
claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
claude_wait 30
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

hook_fired_with_allow=0
[[ -s "$HOOK_LOG" ]] && hook_fired_with_allow=1
tool_ran_with_allow=0
[[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_with_allow=1
prompt_with_allow=0
cc_permission_prompt_up && prompt_with_allow=1

cc_save_pane 60
cc_keep_evidence "07-allow-present"
cc_dismiss_permission_prompt
claude_exit

# ── Sub-run B: allow wildcard ABSENT — does hook fire?
scenario "07b perms_preempt: WITHOUT allow wildcard, does PermissionRequest hook fire?"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": [], "defaultMode": "default" },
  "hooks": {
    "PermissionRequest": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/perm_hook.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
cc_reset_transcripts
claude_start_enforced
# Deferred-tool warmup (see README "Deferred MCP tools"): load the schema before
# the measured call so it doesn't race discovery.
claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
claude_wait 30
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

hook_fired_without_allow=0
[[ -s "$HOOK_LOG" ]] && hook_fired_without_allow=1
tool_ran_without_allow=0
[[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_without_allow=1
prompt_without_allow=0
cc_permission_prompt_up && prompt_without_allow=1

cc_save_pane 60
cc_keep_evidence "07-allow-absent"
cc_dismiss_permission_prompt
claude_exit

# ── Verdict (cc_preempt_verdict, runner.sh): sub-run B is the control that the
# permission gate is real; sub-run A then reports whether the hook fired with the allow rule
# present, for this Claude Code build.
cc_preempt_verdict "$hook_fired_with_allow" "$tool_ran_with_allow" "$prompt_with_allow" \
    "$hook_fired_without_allow" "$tool_ran_without_allow" "$prompt_without_allow"

scenario_end
