#!/usr/bin/env bash
# UNVERIFIED on 2026-08-28 (nexus-qs1g6): written and self-checked (bash -n,
# house conventions, non-vacuity guards) but never run to a live verdict on this
# box — the harness's OAuth keychain entry ('Claude Code-credentials') carried
# expiresAt: 0 and scenario 16 was blocked the same way. First real run:
#   tests/cc-validation/runner.sh --scenario "16,28"   (after `claude /login`)
# A pass here is what closes nexus-qs1g6's remaining finding (T1 b4aebceb).
# Scenario 28 — does a PreToolUse hook's permissionDecision actually GOVERN
# tool execution under `defaultMode: auto` (no --dangerously-skip-permissions)?
#
# nexus-qs1g6 follow-up (substantive-critic round 1, T1 b4aebceb): the fix
# shipped at 91c0cddeb/476fcaa1b registers auto-approve-nx-mcp.sh on BOTH
# PermissionRequest and PreToolUse, on the documented theory that a PreToolUse
# permissionDecision:allow response lands BEFORE the auto-mode classifier
# (whereas PermissionRequest fires only when a prompt would be shown, and
# scenario 16 already proved auto mode never reaches that prompt at all —
# the classifier decides first and the PermissionRequest hook is never
# consulted). That ordering claim was verified only at the shell-script/unit
# level (tests/hooks/test_permission_request_hooks.py asserts the JSON SHAPE
# the hook script emits); no live cc-validation scenario had exercised the
# new PreToolUse arm the way 16 exercised the old PermissionRequest arm.
#
# A bare positive test (register an "allow" PreToolUse hook, watch the tool
# run) would NOT be enough: scenario 16 already showed auto mode auto-runs
# MCP tools with NO hook involved at all ("auto mode auto-approves MCP tools
# ... without consulting [the] hook"). So "tool ran" with an allow hook
# present is equally consistent with (a) the hook's decision governing, or
# (b) auto mode's own leniency running the tool regardless of any hook.
# Only a NEGATIVE control disambiguates: if a PreToolUse hook can emit
# permissionDecision:deny and the tool is BLOCKED anyway, that proves the
# hook's decision is consulted and honored (not just fired-and-ignored) —
# which is the load-bearing half of "lands before the classifier" for THIS
# bead's fix (the allowlist would be meaningless if auto mode ran the tool
# regardless of what the hook said).
#
#   28a: PreToolUse hook emits permissionDecision:allow, NO permissions.allow
#        rule for the tool → tool must RUN, hook must have FIRED.
#   28b: PreToolUse hook emits permissionDecision:deny,  NO permissions.allow
#        rule for the tool → tool must NOT run, hook must have FIRED (proves
#        the deny was consulted, not that the server never connected).

# Custom claude launcher for this scenario — uses --permission-mode=auto, no
# bypass flag. Copied from scenario 16 rather than relying on 16 having run
# first in the same suite invocation (this scenario must also pass under
# `runner.sh --scenario 28` in isolation, where 16's function definition
# would never have been sourced).
claude_start_auto() {
    # Mirror the standard claude_start wrapper's trust pre-seed + --mcp-config so
    # the stub actually connects. Without this the tool never runs (tool_ran=0)
    # and any verdict below would be vacuous (it would "pass" with the MCP
    # stack completely broken). _preseed_trust / _prepare_mcp_args are defined
    # in runner.sh and in scope here.
    _preseed_trust 2>/dev/null || true
    local _extra; _extra="$(_prepare_mcp_args 2>/dev/null || true)"
    send_keys "claude --permission-mode=auto ${_extra}" Enter
    sleep 8
    local deadline=$(( $(date +%s) + 60 ))
    local _trust_done=0
    while [[ $(date +%s) -lt $deadline ]]; do
        local pane; pane=$(capture)
        if [[ $_trust_done -eq 0 ]] && echo "$pane" | grep -qiE "trust this folder|project you trust"; then
            echo "    [auth] trust — accept"
            _tmux send-keys -t "${TMUX_SESSION}" Enter
            _trust_done=1; sleep 2
        elif echo "$pane" | grep -qiE "custom API key"; then
            _tmux send-keys -t "${TMUX_SESSION}" Enter; sleep 5
        elif echo "$pane" | grep -qiE "Type a message|auto.*on|❯ "; then
            break
        fi
        sleep 1
    done
    sleep 5
}

# PreToolUse hook scripts. README "Hooks" section: PreToolUse command hooks do
# NOT inherit the harness env, so $HOOK_LOG must be baked in at WRITE time via
# an unquoted heredoc — only $HOOK_LOG expands now; $(cat)/$(date) are escaped
# so they expand at RUNTIME inside the hook process instead.
cat > "$TEST_HOME/.claude/pretooluse_allow_hook.sh" <<BASH_EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s)] PRETOOLUSE_ALLOW_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"scenario-28-allow"}}))'
BASH_EOF
chmod +x "$TEST_HOME/.claude/pretooluse_allow_hook.sh"

cat > "$TEST_HOME/.claude/pretooluse_deny_hook.sh" <<BASH_EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s)] PRETOOLUSE_DENY_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"scenario-28-deny"}}))'
BASH_EOF
chmod +x "$TEST_HOME/.claude/pretooluse_deny_hook.sh"

# Need .mcp.json at workspace root for stub MCP to load (README "MCP servers").
cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOF

