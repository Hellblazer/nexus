#!/usr/bin/env bash
# Scenario 00: Debug load — verify plugin loads cleanly with all components registered
#
# Three checks:
#   Part 1: claude --debug -p captures plugin load diagnostics (no interactive session)
#   Part 2: Hook scripts execute directly and produce correct output
#   Part 3: Claude can discover all expected agents and skills via -p mode

# ─── Part 1: Plugin load via --debug ─────────────────────────────────────────

scenario "00 debug-load: plugin load diagnostics (claude --debug -p)"

echo "    Running claude --debug in print mode..."
debug_out=$(crun "claude --debug --dangerously-skip-permissions \
    -p 'Reply with just the word OK.' 2>&1" || true)

# Always dump debug header lines for investigation
echo "    --- debug output (first 60 lines) ---"
echo "$debug_out" | head -60 | sed 's/^/    | /'
echo "    ---"

# Plugin must appear by name
if echo "$debug_out" | grep -qiE "\bnx\b|nexus.plugins|nexus-plugins"; then
    pass "nx plugin name appears in output"
else
    fail "nx plugin name not found in output"
fi

# No load-time errors
if echo "$debug_out" | grep -qiE \
    "error.*plugin|plugin.*error|failed to load|load.*failed|invalid.*manifest|manifest.*invalid|could not load"; then
    fail "Plugin load errors detected:"
    echo "$debug_out" | grep -iE "error|fail|invalid|could not" | head -20 | sed 's/^/    | /'
else
    pass "No plugin load errors"
fi

# Model got a response — Claude ran to completion
if echo "$debug_out" | grep -qiE "\bOK\b|okay"; then
    pass "Claude responded successfully with plugin loaded"
else
    fail "Claude did not respond — plugin may have prevented startup"
fi

scenario_end

# ─── Part 2: Hook scripts execute correctly ───────────────────────────────────

scenario "00 debug-load: SessionStart hook produces expected output"

# Run session_start_hook.py directly with CLAUDE_PROJECT_DIR pointing at repo
session_hook_out=$(HOME="$TEST_HOME" \
    PATH="$TEST_HOME/.local/bin:$PATH" \
    CLAUDE_PROJECT_DIR="$REPO_ROOT" \
    python3 "$REPO_ROOT/nx/hooks/scripts/session_start_hook.py" 2>&1 || true)

echo "    --- session_start_hook.py output ---"
echo "$session_hook_out" | head -20 | sed 's/^/    | /'
echo "    ---"

if echo "$session_hook_out" | grep -qiE "Nexus ready|session|T2 Memory|Ready Beads|Project Management"; then
    pass "SessionStart hook produced context output"
else
    fail "SessionStart hook produced no recognisable output"
fi

scenario_end

scenario "00 debug-load: SubagentStart hook injects RELAY_TEMPLATE"

# Run subagent-start.sh directly with CLAUDE_PLUGIN_ROOT set
subagent_hook_out=$(CLAUDE_PLUGIN_ROOT="$REPO_ROOT/nx" \
    HOME="$TEST_HOME" \
    PATH="$TEST_HOME/.local/bin:$PATH" \
    CLAUDE_PROJECT_DIR="$REPO_ROOT" \
    bash "$REPO_ROOT/nx/hooks/scripts/subagent-start.sh" 2>&1 || true)

echo "    --- subagent-start.sh output ---"
echo "$subagent_hook_out" | head -30 | sed 's/^/    | /'
echo "    ---"

# Must inject the relay template header
if echo "$subagent_hook_out" | grep -qiE "Relay Format|injected by hook"; then
    pass "SubagentStart hook injected relay template header"
else
    fail "SubagentStart hook did not inject relay template header"
fi

# Must contain the required fields section
if echo "$subagent_hook_out" | grep -qiE "Required Fields|Task.*field|Input Artifacts"; then
    pass "SubagentStart hook injected required fields content"
else
    fail "SubagentStart hook relay content missing required fields"
fi

# Must NOT include the Optional Fields section (awk stops before it)
if echo "$subagent_hook_out" | grep -qiE "^## Optional Fields"; then
    fail "SubagentStart hook included Optional Fields (awk stop condition broken)"
else
    pass "SubagentStart hook correctly stops before Optional Fields"
fi

scenario_end

scenario "00 debug-load: mcp_health_hook and setup scripts are executable"

for script in \
    "$REPO_ROOT/nx/hooks/scripts/mcp_health_hook.sh" \
    "$REPO_ROOT/nx/hooks/scripts/setup.sh" \
    "$REPO_ROOT/nx/hooks/scripts/subagent-start.sh" \
    "$REPO_ROOT/nx/hooks/scripts/permission-request-stdin.sh"; do
    name=$(basename "$script")
    if [[ -x "$script" ]]; then
        pass "$name is executable"
    else
        fail "$name is NOT executable (chmod +x needed)"
    fi
done

scenario_end

# ─── Part 3: Component discovery via -p mode ─────────────────────────────────

scenario "00 debug-load: agents visible to Claude"

echo "    Asking Claude to list nx plugin agents (print mode)..."
agents_out=$(crun "claude --dangerously-skip-permissions \
    -p 'List ALL agent names provided by the nx plugin. One per line, names only.' \
    2>&1" || true)

echo "    --- agents output (first 30 lines) ---"
echo "$agents_out" | head -30 | sed 's/^/    | /'
echo "    ---"

# Check a representative spread of agents across model tiers
for agent in \
    "strategic-planner" \
    "java-developer" \
    "code-review-expert" \
    "plan-auditor" \
    "orchestrator" \
    "deep-analyst" \
    "knowledge-tidier"; do
    if echo "$agents_out" | grep -qiE "$agent"; then
        pass "agent visible: $agent"
    else
        fail "agent NOT visible: $agent"
    fi
done

scenario_end

scenario "00 debug-load: skills visible to Claude"

echo "    Asking Claude to list nx plugin skills (print mode)..."
skills_out=$(crun "claude --dangerously-skip-permissions \
    -p 'List ALL skill names provided by the nx plugin. One per line, names only.' \
    2>&1" || true)

echo "    --- skills output (first 30 lines) ---"
echo "$skills_out" | head -30 | sed 's/^/    | /'
echo "    ---"

for skill in \
    "sequential-thinking" \
    "nexus" \
    "brainstorming-gate" \
    "rdr-create" \
    "cli-controller" \
    "using-nx-skills"; do
    if echo "$skills_out" | grep -qiE "${skill//-/.}"; then
        pass "skill visible: $skill"
    else
        fail "skill NOT visible: $skill"
    fi
done

scenario_end

scenario "00 debug-load: slash commands visible to Claude"

echo "    Asking Claude to list nx plugin slash commands (print mode)..."
cmds_out=$(crun "claude --dangerously-skip-permissions \
    -p 'List ALL slash commands provided by the nx plugin. One per line.' \
    2>&1" || true)

echo "    --- commands output (first 30 lines) ---"
echo "$cmds_out" | head -30 | sed 's/^/    | /'
echo "    ---"

for cmd in \
    "create-plan" \
    "java-implement" \
    "review-code" \
    "rdr-create" \
    "nx-preflight"; do
    if echo "$cmds_out" | grep -qiE "$cmd"; then
        pass "command visible: /$cmd"
    else
        fail "command NOT visible: /$cmd"
    fi
done

scenario_end
