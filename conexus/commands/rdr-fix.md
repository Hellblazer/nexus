---
allowed-tools: Bash
description: Fix an RDR's gate findings — prints the findings with their sites, the diff since the gated commit, the pre-edit research title, and the fix rules
---

# RDR Fix

!`nx rdr preamble rdr-fix`

## RDR to Fix

$ARGUMENTS

**Targeted load**: parse the **numeric ID** from `$ARGUMENTS` and run, via the
Bash tool, `nx rdr preamble rdr-fix -- <ID>` (literal argv token). Never splice
raw `$ARGUMENTS` into a shell-quoted line.

## Action

All data is pre-loaded above — no additional tool calls needed beyond the reads the rules name.

- Prose register (`{rdr_dir}/REGISTER.md`, fall back to `$CLAUDE_PLUGIN_ROOT/resources/rdr/REGISTER.md`).
- If the preamble says there is no gate record, stop: a finding from a review goes through `/conexus:rdr-research add <id>`.
- If the preamble says the RDR is past the gate, stop: post-accept edits are not gate fixes.
- For every finding listed: read its source, quote it with a tool, and record the research entry FIRST with `nx rdr preamble rdr-research -- add <id> <finding tokens>` (the preamble names the title it will get). A count or a universal in the finding or in the fix needs a census, captured in that entry as an enumeration.
- Edit the RDR at every site in the finding's `Sites:` list (grep the refuted and the corrected phrasing when a finding has none). Change the named fact and nothing else; a gloss, rationale or parenthetical is a separate commit.
- Commit by explicit path. Then run `nx rdr preamble rdr-gate -- <id>`: it prints the Fix check section with the diff range and the T2 title `{id}-fix-check-<sha>`; dispatch the fix check and store its verdict under that title before any re-gate.
- Do not run the gate; the user drives lifecycle transitions.
