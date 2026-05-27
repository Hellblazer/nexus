#!/usr/bin/env bash
# Scenario 24 — RDR-130 P2.5/P2.6: the migrated AGENT-RELAY injection form renders
# its preamble via real Claude Code. P2.5 flipped the 16 agent-relay commands to a
# single-line `!`nx command-context <name> -- "$ARGUMENTS"`` call; the nx subcommand
# (P2.2) prints the shared preamble (## Context, project-type table, top-level
# structure, source locations). This isolates exactly what P2 changed — the
# injection line — using the proven minimal-probe mechanism (scenarios 21/22), not
# the unchanged ## Action agent body (which invokes a skill and goes interactive).
# Uses the real `nx command-context analyze-code` subcommand, so it also proves the
# command-context group is reachable on the test PATH (the repo nx).

scenario "24 rdr130_agent_relay_renders: inline !\`nx command-context analyze-code\` renders preamble"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

write_command "agentrelay-probe" /dev/stdin <<'EOF'
---
allowed-tools: Bash
description: RDR-130 P2 agent-relay preamble probe
---

# Agent-relay Preamble Probe

!`nx command-context analyze-code -- "$ARGUMENTS"`

Report verbatim whether the "### Source Locations" heading and the "**Project type:**" list appear above.
EOF

claude_start
claude_prompt "/agentrelay-probe"
claude_wait 90

pane="$(capture -3000)"
if grep -qiE 'No such command|Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "agent-relay injection errored (command-context group missing or injection broke)"
    tail -25 <<<"$pane" | sed 's/^/    | /'
elif grep -qF 'Project type:' <<<"$pane" && grep -qF 'Source Locations' <<<"$pane"; then
    pass "inline !\`nx command-context analyze-code\` injected its preamble (end-to-end)"
else
    fail "no command-context preamble render evidence in pane"
    tail -25 <<<"$pane" | sed 's/^/    | /'
fi

claude_exit
scenario_end
