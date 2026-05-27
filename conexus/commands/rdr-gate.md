---
allowed-tools: Bash
description: Run finalization gate on an RDR — structural, assumption audit, and AI critique
---

# RDR Gate

!`nx rdr preamble rdr-gate -- "$ARGUMENTS"`

## RDR to Gate

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Run all three gate layers in sequence:
  - **Layer 1 — Structural**: Use the Section Structure and Section Summaries above to check completeness (required headings present, no empty sections). **If no research findings exist** and `--skip-research` was NOT passed, report **BLOCKED** and stop — do not proceed to Layer 2 or 3. If `--skip-research` was passed, note the override and continue.
  - **Layer 2 — Assumption audit**: Use T2 Research Findings above to verify assumptions are evidenced. Every finding classified as "Assumed" must have an explicit risk assessment.
  - **Layer 3 — AI critique**: Dispatch the `substantive-critic` agent via Agent tool with the full RDR content. If the RDR has `related_issues` listing other RDR IDs, read those RDRs and include their content in the critique prompt — the critic should check for consistency and contradictions between related RDRs (P7).
- Gate outcomes: **BLOCKED** (critical issues found, must fix and re-gate) or **PASSED** (no critical issues). Do not use "Conditional Accept" or other ad-hoc outcomes.
- **Write T2 gate result** after completing all layers. Use the repo name from above:
  Use **memory_put** tool: project="{repo_name}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate", content with:
  ```
  outcome: "PASSED"  # or "BLOCKED"
  date: "YYYY-MM-DD"
  critical_count: 0
  significant_count: 2
  observation_count: 3
  summary: "One-sentence summary of gate result"
  ```
  This overwrites any previous gate result for this RDR, so only the latest gate run is stored.
- **If PASSED**, print: `> Run '/conexus:rdr-accept <id>' to accept this RDR.`
- If no ID given, show the available RDR table above and prompt for an ID.
