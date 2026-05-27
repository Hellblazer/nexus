---
allowed-tools: Bash
description: Close an RDR with optional post-mortem, bead status gate, and T3 archival
---

# RDR Close

!`nx rdr preamble rdr-close -- "$ARGUMENTS"`

## RDR to Close

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Parse RDR ID and close reason from `$ARGUMENTS` (e.g. `003 --reason implemented`).
- Pre-check warning is shown above if status is not Final when reason is Implemented.
- **Implemented**: review for divergences, optionally create post-mortem, gate on open bead status.
- **Reverted / Abandoned**: offer post-mortem.
- **Superseded**: prompt for superseding RDR ID, cross-link both files.
- Post-mortem archive location: `{rdr_dir}/post-mortem/NNN-kebab-title.md`.
- Update RDR file status field and register close in T2: use **memory_put** tool: project="{repo}_rdr", title="{id}" with updated status fields.
