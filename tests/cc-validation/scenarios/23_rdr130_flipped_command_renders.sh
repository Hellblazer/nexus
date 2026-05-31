#!/usr/bin/env bash
# Scenario 23 — RDR-130 P1.7: a FLIPPED RDR command renders via real Claude Code.
# rdr-list.md now injects via `!`nx rdr preamble rdr-list`` (P1.4);
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
# Poll for the render rather than a fixed wait: the slash-command preamble + the
# model's table render can land AFTER claude_wait returns, leaving an empty
# capture (a flaky 0-id fail — 19A and 23 run the same command, and 19A caught
# the render while 23 missed it in the same suite run). Wait for an RDR id to
# appear (up to 90s), then settle and capture. A genuine no-render still times
# out and fails correctly.
poll_for "RDR-[0-9]" 90 "rdr-list render" || true
claude_wait 20
pane="$(capture -3000)"
# Robust check (reworked 2026-05-31): the prior version grepped for two hardcoded
# RDR titles, which the model does not reliably echo (it summarizes the table and
# picks different RDRs each run — false-failed when neither title appeared). Assert
# the deterministic signals instead: command did not error, and at least one real
# RDR-NNN id reached the model (proving the nx rdr preamble executed and its data
# flowed). See scenario 19A for the same rationale.
# `|| true` so a no-match grep (exit 1) does not fail the pipeline under the
# runner's `set -euo pipefail` and abort the whole suite (it did, on a run whose
# pane had no RDR ids). wc still emits 0.
rdr_ids="$( { grep -oE 'RDR-[0-9]+' <<<"$pane" || true; } | sort -u | wc -l | tr -d ' ')"
if grep -qiE 'No such command|Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "flipped command errored (subgroup missing or injection broke)"
    tail -25 <<<"$pane" | sed 's/^/    | /'
elif [[ "$rdr_ids" -ge 1 ]]; then
    pass "flipped /rdr-list rendered the RDR table via nx rdr preamble ($rdr_ids RDR id(s), end-to-end)"
else
    fail "no RDR-table render evidence in pane (RDR ids=$rdr_ids)"
    tail -25 <<<"$pane" | sed 's/^/    | /'
fi

claude_exit
scenario_end
