#!/usr/bin/env bash
# Scenario 07 — does permissions.allow wildcard preempt PermissionRequest hook?
# Two sub-runs: with allow rule (hook should NOT fire if preempted), without (hook SHOULD fire).
# Critical for the "delete auto-approve-nx-mcp.sh" decision.

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
claude_start
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
claude_start
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

claude_exit

# ── Verdict
echo "    sub-A (allow present): hook_fired=$hook_fired_with_allow tool_ran=$tool_ran_with_allow"
echo "    sub-B (allow absent):  hook_fired=$hook_fired_without_allow tool_ran=$tool_ran_without_allow"

# VALIDITY NOTE (reworked 2026-05-31): the hook-firing signal is only meaningful
# if the tool actually RAN — a tool that never reached the permission gate can't
# exercise a PermissionRequest hook, so "hook never fires" would be vacuous. With
# the stub now connected via .mcp.json + --mcp-config, tool_ran=1 confirms the
# call reached the gate, so hook_fired is a real measurement. The empirically
# observed outcome (tool runs both ways, hook never fires) is the answer to this
# scenario's question (the auto-approve-sn/nx-mcp decision): under
# skipDangerousModePermissionPrompt the gate is bypassed entirely, so a
# PermissionRequest-based auto-approver is redundant. That is a PASS, not a fail.
if [[ $tool_ran_with_allow -eq 0 || $tool_ran_without_allow -eq 0 ]]; then
    fail "tool did not run in at least one sub-run — server not connected; cannot assess the PermissionRequest gate (would be vacuous)"
elif [[ $hook_fired_with_allow -eq 0 && $hook_fired_without_allow -eq 0 ]]; then
    pass "skipDangerousMode bypasses the PermissionRequest gate: tool auto-runs with AND without an allow rule, hook never fires (a PermissionRequest auto-approver is therefore redundant under skipDangerousMode)"
elif [[ $hook_fired_with_allow -eq 0 && $hook_fired_without_allow -eq 1 ]]; then
    pass "allow wildcard PREEMPTS the PermissionRequest hook (fires only when no allow rule)"
elif [[ $hook_fired_with_allow -eq 1 && $hook_fired_without_allow -eq 1 ]]; then
    pass "PermissionRequest hook fires regardless of allow rule (an auto-approver would still run if kept)"
else
    fail "unexpected: hook fires WITH allow but not WITHOUT — investigate"
fi

scenario_end
