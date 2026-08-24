---
allowed-tools: Bash
description: Create a new RDR — scaffold from template, assign sequential ID, register in T2
---

# New RDR

!`nx rdr preamble rdr-create`

## Title / Details

$ARGUMENTS

## Action

- Prose register: the RDR is written for the reader named in `{rdr_dir}/REGISTER.md` (fall back to `$CLAUDE_PLUGIN_ROOT/resources/rdr/REGISTER.md` if the repo copy is not there yet) (a smart future engineer who may not know the jargon; define terms on first use; simplified, never simplistic).

All data is pre-loaded above — no additional tool calls needed.

### PRIOR-ART SCAN — do this BEFORE drafting, not after

The existing-RDR table above is a **prior-art corpus**, not just a source for
the next ID. Scan it for RDRs whose titles overlap this one's problem, and read
the ones that do before writing a word of the draft.

An RDR that states a diagnosis as novel when a CLOSED RDR already reached it —
or that re-derives a design an existing DRAFT already specifies — wastes the
reader's trust and risks duplicating shipped work. This is a real failure mode,
not a hypothetical: RDR-198 was drafted claiming a novel diagnosis of non-atomic
cross-store orchestration while RDR-164, listed in its own pre-load table, had
already diagnosed it, shipped the fix for one domain, and cited two resulting
bugs by id.

For each overlapping RDR, decide and record which it is:

- **Origin** — it created the thing you now want to change. Quote its stated
  rationale and say plainly whether that rationale still holds. A rationale that
  has expired is the strongest possible evidence for the change.
- **Precedent** — it reached your diagnosis for a different scope and shipped.
  Cite it; your claim is now proven rather than asserted, and your RDR argues
  the remedy generalises.
- **Adjacent draft** — unmerged and overlapping. State an explicit scope
  boundary (a table of which concern belongs to which RDR) and say how the two
  sequence against each other.
- **Superseded** — your RDR replaces it. Say so, and follow the repo's
  supersede convention.

Put the result in a `## Relationship to Prior RDRs` section, placed after the
Problem Statement and before Context. If the scan genuinely finds nothing,
write one line saying you looked and what you searched for — a recorded dead
end is a finding, and its absence is indistinguishable from not having looked.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Use the existing RDR list to determine the next sequential ID (shown as **Next ID** above).
- Use the detected ID style (`RDR-NNN-*` or `NNN-*`) for the new filename.
- If `$ARGUMENTS` contains a title, pre-fill it; otherwise prompt.
- If the RDR directory does not exist, run bootstrap: create the directory and copy templates from `$CLAUDE_PLUGIN_ROOT/resources/rdr/`.
- Register the new RDR in T2: use **memory_put** tool: project="{repo}_rdr", title="{id}" with RDR metadata fields.
