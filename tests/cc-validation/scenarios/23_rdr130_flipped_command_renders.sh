#!/usr/bin/env bash
# Scenario 23 — RDR-130 P1.7: a FLIPPED RDR command renders via real Claude Code.
# rdr-list.md now injects via `!`nx rdr preamble rdr-list -- "$ARGUMENTS"`` (P1.4);
# the nx subcommand (P1.2) reads T2 with a file fallback and prints the RDR table.
# This proves the full chain — inline injection -> nx subgroup -> markdown output —
# works end to end (requires the repo nx with the preamble subgroup on PATH).

scenario "23 rdr130_flipped_command_renders: /rdr-list -> nx rdr preamble renders"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

write_command "rdr-list" "$REPO_ROOT/conexus/commands/rdr-list.md"

claude_start
claude_prompt "/rdr-list"
claude_wait 90

pane="$(capture -3000)"
if grep -qiE 'No such command|Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "flipped command errored (subgroup missing or injection broke)"
    tail -25 <<<"$pane" | sed 's/^/    | /'
elif grep -qE 'Single-Writer Enforcement|Idempotent Upgrade|RDRs \(' <<<"$pane"; then
    pass "flipped /rdr-list rendered the RDR table via nx rdr preamble (end-to-end)"
else
    fail "no RDR-table render evidence in pane"
    tail -25 <<<"$pane" | sed 's/^/    | /'
fi

claude_exit
scenario_end
