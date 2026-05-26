#!/usr/bin/env bash
# Scenario 22 — RDR-130 delivery-mechanism probe: the INLINE !`cmd` form (which
# every RDR-130 command will use: !`nx <preamble> "$ARGUMENTS"`) executes and its
# stdout is injected. Prior scenarios (19/20/21) only exercised the fenced ```!
# form; the inline single-backtick form was assumed, not verified (gate finding).
# PATH-independent: uses printf so it does not depend on nx being on the test PATH.

scenario "22 inline_backtick_executes: inline !\`cmd\` runs and injects stdout"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

write_command "inline-probe" /dev/stdin <<'EOF'
---
allowed-tools: Bash
description: RDR-130 inline-form probe
---

# Inline Probe

Marker: !`printf 'INLINE-OK-%s' "$((6*7))"`

Report the marker above verbatim.
EOF

claude_start
claude_prompt "/inline-probe"
claude_wait 60

pane="$(capture -2000)"
if grep -qiE 'Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "inline form errored"
    tail -20 <<<"$pane" | sed 's/^/    | /'
elif grep -qF 'INLINE-OK-42' <<<"$pane"; then
    pass "inline !\`cmd\` executed — stdout injected (INLINE-OK-42 present)"
else
    fail "INLINE-OK-42 absent — inline form did not execute/inject"
    tail -20 <<<"$pane" | sed 's/^/    | /'
fi

claude_exit
scenario_end
