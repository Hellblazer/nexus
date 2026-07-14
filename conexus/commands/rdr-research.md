---
allowed-tools: Bash
description: Add, track, or verify structured research findings for an active RDR
---

# RDR Research

!`nx rdr preamble rdr-research`

## Subcommand and Arguments

$ARGUMENTS

**Targeted load**: parse the subcommand and **numeric ID** from `$ARGUMENTS`
and run, via the Bash tool, `nx rdr preamble rdr-research -- <subcommand> <ID>`
with literal argv tokens. Free-text research content (which may contain
apostrophes, quotes, parens) must be passed as real argv tokens by the Bash
tool, never spliced into a shell-quoted line (nexus-ybvyo — the apostrophe
crash class).

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Subcommands: `add <id>`, `status <id>`, `verify <id> <seq>`.
- Parse subcommand and RDR ID from `$ARGUMENTS`.
- Existing T2 findings and file Research Findings section are pre-loaded above.
- Dispatch `codebase-deep-analyzer` or `deep-research-synthesizer` if investigation (not just recording) is requested.
