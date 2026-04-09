---
rdr: "060"
title: "Catalog Path Rationalization and Link Graph Usability"
status: closed
closed_date: 2026-04-09
reason: implemented
---

# RDR-060 Post-Mortem

## Outcome

Fully implemented. All eight elements (E1-E8) shipped, covering path rationalization, resolve_path() helper, T3 batch migration, incremental link generation, catalog housekeeping, garbage collection, and agent integration commands (links-for-file, session-summary). Link-aware search boost added to scoring.py with per-type weights.

## What Worked

- **Incremental approach**: E1 (relative path storage) landed first at all call sites, establishing the foundation before E2-E3 built resolution and migration on top.
- **repo_root in OwnerRecord**: Clean separation between "where is the repo" and "where is the file within it" — eliminates the absolute path portability problem entirely.
- **Link-aware scoring**: Per-type link weights in scoring.py give `implements` links higher boost than `implements-heuristic`, addressing the signal-to-noise ratio problem identified in the problem statement.
- **Agent integration commands**: `links-for-file` and `session-summary` (E7-E8) make the link graph visible to agents without requiring them to understand tumblers or link types.

## What Didn't Work

- **Heuristic link noise**: The 87% implements-heuristic link ratio from the problem statement remains a data quality concern. E4-E6 improve generation and cleanup, but the underlying substring-matching approach still produces false positives for common terms.

## Metrics

- Test suite: 272+ catalog CLI tests, 279+ path tests passing
- Elements delivered: 8 (E1-E8)
- Related RDRs: 5 (RDR-049, RDR-050, RDR-051, RDR-052, RDR-053 — all closed predecessors)
