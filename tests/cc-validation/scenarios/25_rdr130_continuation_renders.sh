#!/usr/bin/env bash
# Scenario 25 — RDR-130 P2.4/P2.6: the migrated continuation injection form renders
# its DIVERGENT file-writer preamble via real Claude Code. P2.5 flipped
# continuation.md to `!`nx command-context continuation -- "$ARGUMENTS"``; the
# subcommand (P2.4) computes the dated /tmp handoff path and prints the mechanical
# session context (**Target file:**, ## Working state, ### Uncommitted, ...). Its
# output shape differs from the read-and-print commands (a target path plus
# working-state), so per the RDR P2 Test Plan it gets its own scenario. Uses the
# minimal-probe mechanism (scenarios 21/22) so the unchanged ## Action body (which
# drives the agent to author a handoff file) does not obscure the preamble render.

scenario "25 rdr130_continuation_renders: inline !\`nx command-context continuation\` emits path + working state"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

write_command "continuation-probe" /dev/stdin <<'EOF'
---
allowed-tools: Bash
description: RDR-130 P2 continuation preamble probe
---

# Continuation Preamble Probe

!`nx command-context continuation -- "$ARGUMENTS"`

Report verbatim whether the "**Target file:**" line and the "## Working state" heading appear above.
EOF

claude_start
claude_prompt "/continuation-probe cc-val probe"
claude_wait 90

pane="$(capture -3000)"
if grep -qiE 'No such command|Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "continuation injection errored (command-context group missing or injection broke)"
    tail -25 <<<"$pane" | sed 's/^/    | /'
elif grep -qF 'Target file:' <<<"$pane" && grep -qF 'Working state' <<<"$pane"; then
    pass "inline !\`nx command-context continuation\` injected its file-writer preamble (path + working state)"
else
    fail "no continuation preamble render evidence in pane"
    tail -25 <<<"$pane" | sed 's/^/    | /'
fi

claude_exit
scenario_end
