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

# ── Sub-run A: allow wildcard PRESENT — does hook fire?
scenario "07a perms_preempt: with allow wildcard, does PermissionRequest hook still fire?"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "default" },
  "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
  },
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
  "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
  },
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

if [[ $hook_fired_with_allow -eq 0 && $hook_fired_without_allow -eq 1 ]]; then
    pass "wildcard PREEMPTS the PermissionRequest hook (hook fires only when no allow rule)"
elif [[ $hook_fired_with_allow -eq 1 && $hook_fired_without_allow -eq 1 ]]; then
    pass "wildcard does NOT preempt — hook fires regardless (auto-approve-nx-mcp.sh would still run if kept)"
elif [[ $hook_fired_with_allow -eq 0 && $hook_fired_without_allow -eq 0 ]]; then
    fail "PermissionRequest hook never fires — possibly skipDangerousMode bypasses gate entirely"
else
    fail "unexpected: hook fires WITH allow but not WITHOUT — investigate"
fi

scenario_end
