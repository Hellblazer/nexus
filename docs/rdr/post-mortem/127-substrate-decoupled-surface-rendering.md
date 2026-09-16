# RDR-127 Post-Mortem: Surface Rendering, Palinex is Downstream

**Closed** 2026-09-16 · **Accepted** 2026-05-22 · **Beads** none · **Supersedes** RDR-123, RDR-124

## What the RDR set out to do

Decide where a2ui surface rendering lives. Version 1 of the RDR proposed a
nexus-side `render_surface` MCP tool importing palinex, and the code landed
the same day. Version 2 reversed the dependency: nexus ships no rendering
code, and palinex, a separate project, owns the integration through its
own Claude Code plugin and HTTP sidecar, importing nexus as an extra. The
two earlier surface RDRs, 123 for `nx_answer` and 124 for subagents, were
superseded with their intent preserved at the palinex layer.

## Implementation status

Implemented, as a decision. There was nothing to build on the nexus side;
the v1 implementation files were removed the day the decision flipped. The
palinex plugin exists, is installed on this box, and reaches nexus through
the catalog and store APIs as the RDR describes.

## Implementation vs plan

Nothing diverged in the code, because the decision was to have none. What
drifted was the record. The file sat at `status: draft` until the
2026-08-18 audit found the v2 decision had been operative since May. The
T2 record said `abandoned` from 2026-06-05 while the file said `accepted`,
and was re-synced 2026-08-28. The RDR was closable from the day it was
accepted and stayed open four months.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Missing Day 2 operation | 1 | no close step for a decision-only RDR | Yes, close at accept |

## What to check first next time

A decision-only RDR is done the moment it is accepted. Close it in the
same commit as the accept, or it will drift across three surfaces, file,
T2 and README, and each audit will find a different status.
