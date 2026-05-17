---
title: "RDR-110 post-mortem: Semantic Tuple Space"
rdr: RDR-110
status: closed
close_reason: implemented
closed_date: 2026-05-17
author: Hal Hildebrand
epic: nexus-qg7t
---

# RDR-110 Post-Mortem — Semantic Tuple Space

Close reason: **implemented**. Epic `nexus-qg7t` closed 2026-05-17 with 21/21 children complete. Surface ships in v4.32.x.

## What landed

The v1 Semantic Tuple Space primitive over ChromaDB + SQLite:

- 8 MCP tools: `out`, `read`, `take`, `ack`, `nack`, `list_subspaces`, `subspace_schema`, `subspace_stats`
- 8 `nx tuplespace` CLI subcommands mirroring the MCP surface
- 5 coordination skills wiring the tools into agentic workflows
- Static YAML-registered subspaces (Phase 1/2 of the RDR's plan)
- Atomic destructive read via `UPDATE … RETURNING`
- `take(block=True)` end-to-end with polling-based wake (RF-9 design)
- Retention sweep (expires_at + Chroma) — closes Gap 3 of the RDR
- 10-worker MVV harness validating concurrency claims
- Direct + daemon-mode integration; under `NX_STORAGE_MODE=daemon`, the watcher is daemon-internal and clients subscribe via the RDR-112 EventStream RPC

Key closing commits: `5de67dfb` (MVV harness), `c1511527` (`block=True` client side), `3aa0e9fe` (epic retirement sweep).

## Divergences from RDR proposal

1. **Phase re-numbering.** The RDR's §Implementation Plan called out 6 phases; the execution epic re-numbered them as P1–P4 covering the RDR's Phase 1 work plus the four-consumer landing surface. RDR Phases 5 (v2 wrappers over plans/scratch/memory) and 6 (deprecation/removal) are explicit `separate RDR` deferrals per the RDR text — not residual scope.
2. **Planning hold.** RDR-110 was accepted 2026-05-09 but planning was deliberately held pending RDR-112 substrate landing. The hold was lifted 2026-05-13 once daemon-as-tuplespace-host was settled. Net effect: tight coupling to RDR-112 was managed without scope creep.
3. **Watcher mode-split.** Triad rework added a load-bearing decision: `_TupleSpaceWatcher` runs in-process only under `NX_STORAGE_MODE=direct`. Daemon mode routes via the RDR-112 EventStream RPC (`nexus-m4gm`). This wasn't in the RDR draft; it was discovered during the RDR-110 / RDR-112 alignment pass.
4. **Six load-bearing post-triad decisions** were captured in the epic body up front and reflected in the bead descriptions, so each child carried enough context to execute without re-reading the parent.

## Lessons

- **Planning holds are cheap; cleanup is not.** Holding RDR-110 planning until RDR-112 substrate landed avoided two re-plans worth of churn. The hold-then-lift pattern is worth repeating when triad RDRs depend on each other.
- **Distinguish residual scope from explicit deferral.** Both grooming agents (RDR-110 and RDR-111) initially treated "phases mentioned in the RDR" as planning targets. The right answer was to read the RDR's own deferral markers — `(separate RDR)` lines 1651, 1659 — and decline to manufacture beads.
- **Execution-phase numbering can diverge from RDR-phase numbering.** As long as the close-out cross-walks them, this is fine; just don't pretend execution P3 = RDR Phase 3.

## Deferred / out-of-scope (RDR-author-declared, not silent reduction)

The RDR §Implementation Plan declares Phase 5 and Phase 6 out-of-scope at RDR-authoring time. Quoted verbatim from `docs/rdr/rdr-110-semantic-tuple-space.md`:

- Line 1654: `### Phase 5: Persistent addressable surfaces (separate RDR)` — body begins "Out of scope for this RDR." (line 1656) and ends "RDR-1NN to be authored once Phase 4 stabilises." (line 1660).
- Line 1662: `### Phase 6: Removal (separate RDR)` — body ends "Out of scope for this RDR." (line 1668).

`close_reason=implemented` (not `partial`) because the RDR's stated scope is Phases 1–4; all four shipped via epic `nexus-qg7t`. Phase 5/6 are forward references to future RDRs, not residual scope that was silently dropped. If a real consumer signals demand for unified threshold semantics across `memory_search` / `plan_match` / `scratch`, draft a new RDR-1NN at that time — do not revive RDR-110.

## References

- Epic: `bd show nexus-qg7t` (CLOSED, 21/21 children, P1)
- T2 memory: `nexus_rdr/110-planning-chain-2026-05-17` (permanent)
- Related RDRs: RDR-112 (storage-as-service, owns the daemon boundary), RDR-116 (tuplespace lock decomposition, accepted), RDR-117 (tuples.db durability and recovery, accepted)
- Global memory: `~/.claude/projects/-Users-hal-hildebrand-git-nexus/memory/project_rdr110_planning_landed.md`
