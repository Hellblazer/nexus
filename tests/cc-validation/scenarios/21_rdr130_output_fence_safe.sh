#!/usr/bin/env bash
# Scenario 21 — RDR-130 critical-assumption probe: command bash-injection OUTPUT
# that contains a triple-backtick is injected as plain text and NOT re-parsed
# by Claude Code. RDR-130 moves preamble logic into the nx CLI; those nx
# subcommands emit markdown (tables, code fences) whose OUTPUT will contain
# triple-backticks. The block SOURCE here has none (built via octal \140), so
# CC cannot truncate the fence; the only triple-backticks are in the OUTPUT.
# If the model sees the full fenced output, the assumption holds.
#
# nexus-qsmc5: the verdict used to grep the PANE for OUTFENCE-END-42, and the model was
# asked "whether the marker ... appears": a model that saw nothing wrote "OUTFENCE-END-42
# does not appear", the string was on the pane, and the scenario passed. The verdict now
# reads the expanded command out of the main session's TRANSCRIPT (the user turn Claude
# Code sent to the model, cc_session_user_text): the output block
#   OUTFENCE-START / ``` / inside-output-fence / ``` / OUTFENCE-END-42
# must be there, and none of its lines exists in the command source in that shape (the
# source has a literal backslash-n, no real newline, and no "42"). What the model says
# about it is not consulted, and it is told to say nothing but DONE.
#
# 21b is the negative control: the same command with an injection that prints nothing.
# The body reaches the model, the output block does not, and the SAME check must FAIL
# (reported as a pass of the control). A check that passed there could not tell a rendered
# injection from an absent one.

scenario "21 rdr130_output_fence_safe: injected OUTPUT triple-backticks are not re-parsed"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

# Source has NO literal triple-backtick (octal \140 builds it at runtime).
write_command "outfence-probe" /dev/stdin <<'EOF'
---
allowed-tools: Bash
description: RDR-130 CA probe — output triple-backtick safety
---

# Output Fence Probe

```!
printf 'OUTFENCE-START\n'
printf '\140\140\140\n'
printf 'inside-output-fence\n'
printf '\140\140\140\n'
printf 'OUTFENCE-END-%s\n' "$((6*7))"
```

Do not use any tools. Reply with the single word DONE.
EOF

# 21b's command: the same, but the block prints nothing.
write_command "outfence-noop" /dev/stdin <<'EOF'
---
allowed-tools: Bash
description: RDR-130 CA probe — negative control, an injection that prints nothing
---

# Output Fence Probe (no-op control)

```!
true
```

Do not use any tools. Reply with the single word DONE.
EOF

# The output block, as real lines. Only an executed block produces it.
OUTFENCE_BLOCK=$'OUTFENCE-START\n\x60\x60\x60\ninside-output-fence\n\x60\x60\x60\nOUTFENCE-END-42'

claude_start
claude_prompt "/outfence-probe"
cc_wait_user_text "# Output Fence Probe" 90 || true
claude_wait 30
cc_command_render_check "21" render "# Output Fence Probe" "$OUTFENCE_BLOCK"
cc_keep_evidence "21"
claude_exit

scenario "21b rdr130_output_fence_noop_control: an injection that prints nothing must FAIL the 21 check"
cc_reset_transcripts
claude_start
claude_prompt "/outfence-noop"
cc_wait_user_text "# Output Fence Probe (no-op control)" 90 || true
claude_wait 30
cc_command_render_check "21b" empty "# Output Fence Probe (no-op control)" "$OUTFENCE_BLOCK"
cc_keep_evidence "21b"
claude_exit
scenario_end
