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
#
# nexus-qsmc5: the verdict used to grep the PANE for "Target file:" and "Working state",
# which a model that saw no preamble can write while saying it is missing. It now reads the
# expanded command out of the main session's TRANSCRIPT (cc_session_user_text): the user turn
# Claude Code sent to the model must carry the subcommand's own output (neither string is in
# the command source). The model's account is not consulted; it is told to say only DONE.

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

Do not use any tools. Reply with the single word DONE.
EOF

claude_start
claude_prompt "/continuation-probe cc-val probe"
cc_wait_user_text "# Continuation Preamble Probe" 90 || true
claude_wait 60
cc_command_render_check "25" render "# Continuation Preamble Probe" "Target file:" "Working state"
cc_keep_evidence "25"
claude_exit
scenario_end