# ── 28a: PreToolUse allow, NO permissions.allow rule → tool must run ─────────
scenario "28a pretooluse_allow: defaultMode=auto, PreToolUse hook emits allow, NO permissions.allow rule"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "permissions": { "allow": [], "defaultMode": "auto" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/pretooluse_allow_hook.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
send_keys "cd $TEST_HOME" Enter; sleep 0.3
claude_start_auto
# WARMUP (see README "Deferred MCP tools"): MCP tools are deferred — the first
# call after launch races tool-schema discovery. A throwaway list-tools turn
# forces the model to load the mcp__stub__ schema first; the measured call
# below is then deterministic (same fix as scenario 16).
claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
claude_wait 30
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

hook_fired_a=0
grep -q "PRETOOLUSE_ALLOW_FIRED" "$HOOK_LOG" 2>/dev/null && hook_fired_a=1
tool_ran_a=0
[[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_a=1
server_connected_a=0
[[ -s "$STUB_LOG" ]] && grep -q '"event": "mcp_imported_ok"' "$STUB_LOG" && server_connected_a=1

echo "    28a: hook_fired=$hook_fired_a  tool_ran=$tool_ran_a  server_connected=$server_connected_a"
claude_exit

# ── 28b: PreToolUse deny, NO permissions.allow rule → tool must NOT run ──────
scenario "28b pretooluse_deny: defaultMode=auto, PreToolUse hook emits deny, NO permissions.allow rule"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "permissions": { "allow": [], "defaultMode": "auto" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "mcp__stub__.*",
        "hooks": [{ "type": "command", "command": "bash $TEST_HOME/.claude/pretooluse_deny_hook.sh" }] }
    ]
  }
}
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
claude_start_auto
# Same warmup as 28a — the deny sub-run must race schema discovery identically
# so a false "tool_ran=0" here can't be blamed on a slower connection.
claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
claude_wait 30
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

hook_fired_b=0
grep -q "PRETOOLUSE_DENY_FIRED" "$HOOK_LOG" 2>/dev/null && hook_fired_b=1
tool_ran_b=0
[[ -s "$STUB_LOG" ]] && grep -q '"tool": "ping"' "$STUB_LOG" && tool_ran_b=1
server_connected_b=0
[[ -s "$STUB_LOG" ]] && grep -q '"event": "mcp_imported_ok"' "$STUB_LOG" && server_connected_b=1

echo "    28b: hook_fired=$hook_fired_b  tool_ran=$tool_ran_b  server_connected=$server_connected_b"
claude_exit
send_keys "cd $REPO_ROOT" Enter; sleep 0.3

# ── verdict ───────────────────────────────────────────────────────────────
echo ""
echo "    ──────────── 28 verdict (PreToolUse permissionDecision governs auto mode) ────────────"
echo "    28a (allow): hook_fired=$hook_fired_a  tool_ran=$tool_ran_a  server_connected=$server_connected_a"
echo "    28b (deny):  hook_fired=$hook_fired_b  tool_ran=$tool_ran_b  server_connected=$server_connected_b"

# VALIDITY NOTE: two non-vacuity guards, mirroring scenario 16's tool_ran
# requirement.
#   1. server_connected must be 1 in BOTH sub-runs — otherwise a "tool did
#      not run" reading in 28b is indistinguishable from "the MCP stack was
#      never up", not a real deny.
#   2. hook_fired must be 1 in BOTH sub-runs — a hook that never fired can't
#      have governed anything; a green 28b verdict with hook_fired_b=0 would
#      mean auto mode denied the tool on its OWN classifier leniency (or lack
#      thereof), which says nothing about whether a PreToolUse decision is
#      consulted at all.
if [[ $server_connected_a -eq 0 || $server_connected_b -eq 0 ]]; then
    fail "MCP stub server never connected in a sub-run (a=$server_connected_a b=$server_connected_b) — verdict would be vacuous"
elif [[ $hook_fired_a -eq 0 || $hook_fired_b -eq 0 ]]; then
    fail "PreToolUse hook did not fire in a sub-run (a=$hook_fired_a b=$hook_fired_b) — cannot assess whether its decision governs"
elif [[ $tool_ran_a -eq 1 && $tool_ran_b -eq 0 ]]; then
    pass "PreToolUse permissionDecision GOVERNS in auto mode: allow ran the tool, deny blocked it (both with no permissions.allow rule and no prompt to accept/reject) — the fix's ordering claim (PreToolUse allow lands before the auto-mode classifier) is confirmed live, not just at the hook-script/unit level"
elif [[ $tool_ran_a -eq 1 && $tool_ran_b -eq 1 ]]; then
    fail "REAL FINDING, do not mask: PreToolUse deny did NOT block the tool in auto mode — the hook fired but auto mode's classifier overrode/ignored its decision. This means nexus-qs1g6's PreToolUse allowlist fix rides on auto mode's OWN leniency, not on the hook's decision being consulted; the allowlist's decision may not be what governs after all."
elif [[ $tool_ran_a -eq 0 ]]; then
    fail "PreToolUse allow did NOT run the tool in auto mode (hook_fired_a=$hook_fired_a) — the fix's core claim (PreToolUse allow governs) does not hold; investigate the exact hookSpecificOutput shape auto-approve-nx-mcp.sh emits vs what this scenario emits"
else
    fail "unexpected combination: tool_ran_a=$tool_ran_a tool_ran_b=$tool_ran_b"
fi

rm -f "$TEST_HOME/.mcp.json"
scenario_end
