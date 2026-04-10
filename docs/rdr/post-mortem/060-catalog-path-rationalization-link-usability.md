---
rdr: "060"
title: "Catalog Path Rationalization and Link Graph Usability"
status: closed
closed_date: 2026-04-09
reason: implemented
---

# RDR-060 Post-Mortem

## Outcome

Fully implemented. Six elements shipped (E1, E3–E7); E2 (file-level link deduplication) was removed after RF-7 disproved the underlying problem. Covers path rationalization, link-aware search boost, discovery tools, incremental linker improvements, agent integration, and catalog housekeeping. Link-aware scoring added to scoring.py with per-type weights.

## What Worked

- **Incremental approach**: E1 (relative path storage) landed first at all call sites, establishing the foundation before E2-E3 built resolution and migration on top.
- **repo_root in OwnerRecord**: Clean separation between "where is the repo" and "where is the file within it" — eliminates the absolute path portability problem entirely.
- **Link-aware scoring**: Per-type link weights in scoring.py give `implements` links higher boost than `implements-heuristic`, addressing the signal-to-noise ratio problem identified in the problem statement.
- **Agent integration commands**: `links-for-file` and `session-summary` (E6) make the link graph visible to agents without requiring them to understand tumblers or link types.

## What Didn't Work

- **Heuristic link noise**: The 87% implements-heuristic link ratio from the problem statement remains a data quality concern. E4-E6 improve generation and cleanup, but the underlying substring-matching approach still produces false positives for common terms.

## Metrics

- Test suite: 272+ catalog CLI tests, 279+ path tests passing
- Elements delivered: 6 (E1, E3–E7; E2 removed)
- Related RDRs: 5 (RDR-049, RDR-050, RDR-051, RDR-052, RDR-053 — all closed predecessors)
