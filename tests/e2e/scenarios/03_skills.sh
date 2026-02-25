#!/usr/bin/env bash
# Scenario 03: Skill invocation — nx:nexus (using-nexus) and search integration
#
# Tests that Claude correctly uses the nx:nexus skill and calls nx search
# when asked a codebase question. Validates the skill's core assumption:
# Claude will invoke it and use search results to answer.

scenario "03 skills: nx:nexus triggers and searches codebase"

# First, index the workspace so there's something to search
echo "    Indexing /workspace (this may take a minute)..."
crun "nx index repo /workspace --no-frecency 2>&1 | tail -5" || true

claude_start

# Ask a codebase question that should trigger nx:nexus skill
claude_prompt "Search the codebase to understand how frecency scoring works. How does the git commit history factor into search ranking?"

echo "    Waiting for Claude to search codebase (up to 3 min)..."
poll_for "nx search|frecency|git.*log|decay|scoring" 180 "codebase search" || true
claude_wait 120

# Verify Claude used nx search
assert_output "Claude invoked nx search" \
    "nx search|search.*frecency|Bash.*nx"

# Verify Claude got results and answered
assert_output "Claude provided answer about frecency" \
    "frecency|decay|commit|scoring|weight"

claude_exit
scenario_end

# ─── nx:sequential-thinking skill description test ──────────────────────────

scenario "03 skills: using-nexus skill guidance is correct"

# Use print mode to ask Claude about when to use the nx:nexus skill.
# Tests that the skill description itself gives correct, actionable guidance.
echo "    Asking Claude about the nx:nexus skill in print mode..."
skill_check=$(crun "claude --dangerously-skip-permissions -p \
    'Invoke the nx:nexus skill and summarize its key guidance in 3 bullet points.' \
    2>&1" || true)

if echo "$skill_check" | grep -qiE "nx search|index|codebase|semantic"; then
    pass "nx:nexus skill loaded and gives search guidance"
else
    fail "nx:nexus skill guidance missing — output: $(echo "$skill_check" | head -10)"
fi

scenario_end

# ─── T2 session memory check ─────────────────────────────────────────────────

scenario "03 skills: T2 session memory survives across sessions"

# Write something to T2 memory
crun "nx memory put 'e2e test marker: $(date -u +%Y%m%dT%H%M%SZ)' \
    --project nexus_active --title e2e-marker.md 2>&1" || true

# Verify it's retrievable
assert_cmd "T2 memory write/read roundtrip" \
    "nx memory get --project nexus_active --title e2e-marker.md 2>&1" \
    "e2e test marker"

# Start a new Claude session (simulates new session reading prior T2 state)
claude_start
claude_prompt "Run: nx memory get --project nexus_active --title e2e-marker.md"
claude_wait 30
assert_output "Claude can read T2 memory from prior session" \
    "e2e test marker|e2e.marker"
claude_exit

scenario_end
