# Post-Mortem: RDR-029 Pipeline Versioning

**Closed**: 2026-03-09  **Reason**: implemented  **PR**: #77

## Plan vs Actual

| Phase | Plan | Actual | Divergence |
|-------|------|--------|------------|
| 1 | PIPELINE_VERSION + helpers in indexer.py | As planned | None |
| 2 | Wire stamp into _run_index + standalone CLI (pdf/md/rdr) | Stamping in _run_index only; standalone CLI not wired | Minor — `nx index repo --force` is the primary upgrade path |
| 3 | Staleness check + --force-stale flag | As planned, plus force_stale escalation logic | None |
| 4 | nx doctor version check via registered repos | Iterates all 4 store databases directly | Better coverage — catches unregistered collections |
| 5 | Separate test phase | Tests integrated per-phase (TDD) | Process improvement |

## Key Decisions During Implementation

1. **RDR collection stamp gated on `rdr_indexed > 0`**: Only stamp if RDR docs were actually indexed, avoiding unnecessary collection creation.
2. **Doctor uses CloudClient per store**: Creates 4 additional CloudClient connections (one per store database). Acceptable for a diagnostic command.
3. **force_stale staleness check duplicated**: Both `check_pipeline_staleness()` (warning) and explicit version comparison (for force escalation) run. The warning fires before escalation, which is the correct UX — user sees what triggered the force.

## LOC

- Plan estimate: ~175 impl + ~120 tests = ~295 total
- Actual: ~125 impl + ~60 tests = ~185 total (leaner than estimated)

## Gate Value

The gate caught two critical bugs before implementation:
1. **Metadata clobber**: `col.modify(metadata=...)` replaces ALL metadata. Without merge semantics, every stamp would destroy existing collection metadata.
2. **Unconditional stamp**: Original plan stamped on every run. This would mark stale chunks as current after partial incremental indexing.
