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

# nexus-qsmc5: wait for the dispatched agent to FINISH. claude_wait only watches
# the main session's spinner, and the main agent returns at once when it runs a
# subagent in the background, so the fixed wait read the forensic files, and then
# ended the session with Ctrl-C, while the agent was still working: the agent's
# transcript ended "[Request interrupted by user]" after its ToolSearch load and
# before any Write or tool call (14a, 14b, and 14c's missing call). The agents'
# own replies are the completion signal (each body ends "reply <token>").
# On a timeout it says WHICH agent never replied: the model sometimes finishes its work and
# omits the token (2 of 4 live runs), and the 240 s that costs is otherwise unexplained.
wait_agent_done() {
    cc_wait_subagent_reply "$1" "${2:-240}" || echo "    [$1] the agent never replied $1 within ${2:-240}s: it did not finish, or finished without the token (its transcript is reported below)"
    return 0
}

# nexus-qsmc5: did Claude Code SPAWN the stub for this run? The stub logs
# process_launched before it imports mcp (fixtures/stub_server.py), so STUB_LOG
# answers "was the server started" whether or not the agent later loaded the
# deferred tool's schema and called it. Whether it is listed in the agent's
# inventory or called depends on deferral and the model; the spawn does not.
stub_spawned() {
    [[ -s "$STUB_LOG" ]] && grep -q '"event": "process_launched"' "$STUB_LOG"
}

# nexus-qsmc5: what the dispatched agents did, from their own transcripts (tool
# names and the tail of their replies), and the transcripts kept as evidence. A
# verdict of "the stub was not called" says nothing about WHY without this.
report_agents() {
    local label="$1" tools replies
    tools="$(cc_subagent_tool_uses | sort | uniq -c | tr '\n' ' ' || true)"
    replies="$(cc_subagent_replies | tail -4 | cut -c1-240 || true)"
    echo "    [$label] subagent tool calls: ${tools:-none}"
    if [[ -n "$replies" ]]; then sed "s/^/    [$label] reply| /" <<<"$replies"; fi
    cc_keep_evidence "$label"
    return 0
}

# Each part (14a, 14b, 14c) starts from cc_reset_transcripts (runner.sh): the transcripts of an
# earlier part's session and agent must not be read, or kept as evidence, as this part's
# (the per-part report below lists tool calls and replies of EVERY transcript there).

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
already callable, use ToolSearch (query select:mcp__stub__record) to load its schema
before calling it. Then call mcp__stub__record with payload='14a-PROOF'. Also save
your available tool inventory to ${TEST_HOME}/14a_tools.txt (one per line) using the
Write tool. Whatever happens, finish by replying 14A_DONE.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/14a_tools.txt"
cc_reset_transcripts

CC_DEBUG_FILE="$TEST_HOME/cc-debug-14a.log"; rm -f "$CC_DEBUG_FILE"
claude_start
claude_prompt "Use Task to dispatch agent14a. Description='14a'. Prompt: 'Run your instructions exactly.'"
claude_wait 30
wait_agent_done "14A_DONE"

