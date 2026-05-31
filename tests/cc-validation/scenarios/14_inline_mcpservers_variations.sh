#!/usr/bin/env bash
# Scenario 14 — investigate why inline agent mcpServers didn't spawn in interactive
# (despite working in -p). Try 4 variations.

# Reset stub log path expansion check helper
log_inventory() {
    local label="$1"
    local agent_tools_file="$2"
    if [[ -f "$agent_tools_file" ]]; then
        local mcp_count
        mcp_count=$(grep -cE "mcp__" "$agent_tools_file" 2>/dev/null || echo 0)
        local total
        total=$(wc -l < "$agent_tools_file" | tr -d ' ')
        echo "    [$label] tools file: $total lines, $mcp_count mcp__ entries"
        head -10 "$agent_tools_file" | sed 's/^/    | /'
    else
        echo "    [$label] tools file MISSING"
    fi
}

# ── 14a: inline mcpServers, NO tools: filter — does removing the filter fix it?
scenario "14a inline_no_tools_filter: remove the tools: frontmatter restriction"

write_agent "agent14a" /dev/stdin <<EOF
---
name: agent14a
description: validation, no tools filter
mcpServers:
  - stub:
      type: stdio
      command: python3
      args: ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"]
      env:
        STUB_LOG: "$STUB_LOG"
---

First load the mcp__stub__record tool: it is a DEFERRED MCP tool, so if it is not
already callable, use ToolSearch to load its schema before calling it. Then call
mcp__stub__record with payload='14a-PROOF'. Also save your available tool inventory
to ${TEST_HOME}/14a_tools.txt (one per line) using the Write tool. Reply 14A_DONE.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/14a_tools.txt"

claude_start
claude_prompt "Use Task to dispatch agent14a. Description='14a'. Prompt: 'Run your instructions exactly.'"
claude_wait 90

