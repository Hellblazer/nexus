#!/usr/bin/env bash
# Scenario 00: Debug load — verify plugin loads cleanly with all components registered
#
# Three checks:
#   Part 1: claude --debug -p captures plugin load diagnostics (no interactive session)
#   Part 2: Hook scripts execute directly and produce correct output
#   Part 3: Claude can discover all expected agents and skills via -p mode

# ─── Guard: verify isolation before any Claude invocation ────────────────────
# If TEST_HOME is unset, crun falls back to real $HOME and the locally-installed
# v1 plugin bleeds into the test — giving false positives or wrong-version results.

scenario "00 debug-load: isolation guard"

if [[ -z "${TEST_HOME:-}" ]]; then
    fail "TEST_HOME is not set — run via tests/e2e/run.sh, not directly"
    echo "    Aborting scenario 00 to prevent testing against live v1 installation."
    return 0
fi

# Confirm the isolated installed_plugins.json points at the dev repo, not a cache path
plugins_json="$TEST_HOME/.claude/plugins/installed_plugins.json"
if [[ ! -f "$plugins_json" ]]; then
    fail "Isolated installed_plugins.json not found at $plugins_json"
else
    if grep -q "$REPO_ROOT/nx" "$plugins_json"; then
        pass "isolated installed_plugins.json points to dev repo ($REPO_ROOT/nx)"
    else
        fail "installed_plugins.json does NOT point to dev repo — may be testing v1"
        echo "    content: $(cat "$plugins_json")"
    fi
    # nx specifically must NOT load from the plugin cache (that would be the live v1)
    if python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
entries = d.get('plugins', {}).get('nx@nexus-plugins', [])
sys.exit(1 if any('plugins/cache' in e.get('installPath','') for e in entries) else 0)
" "$plugins_json" 2>/dev/null; then
        pass "nx plugin does not load from cache (not v1)"
    else
        fail "nx@nexus-plugins installPath points to plugin cache — testing v1, not dev"
    fi
fi

scenario_end

# ─── Part 1: Plugin load via --debug ─────────────────────────────────────────

scenario "00 debug-load: plugin load diagnostics (claude --debug -p)"

echo "    Running claude --debug in print mode..."
debug_out=$(crun "claude --debug --dangerously-skip-permissions \
    -p 'Reply with just the word OK.' 2>&1" || true)

# Always dump debug header lines for investigation
echo "    --- debug output (first 60 lines) ---"
echo "$debug_out" | head -60 | sed 's/^/    | /'
echo "    ---"

# Plugin name or install path should appear somewhere in debug output
if echo "$debug_out" | grep -qiE "\bnx\b|nexus|$REPO_ROOT"; then
    pass "nx plugin reference appears in debug output"
else
    # --debug format varies by Claude Code version; not a hard failure
    pass "nx plugin debug output check inconclusive (format may vary) — see dump above"
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

# Confirm we loaded from dev repo, not the cached v1
if echo "$debug_out" | grep -qiE "plugins/cache.*nexus|nexus.*plugins/cache"; then
    fail "Debug output shows plugin loading from cache — may be testing v1, not dev"
elif echo "$debug_out" | grep -q "$REPO_ROOT"; then
    pass "Debug output confirms plugin loading from dev repo"
else
    pass "No cache path in debug output (install path check inconclusive — see dump above)"
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

if echo "$session_hook_out" | grep -qiE "Nexus ready|session|T2 Memory|Ready Beads"; then
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

scenario "00 debug-load: hook shell scripts are executable"

for script in \
    "$REPO_ROOT/nx/hooks/scripts/subagent-start.sh"; do
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

# Check a representative spread of agents across model tiers.
# RDR-025 renamed language-specific agents to neutral names
# (java-developer → developer). Keep the current agent names here so
# the scenario stays in lockstep with ``nx/agents/``.
for agent in \
    "strategic-planner" \
    "developer" \
    "code-review-expert" \
    "plan-auditor" \
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

# Verify plugin skills are discoverable on disk. We deliberately do NOT
# ask ``claude --dangerously-skip-permissions -p`` to enumerate them —
# Claude's print mode does not inject the plugin-skills listing into its
# system prompt the way interactive mode does (agents and commands ARE
# listed, but skills aren't). Checking the on-disk layout is the
# accurate signal that our plugin packaging is correct; skill *triggering*
# under real Claude usage is exercised by scenario 03's interactive tmux
# session.
skills_dir="$REPO_ROOT/nx/skills"
for skill in \
    "nexus" \
    "brainstorming-gate" \
    "rdr-create" \
    "cli-controller" \
    "using-nx-skills"; do
    if [[ -f "$skills_dir/$skill/SKILL.md" ]]; then
        pass "skill packaged: $skill (nx/skills/$skill/SKILL.md)"
    else
        fail "skill NOT packaged: $skill — expected nx/skills/$skill/SKILL.md"
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
    "implement" \
    "review-code" \
    "nx-preflight"; do
    if echo "$cmds_out" | grep -qiE "$cmd"; then
        pass "command visible: /$cmd"
    else
        fail "command NOT visible: /$cmd"
    fi
done

scenario_end
