#!/usr/bin/env bash
# Scenario 02: Sequential thinking + compaction resilience
#
# Tests that nx:sequential-thinking thought chains survive /compact.
# The key property: nx thought add persists to T2 SQLite on every call,
# so compaction (which only clears the context window) cannot lose the chain.

scenario "02 sequential-thinking: build a thought chain"

claude_start

# Ask Claude to work through a problem using nx:sequential-thinking.
# We want it to actually invoke the skill and call nx thought add.
claude_prompt "Use nx:sequential-thinking to think through this problem: how should the nx CLI handle rate limiting from Voyage AI during bulk indexing? Work through at least 4 thoughts."

echo "    Waiting for thought chain to build (up to 3 min)..."
if poll_for "Thought [0-9]|thought.*[0-9]|nx thought add|✓.*thought" 180 "thought chain building"; then
    pass "Claude invoked sequential-thinking and built thoughts"
else
    fail "No evidence of thought chain being built"
fi

claude_wait 60

# Verify T2 has the chain
echo "    Verifying T2 has the thought chain..."
assert_cmd "T2 has active thought chain" \
    "nx thought list 2>&1" \
    "chain|session|thought"

# Capture the chain ID / content so we can verify it survives compaction
chain_before=$(crun "nx thought show 2>&1" || true)
echo "    Chain before compaction: $(echo "$chain_before" | head -3)"

scenario_end

# ─── Trigger compaction ──────────────────────────────────────────────────────

scenario "02 sequential-thinking: /compact and verify chain persists"

echo "    Sending /compact to Claude..."
send_keys "/compact" Enter

# Wait for compaction to complete (Claude returns to prompt)
echo "    Waiting for compaction to complete..."
sleep 5
poll_for "❯|compacted|summarized|context" 60 "compaction complete" || true
sleep 3

# Verify T2 chain still exists after compaction
assert_cmd "T2 chain persists after /compact" \
    "nx thought show 2>&1" \
    "Thought [0-9]|thought.*[0-9]|totalThoughts"

# Ask Claude to continue — it should be able to resume the chain
echo "    Asking Claude to add more thoughts after compaction..."
claude_prompt "Continue the sequential thinking — add 2 more thoughts on concrete implementation strategies for the rate limiting approach."

claude_wait 90

assert_output "Claude continued thinking after compaction" \
    "Thought [0-9]|thought|rate.limit|implement"

# Verify T2 chain grew (more thoughts than before)
chain_after=$(crun "nx thought show 2>&1" || true)

before_count=$(echo "$chain_before" | grep -cE "Thought [0-9]" || echo 0)
after_count=$(echo "$chain_after" | grep -cE "Thought [0-9]" || echo 0)

if [[ "$after_count" -gt "$before_count" ]]; then
    pass "Thought chain grew after compaction ($before_count → $after_count thoughts)"
else
    fail "Thought count did not grow after compaction (before=$before_count, after=$after_count)"
fi

claude_exit
scenario_end