log_inventory "14a" "$TEST_HOME/14a_tools.txt"
log_called=0; [[ -s "$STUB_LOG" ]] && grep -q "14a-PROOF" "$STUB_LOG" && log_called=1
mcp_in_inv=0; [[ -f "$TEST_HOME/14a_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/14a_tools.txt" && mcp_in_inv=1
echo "    14a verdict: mcp_in_inv=$mcp_in_inv  stub_called=$log_called"

# VALIDITY NOTE (reworked 2026-05-31): the question is whether the agent's inline
# mcpServers LOADED. EITHER signal proves it: the stub appears in the agent's
# self-listed inventory (mcp_in_inv=1 — schema loaded and visible) OR the agent's
# call landed in STUB_LOG (stub_called=1 — callable). mcp__stub__ is a DEFERRED
# tool, so the agent prompt instructs loading its schema first (same deferred-tool
# root cause as scenario 16). Whether the subagent model then follows through on
# the call is variable and NOT what this scenario tests — loading is. Only both
# signals absent means the inline server genuinely did not load.
if [[ $mcp_in_inv -eq 1 || $log_called -eq 1 ]]; then
    pass "14a: inline mcpServers (no tools filter) LOADED for the agent (mcp_in_inv=$mcp_in_inv, stub_called=$log_called — either proves load)"
else
    fail "14a: inline mcpServers did NOT load for the project-level subagent (tool neither listed nor callable)"
fi

claude_exit
scenario_end

# ── 14b: agent in fake-plugin agents/ dir (mirror how nexus ships agents)
scenario "14b plugin_agent: same agent definition shipped via a plugin"

mkdir -p "$TEST_HOME/.claude/test-plugin/.claude-plugin"
mkdir -p "$TEST_HOME/.claude/test-plugin/agents"

cat > "$TEST_HOME/.claude/test-plugin/.claude-plugin/plugin.json" <<'EOF'
{ "name": "test-plugin", "version": "0.0.1", "description": "fake plugin" }
EOF

cat > "$TEST_HOME/.claude/test-plugin/agents/agent14b.md" <<EOF
---
name: agent14b
description: validation via plugin agent
mcpServers:
  - stub:
      type: stdio
      command: python3
      args: ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"]
      env:
        STUB_LOG: "$STUB_LOG"
---

Save your tool inventory to ${TEST_HOME}/14b_tools.txt using Write.
Then call mcp__stub__record with payload='14b-PROOF'. Reply 14B_DONE.
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
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/14b_tools.txt"

claude_start
claude_prompt "Use Task to dispatch agent14b. Description='14b'. Prompt: 'Run your instructions exactly.'"
claude_wait 90

log_inventory "14b" "$TEST_HOME/14b_tools.txt"
log_called=0; [[ -s "$STUB_LOG" ]] && grep -q "14b-PROOF" "$STUB_LOG" && log_called=1
mcp_in_inv=0; [[ -f "$TEST_HOME/14b_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/14b_tools.txt" && mcp_in_inv=1
agent_ran=0; [[ -f "$TEST_HOME/14b_tools.txt" ]] && agent_ran=1
echo "    14b verdict: agent_ran=$agent_ran  mcp_in_inv=$mcp_in_inv  stub_called=$log_called"

# VALIDITY NOTE (root-caused 2026-05-31): agent14b never runs because the
# plugin-shipped AGENT is not registered. The sandbox installs the fake plugin
# via installed_plugins.json + enabledPlugins, which loads plugin HOOKS (proven
# by scenario 13b) but NOT plugin AGENTS — the dispatch errors with "Agent type
# 'agent14b' not found. Available agents: agent14a, ...". So this scenario cannot
# exercise its intended behavior in this harness: that is a SKIP (untestable),
# not a pass (we verified nothing) and not a fail (no CC defect under test). If
# agent14b ever DOES run, report the real result. Registering plugin agents would
# need a marketplace-style install, out of scope for the manual-install harness.
if [[ $mcp_in_inv -eq 1 && $log_called -eq 1 ]]; then
    pass "14b: plugin-shipped agent inline mcpServers WORK (server loaded, tool used)"
elif [[ $agent_ran -eq 1 && $mcp_in_inv -eq 0 ]]; then
    pass "14b: plugin-shipped agent inline mcpServers do NOT load (agent ran, stub absent from inventory) — vs 14a project-level which loads"
else
    skip "14b: plugin-shipped agent 'agent14b' is not registered by the manual plugin install (hooks load, agents do not — 'Agent type not found'). Untestable in this harness; needs a marketplace-style install to register plugin agents."
fi

# Reset plugins
cat > "$TEST_HOME/.claude/plugins/installed_plugins.json" <<'EOF'
{"version": 2, "plugins": {}}
EOF
claude_exit
scenario_end

# ── 14c: project-level .mcp.json + agent string-reference (no inline)
scenario "14c string_reference: project .mcp.json + agent string-reference"

cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio", "command": "python3",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } }
} }
EOF

write_agent "agent14c" /dev/stdin <<EOF
---
name: agent14c
description: validation via mcpServers string ref
mcpServers:
  - stub
---

Save your tool inventory to ${TEST_HOME}/14c_tools.txt using Write.
Then call mcp__stub__record with payload='14c-PROOF'. Reply 14C_DONE.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/14c_tools.txt"

# cd into TEST_HOME so .mcp.json is at the workspace root
send_keys "cd $TEST_HOME" Enter; sleep 0.3
claude_start
claude_prompt "Use Task to dispatch agent14c. Description='14c'. Prompt: 'Run your instructions exactly.'"
claude_wait 90

log_inventory "14c" "$TEST_HOME/14c_tools.txt"
log_called=0; [[ -s "$STUB_LOG" ]] && grep -q "14c-PROOF" "$STUB_LOG" && log_called=1
mcp_in_inv=0; [[ -f "$TEST_HOME/14c_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/14c_tools.txt" && mcp_in_inv=1
echo "    14c verdict: mcp_in_inv=$mcp_in_inv  stub_called=$log_called"

if [[ $mcp_in_inv -eq 1 && $log_called -eq 1 ]]; then
    pass "14c: agent string-reference to project .mcp.json server works"
else
    fail "14c: string-reference failed; project .mcp.json may not be loading either"
fi

claude_exit
send_keys "cd $REPO_ROOT" Enter; sleep 0.3
rm -f "$TEST_HOME/.mcp.json"
scenario_end
