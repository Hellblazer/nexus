---
allowed-tools: Bash
description: Close an RDR with optional post-mortem, bead status gate, and T3 archival
---

# RDR Close

!`nx rdr preamble rdr-close`

## RDR to Close

$ARGUMENTS

## Action

- The preamble above lists the open/draft RDRs and usage (no id was passed to it).
- Parse the RDR ID and close reason from `$ARGUMENTS` (e.g. `003 --reason implemented`).
- Load the targeted close context: run, via the Bash tool, `nx rdr preamble rdr-close -- <ID>` with the parsed **numeric ID** as a literal argument (e.g. `nx rdr preamble rdr-close -- 003`). The Bash tool passes the id as a real argv token, so free-form text in `$ARGUMENTS` (a reason containing quotes, parens, or apostrophes) never reaches a shell-quoted line. Do NOT pass the raw close reason on this command line — you already have it from `$ARGUMENTS`.
- RDR directory is shown in that targeted output (from `.nexus.yml` `indexing.rdr_paths[0]`).
- The pre-check warning (status not Final when reason is Implemented) appears in the targeted output.
- **Implemented**: review for divergences, optionally create post-mortem, gate on open bead status.
- **Reverted / Abandoned**: offer post-mortem.
- **Superseded**: prompt for superseding RDR ID, cross-link both files.
- Post-mortem archive location: `{rdr_dir}/post-mortem/NNN-kebab-title.md`.
- Update RDR file status field and register close in T2: use **memory_put** tool: project="{repo}_rdr", title="{id}" with updated status fields.
