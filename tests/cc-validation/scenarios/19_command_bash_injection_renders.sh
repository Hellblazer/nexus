#!/usr/bin/env bash
# Scenario 19 — nexus-ln9y5: command bash-injection RENDER PATH.
#
# This is the layer that no pytest can reach and that let conexus 5.1.1 ship a
# non-working "fix": Claude Code only executes command bash injection in the
# documented ```! fenced form (or inline !`cmd`). The legacy !{ } brace form —
# used by every conexus command through 5.1.1 — emits as raw source and never
# runs. This scenario drives a real Claude Code and proves:
#
#   Part A (positive): the converted rdr-list command's ```! block EXECUTES —
#                      the RDR table renders in the pane.
#   Part B (negative control): a !{ } brace-form probe does NOT execute — its
#                      sentinel arithmetic is emitted verbatim, never evaluated.
#
# Part B is what makes Part A meaningful: it shows the test discriminates the
# working syntax from the broken one. If a future CC starts honoring !{ },
# Part B will flip and tell us the landscape changed.

scenario "19 command_bash_injection_renders: fenced-bang executes, brace does not"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

# Part A: the real converted command (inlines its Python into a ```! block).
write_command "rdr-list" "$REPO_ROOT/conexus/commands/rdr-list.md"

# Part B: a minimal brace-form probe. If !{ } executed, the pane would show
# "BRACE-EXECUTED-2"; if emitted raw (expected), it shows the literal
# "BRACE-EXECUTED-$((1+1))".
write_command "brace-probe" /dev/stdin <<'EOF'
---
allowed-tools: Bash
description: nexus-ln9y5 negative control — !{ } must NOT execute
---

# Brace Probe

!{
echo "BRACE-EXECUTED-$((1+1))"
}

Report whether the marker above is a literal or an evaluated number.
EOF

claude_start

# ── Part A: the fenced-bang block must EXECUTE and render the RDR table ──────
# The preamble scans docs/rdr and injects the RDR table; the model then
# reformats it (so the literal "### RDRs (" header is reworded — do not grep
# for it). Distinctive verbatim RDR titles prove the preamble executed and its
# data flowed: the model cannot produce these without the injected output.
# Raw-failure markers (unexpanded heredoc / $CLAUDE_PLUGIN_ROOT / auth error /
# empty-scan) must be absent.
claude_prompt "/rdr-list"
claude_wait 90
paneA="$(capture -3000)"
# VALIDITY NOTE (reworked 2026-05-31): the prior check grepped for two hardcoded
# RDR titles ("Single-Writer Enforcement", "Storage Substrate Split"). The model
# reformats and SELECTS which RDRs to surface, so it legitimately rendered a
# different subset (RDR-070/137/106/134) and the test false-failed even though
# the fenced-bang block executed correctly. Replace with a structural check that
# does not depend on which RDRs are live: multiple distinct RDR-NNN ids plus a
# status word prove the `nx rdr list` preamble executed and its table data flowed
# (the model cannot fabricate several real RDR ids + statuses without it), and
# the failure markers must be absent.
# Robust floor: the model SUMMARIZES nx rdr list output and surfaces a variable
# number of RDRs (observed 1..many across runs), so do not assert a count. The
# deterministic signals are: (1) no failure markers (a broken fenced-bang shows
# raw heredoc / CLAUDE_PLUGIN_ROOT / errors / "No RDRs found"), and (2) at least
# one real RDR-NNN id reached the model (it cannot produce one in a /rdr-list
# context without the injected table). Together these prove the block executed
# and its data flowed, without depending on which/how-many RDRs the model echoes.
# `|| true` so a no-match grep (exit 1) does not fail the pipeline under the
# runner's `set -euo pipefail` and abort the whole suite. wc still emits 0.
rdr_ids="$( { grep -oE 'RDR-[0-9]+' <<<"$paneA" || true; } | sort -u | wc -l | tr -d ' ')"
if ! grep -qE 'python3 <<|CLAUDE_PLUGIN_ROOT|API Error|No RDRs found' <<<"$paneA" \
   && [[ "$rdr_ids" -ge 1 ]]; then
    pass "A: fenced-bang block executed — RDR data flowed ($rdr_ids RDR id(s) present), no failure markers"
else
    fail "A: fenced-bang block did NOT render the RDR data (RDR ids=$rdr_ids, failure markers present?)"
    tail -30 <<<"$paneA" | sed 's/^/    | /'
fi

# ── Part B: the brace form must NOT execute (negative control) ───────────────
# If !{ } had been preprocessed, the body would show BRACE-EXECUTED-2 and the
# literal $((1+1)) would be GONE. Its survival proves the brace form did not
# execute. (Do NOT grep for BRACE-EXECUTED-2 — the model quotes it in prose
# while explaining it was NOT produced.)
claude_prompt "/brace-probe"
claude_wait 30
if capture -400 | grep -qF '$((1+1))'; then
    pass "B: brace form did not execute — literal \$((1+1)) survived (negative control holds)"
else
    fail "B: literal \$((1+1)) absent — brace form may have executed (revisit nexus-ln9y5)"
fi

claude_exit
scenario_end
