#!/usr/bin/env bash
# Scenario 21 — RDR-130 critical-assumption probe: command bash-injection OUTPUT
# that contains a triple-backtick is injected as plain text and NOT re-parsed
# by Claude Code. RDR-130 moves preamble logic into the nx CLI; those nx
# subcommands emit markdown (tables, code fences) whose OUTPUT will contain
# triple-backticks. The block SOURCE here has none (built via octal \140), so
# CC cannot truncate the fence; the only triple-backticks are in the OUTPUT.
# If the model sees the full fenced output, the assumption holds.

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

Report verbatim whether the marker OUTFENCE-END-42 and the fenced block appear above.
EOF

claude_start
claude_prompt "/outfence-probe"
claude_wait 60

pane="$(capture -2000)"
if grep -qiE 'Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "block errored — output-fence source not safe"
    tail -25 <<<"$pane" | sed 's/^/    | /'
elif grep -qF 'OUTFENCE-END-42' <<<"$pane"; then
    pass "output containing triple-backticks rendered intact (RDR-130 CA holds)"
else
    fail "no OUTFENCE-END-42 in pane — output may have been mangled"
    tail -25 <<<"$pane" | sed 's/^/    | /'
fi

claude_exit
scenario_end