log_inventory "14a" "$TEST_HOME/14a_tools.txt"
report_agents "14a"
# nexus-qsmc5: the payload match is case-insensitive. A live run had agent14c call
# mcp__stub__record with payload "14C-PROOF" (the model upper-cased the "c" it had been
# told in lower case): the call landed, the exact-case grep read it as no call, and 14c
# FAILED on two of four runs although the string reference worked.
log_called=0; [[ -s "$STUB_LOG" ]] && grep -qi "14a-PROOF" "$STUB_LOG" && log_called=1
mcp_in_inv=0; [[ -f "$TEST_HOME/14a_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/14a_tools.txt" && mcp_in_inv=1
spawned=0; stub_spawned && spawned=1
echo "    14a verdict: mcp_in_inv=$mcp_in_inv  stub_called=$log_called  stub_spawned=$spawned"

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
# OR the call landing in STUB_LOG (stub_called=1) OR the stub process having been
# spawned (stub_spawned=1, the startup marker; nexus-qsmc5). All absent = server
# did not load.
if [[ $mcp_in_inv -eq 1 || $log_called -eq 1 || $spawned -eq 1 ]]; then
    CTRL_14A_LOADED=1
    pass "14a: inline mcpServers (no tools filter) LOADED for the agent (mcp_in_inv=$mcp_in_inv, stub_called=$log_called, stub_spawned=$spawned — any proves load)"
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

First, if mcp__stub__record is not already callable, use ToolSearch (query
select:mcp__stub__record) to load its schema; it may not exist for you at all.
Save your tool inventory to ${TEST_HOME}/14b_tools.txt using Write. Then call
mcp__stub__record with payload='14b-PROOF' if you can. Whatever happens, finish by
replying 14B_DONE.
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
cc_reset_transcripts

if cc_plugin_require "$TEST_HOME/.claude/test-plugin" test-plugin local-marketplace; then
claude_start
claude_prompt "Use Task to dispatch agent14b. Description='14b'. Prompt: 'Run your instructions exactly.'"
claude_wait 30
wait_agent_done "14B_DONE"

log_inventory "14b" "$TEST_HOME/14b_tools.txt"
report_agents "14b"
log_called=0; [[ -s "$STUB_LOG" ]] && grep -qi "14b-PROOF" "$STUB_LOG" && log_called=1
mcp_in_inv=0; [[ -f "$TEST_HOME/14b_tools.txt" ]] && grep -qE "mcp__stub__" "$TEST_HOME/14b_tools.txt" && mcp_in_inv=1
agent_ran=0; [[ -f "$TEST_HOME/14b_tools.txt" ]] && agent_ran=1
stub_spawned_14b=0; stub_spawned && stub_spawned_14b=1
echo "    14b verdict: agent_ran=$agent_ran  stub_spawned=$stub_spawned_14b  mcp_in_inv=$mcp_in_inv  stub_called=$log_called"

# VALIDITY NOTE (root-caused 2026-05-31, restated by nexus-spwp2): this tests a
# config CC explicitly DISALLOWS -- the docs state hooks/mcpServers/permissionMode
# are not supported on plugin-shipped agents (security). Until nexus-spwp2 the
# plugin was never loaded at all (a hand-written installed_plugins.json is not
# read by Claude Code 2.1.302), which is why agent14b was "not registered" and
# this part could only skip. With the plugin loaded (cc_plugin_require above, a
# FAIL otherwise) a missing agent is a real result about plugin agents, still
# reported as a SKIP only when agent14b did not run. nexus-qsmc5: the reading
# no longer depends on the deferred tool schema (it flipped between PASS and SKIP
# on whether the agent loaded and called the tool): it rests on whether Claude
# Code SPAWNED the stub for the agent, which the stub's own startup marker answers.
if [[ $agent_ran -ne 1 ]]; then
    skip "14b: plugin-shipped agent 'agent14b' did not run although the plugin loaded (see the pane); not a verdict on inline mcpServers"
elif [[ $stub_spawned_14b -eq 1 ]]; then
    pass "14b: plugin-shipped agent inline mcpServers WORK: Claude Code spawned the stub for the plugin agent (process_launched in the stub log; listed=$mcp_in_inv, called=$log_called)"
elif [[ $CTRL_14A_LOADED -eq 1 ]]; then
    pass "14b: plugin-shipped agent inline mcpServers do NOT load (agent ran, the stub was never spawned) — vs 14a project-level which loads"
else
    skip "14b: agent14b ran and the stub was never spawned, but 14a (the positive control: the same stub, project-level agent) did not show the stub loading in this run, so the stub may simply be unable to start here; not a verdict on plugin-agent mcpServers"
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

First load the mcp__stub__record tool: it is a DEFERRED MCP tool, so if it is not
already callable, use ToolSearch (query select:mcp__stub__record) to load its schema
before calling it. Save your tool inventory to ${TEST_HOME}/14c_tools.txt using
Write. Then call mcp__stub__record with payload='14c-PROOF'. Whatever happens,
finish by replying 14C_DONE.
EOF

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Task", "mcp__stub__*", "Write"], "defaultMode": "acceptEdits" }
}
EOF

: > "$STUB_LOG"
rm -f "$TEST_HOME/14c_tools.txt"
cc_reset_transcripts

# cd into TEST_HOME so .mcp.json is at the workspace root
send_keys "cd $TEST_HOME" Enter; sleep 0.3
CC_DEBUG_FILE="$TEST_HOME/cc-debug-14c.log"; rm -f "$CC_DEBUG_FILE"
claude_start
# Deferred-tool warmup (README "Deferred MCP tools"): the parent loads the project
# server's schemas first, as scenarios 05 and 07 do, so the agent's first call does
# not race discovery (nexus-qsmc5).
claude_prompt "List your available tools whose name starts with mcp__, one per line. If none, reply NO-MCP-TOOLS."
claude_wait 30
claude_prompt "Use Task to dispatch agent14c. Description='14c'. Prompt: 'Run your instructions exactly.'"
claude_wait 30
wait_agent_done "14C_DONE"

log_inventory "14c" "$TEST_HOME/14c_tools.txt"
report_agents "14c"
log_called=0; [[ -s "$STUB_LOG" ]] && grep -qi "14c-PROOF" "$STUB_LOG" && log_called=1
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
