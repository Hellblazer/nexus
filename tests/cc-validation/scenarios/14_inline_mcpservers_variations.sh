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

# Positive control for 14b (nexus-spwp2 review): 14b's "inline mcpServers do NOT
# load" reading is only a finding if, in THIS run, the same stub definition
# loaded for a project-level agent (14a). Without that, "stub absent" is just as
# likely a stub that cannot start in this environment (the container once lacked
# the mcp package), so 14b is SKIP, not PASS.
CTRL_14A_LOADED=0

# ── 14a: inline mcpServers, NO tools: filter — does removing the filter fix it?
scenario "14a inline_no_tools_filter: remove the tools: frontmatter restriction"

write_agent "agent14a" /dev/stdin <<EOF
---
name: agent14a
description: validation, no tools filter
mcpServers:
  - stub:
      type: stdio
      command: $REPO_ROOT/.venv/bin/python
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

# ROOT CAUSE (2026-05-31): this scenario was intermittently failing because the
# inline-agent mcpServers `command` was bare `python3`, which resolves to a
# python that may lack the `mcp` module (homebrew python3.13) — the server then
# crashed on import ("No module named 'mcp'", proven by the stub's startup
# markers in STUB_LOG) and the tool never loaded. The "flakiness" was python3
# resolving to different interpreters across runs. The --mcp-config wrapper
# normalizes python3->venv for $TEST_HOME/.mcp.json ONLY, not for agent
# frontmatter, so the inline command is pinned to $REPO_ROOT/.venv/bin/python
# directly above. The verdict accepts EITHER signal of a loaded server: the stub
# in the agent's self-listed inventory (mcp_in_inv=1; note deferred tools may not
# self-list until loaded, hence the schema-load instruction in the agent prompt)
# OR the call landing in STUB_LOG (stub_called=1). Both absent = server did not
# load.
if [[ $mcp_in_inv -eq 1 || $log_called -eq 1 ]]; then
    CTRL_14A_LOADED=1
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
      command: $REPO_ROOT/.venv/bin/python
      args: ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"]
      env:
        STUB_LOG: "$STUB_LOG"
---

Save your tool inventory to ${TEST_HOME}/14b_tools.txt using Write.
Then call mcp__stub__record with payload='14b-PROOF'. Reply 14B_DONE.
EOF

# nexus-spwp2: installed through cc_plugin_require (runner.sh), not a
# hand-written installed_plugins.json, which Claude Code 2.1.302 does not load.
# Settings first: the install merges enabledPlugins into them.
cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/14b_tools.txt"

if cc_plugin_require "$TEST_HOME/.claude/test-plugin" test-plugin local-marketplace; then
claude_start
claude_prompt "Use Task to dispatch agent14b. Description='14b'. Prompt: 'Run your instructions exactly.'"
claude_wait 90

log_inventory "14b" "$TEST_HOME/14b_tools.txt"
log_called=0; [[ -s "$STUB_LOG" ]] && grep -q "14b-PROOF" "$STUB_LOG" && log_called=1
mcp_in_inv=0; [[ -f "$TEST_HOME/14b_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/14b_tools.txt" && mcp_in_inv=1
agent_ran=0; [[ -f "$TEST_HOME/14b_tools.txt" ]] && agent_ran=1
echo "    14b verdict: agent_ran=$agent_ran  mcp_in_inv=$mcp_in_inv  stub_called=$log_called"

# VALIDITY NOTE (root-caused 2026-05-31, restated by nexus-spwp2): this tests a
# config CC explicitly DISALLOWS -- the docs state hooks/mcpServers/permissionMode
# are not supported on plugin-shipped agents (security). Until nexus-spwp2 the
# plugin was never loaded at all (a hand-written installed_plugins.json is not
# read by Claude Code 2.1.302), which is why agent14b was "not registered" and
# this part could only skip. With the plugin loaded (cc_plugin_require above, a
# FAIL otherwise) a missing agent is a real result about plugin agents, still
# reported as a SKIP only when agent14b did not run.
if [[ $mcp_in_inv -eq 1 && $log_called -eq 1 ]]; then
    pass "14b: plugin-shipped agent inline mcpServers WORK (server loaded, tool used)"
elif [[ $agent_ran -eq 1 && $mcp_in_inv -eq 0 && $CTRL_14A_LOADED -eq 1 ]]; then
    pass "14b: plugin-shipped agent inline mcpServers do NOT load (agent ran, stub absent from inventory) — vs 14a project-level which loads"
elif [[ $agent_ran -eq 1 && $mcp_in_inv -eq 0 ]]; then
    skip "14b: agent14b ran and its inventory lacks the stub tool, but 14a (the positive control: the same stub, project-level agent) did not show the stub loading in this run, so the stub may simply be unable to start here; not a verdict on plugin-agent mcpServers"
elif [[ $agent_ran -eq 1 ]]; then
    skip "14b: agent14b (registered by the marketplace-installed plugin) ran and listed the stub tool but did not call it (deferred MCP tool schema not loaded, see the README); not a verdict on inline mcpServers"
else
    skip "14b: plugin-shipped agent 'agent14b' did not run although the plugin loaded (see the pane); not a verdict on inline mcpServers"
fi
claude_exit
fi

cc_plugin_uninstall_all
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
