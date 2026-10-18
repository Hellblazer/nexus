#!/usr/bin/env bash
# Scenario 24 — RDR-130 P2.5/P2.6: the migrated AGENT-RELAY injection form renders
# its preamble via real Claude Code. P2.5 flipped the 16 agent-relay commands to a
# single-line `!`nx command-context <name>`` call; the nx subcommand
# (P2.2) prints the shared preamble (## Context, project-type table, top-level
# structure, source locations). This isolates exactly what P2 changed — the
# injection line — using the proven minimal-probe mechanism (scenarios 21/22), not
# the unchanged ## Action agent body (which invokes a skill and goes interactive).
# Uses the real `nx command-context analyze-code` subcommand, so it also proves the
# command-context group is reachable on the test PATH (the repo nx).
#
# nexus-qsmc5: the verdict used to grep the PANE for "Project type:" and "Source Locations",
# which a model that saw no preamble can write while saying it is missing. It now reads the
# expanded command out of the main session's TRANSCRIPT (cc_session_user_text): the user turn
# Claude Code sent to the model must carry the subcommand's own output (neither string is in
# the command source). The model's account is not consulted; it is told to say only DONE.

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

!`nx command-context analyze-code`

Do not use any tools. Reply with the single word DONE.
EOF

claude_start
claude_prompt "/agentrelay-probe"
cc_wait_user_text "# Agent-relay Preamble Probe" 90 || true
claude_wait 60
cc_command_render_check "24" render "# Agent-relay Preamble Probe" "Project type:" "Source Locations"
cc_keep_evidence "24"
claude_exit
scenario_end
