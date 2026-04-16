# RDR-079 Execution Plan — Operator Dispatch + Plan Execution End-to-End

**RDR**: `docs/rdr/rdr-079-operator-dispatch-and-execution.md` (status: accepted, 2026-04-15)
**Epic bead**: `nexus-wc3`
**Branch convention**: `feature/nexus-<bead-id>-<short-desc>`
**Priority**: all beads P1 (matches RDR priority).

## Summary

RDR-078 shipped plan-centric retrieval infrastructure but its scenarios cannot execute end-to-end. This plan closes that gap in seven phases. Amendment 1 (in the RDR) moves the operator pool into the existing `nexus` MCP server as core infrastructure (same tier as T1/T2/T3 singletons); no third MCP server is introduced. All 15 success criteria (SC-1..SC-15) must pass for epic closure.

## Bead Hierarchy

| Bead | Phase | Type | Title |
|------|-------|------|-------|
| `nexus-wc3` | — | epic | RDR-079: Operator Dispatch + Plan Execution End-to-End |
| `nexus-bfs` | P1 | task | tool-output contract (additive `_structured` flag) |
| `nexus-oei` | P2 | feature | operator pool inside nexus MCP server |
| `nexus-oei.1` | P2.1 | task | pool core — async workers + streaming RPC + retirement + health |
| `nexus-oei.2` | P2.2 | task | pool session lifecycle + PID-liveness reconciliation |
| `nexus-oei.3` | P2.3 | task | `resolve_t1_session()` — env-first at four call sites |
| `nexus-oei.4` | P2.4 | task | worker-mode MCP entry point (tool-surface restriction) |
| `nexus-oei.5` | P2.5 | task | session-record schema extension (`pool_pid` + `pool_session`) |
| `nexus-wc3.1` | P3 | feature | five operator MCP tools |
| `nexus-wc3.1.1` | P3.1 | task | `operator_extract` |
| `nexus-wc3.1.2` | P3.2 | task | `operator_rank` |
| `nexus-wc3.1.3` | P3.3 | task | `operator_compare` |
| `nexus-wc3.1.4` | P3.4 | task | `operator_summarize` |
| `nexus-wc3.1.5` | P3.5 | task | `operator_generate` |
| `nexus-wc3.2` | P4 | task | runner integration (`_default_dispatcher` operator routing) |
| `nexus-o5q` | P5 | task | empirical `min_confidence` calibration |
| `nexus-rxk` | P6 | feature | `nx plan promote` CLI with gates |
| `nexus-wc3.3` | P7 | task | end-to-end scenario-seed integration tests |

## Dependency Graph

```
                        P1 (nexus-bfs) ─────────────┐
                                                    │
P2.5 (schema) ──► P2.2 (lifecycle) ──┐              │
P2.3 (resolve) ─────────────────────►┤              │
                                     ▼              │
                            P2.1 (pool core)        │
                                     │              │
           P2.4 (worker-mode) ───────┤              │
                                     ▼              │
                               P2 (nexus-oei)       │
                                     │              │
                                     ▼              │
                               P3 (nexus-wc3.1)     │
                             (rolls up P3.1..P3.5)  │
                                     │              │
                                     ▼              ▼
                                    P4 (nexus-wc3.2)
                                     │
                                     ▼
                                    P7 (nexus-wc3.3)

                    P5 (nexus-o5q) ◄── parallel, independent
                    P6 (nexus-rxk) ◄── parallel, independent
```

Constraints honored:
- P1 blocks P4 ✓
- P2 blocks P3 ✓
- P3 blocks P4 ✓
- P4 blocks P7 ✓
- P5 parallel with P2/P3/P4 ✓
- P6 parallel with P2–P5 ✓
- P7 requires P3 + P4 both done ✓

## SC → Phase Mapping

| SC | Closed by |
|----|-----------|
| SC-1 (all 9 seeds e2e) | P3 + P4 + P7 |
| SC-2 (hung worker recovery) | P2.1 + P7 |
| SC-3 (token retirement drain) | P2.1 + P7 |
| SC-4 (min_confidence ROC) | P5 |
| SC-5 (RDR-078 regression) | P1 + P7 |
| SC-6 (8-hour soak) | P2 |
| SC-7 (json-schema validated) | P3 |
| SC-8 (latency baselines) | P3 + P7 |
| SC-9 (plan promote gate) | P6 |
| SC-10 (graceful no-auth degrade) | P2.1 + P7 |
| SC-11 (worker T1 isolation) | P2.3 + P3 |
| SC-12 (worker tool-surface) | P2.4 + P3 |
| SC-13 (session cleanup + PID reconciliation) | P2.2 |
| SC-14 (PPID-walk regression + fall-through) | P2.3 |
| SC-15 (`PoolConfigError` without env) | P2.1 + P3 |

## Critical Path

`P2.5 → P2.2 → P2.1 → P2 → P3 (×5) → P4 → P7`

Shortest irreducible path. P5/P6 can proceed in parallel at any time.

## Parallelization Opportunities

- **Immediate** (no deps): P1, P2.3, P2.4, P2.5, P5, P6 → all can start day 1
- **After P2.5**: P2.2 unblocks
- **After P2.2 + P2.3**: P2.1 unblocks
- **After P2** (all five subs done): P3.1–P3.5 can all be developed in parallel (same pattern, different prompt + schema)
- **After P3 + P1**: P4 unblocks
- **After P3 + P4**: P7 unblocks

## Testing Strategy

- **Unit** (no auth required): P1 dispatcher tests, P2.3 resolve_t1_session, P2.5 schema, P2.4 worker-mode registration filter, P5 calibration harness, P6 promote CLI dry-run.
- **Integration with subprocess but no API calls**: P2.1 pool core (mock `claude` subprocess via echo/sleep), P2.2 session lifecycle, P2.4 worker tools/list.
- **Live auth-required integration** (`@pytest.mark.integration`): P3 operator tools, P7 end-to-end seed plans, pool-survival scenarios.

All integration tests opt-in via `uv run pytest -m integration`. Unit + mocked suite must remain green for every commit (`uv run pytest` default-excludes `integration`).

Schema regression gate: `P1` must not break any existing RDR-078 T1 or plan-runner test — CI runs full unit suite before merge.

## Rollout Sequence

1. **Week 1 – Foundations**: P1 (tool-output contract), P2.5 (schema), P2.3 (`resolve_t1_session`), P5 (calibration dataset assembly in parallel), P6 (promote CLI in parallel).
2. **Week 2 – Pool infrastructure**: P2.2 (lifecycle, depends on P2.5), P2.4 (worker-mode), P2.1 (pool core, depends on P2.2 + P2.3). P2 rolls up when all five subs green.
3. **Week 3 – Operators**: P3.1..P3.5 in parallel (five sub-PRs, each small: role prompt + schema + tool registration + unit + live integration).
4. **Week 4 – Wiring + validation**: P4 (runner integration), then P7 (E2E seeds + pool-survival), close epic when SC-1..SC-15 all recorded green.

## Open Questions (from RDR PQs and Risks)

Carried forward for resolution during implementation; each is a candidate for a targeted research bead if it blocks progress.

- **PQ-1 / Risk — prompt cache boundary per worker**: start with unbiased dispatch; measure warm-cache hit rate in P7. If tail latency stalls P3 SLAs, add operator-type affinity.
- **PQ-2 — `min_confidence` final value**: determined empirically by P5 (SC-4 artefact in `docs/rdr/rdr-079-calibration.md`).
- **Risk — `claude auth status --json` schema drift**: add defensive parse + CI smoke test in P2.1 (check `loggedIn` key presence before value).
- **Risk — `schema_version: 1` pinning**: shipped as strict-match in P3; v2 compatibility deferred to a follow-up RDR if ever needed.
- **Risk — concurrent worker rate limits (`429`)**: per-worker backoff in P2.1; no pool-wide coordination at ship. Revisit if observed.
- **Risk — `store_get_many` batching**: baseline non-batched cost in P4 first; batching only if measured cost warrants it. If added, hard cap ≤300 IDs per call per ChromaDB `MAX_QUERY_RESULTS` quota, explicit error on overflow.
- **Out-of-scope confirmation**: `nx_answer` MCP tool and multi-LLM operator routing stay in RDR-080; `analytical-operator` agent file is NOT deleted by this RDR.

## Persistence Verification Checklist

- [x] Epic `nexus-wc3` created with 15-SC acceptance.
- [x] All 7 phase roll-up beads present (`nexus-bfs`, `nexus-oei`, `nexus-wc3.1`, `nexus-wc3.2`, `nexus-o5q`, `nexus-rxk`, `nexus-wc3.3`).
- [x] 10 sub-task beads (5 under P2, 5 under P3) present.
- [x] Inter-phase deps: P1→P4, P2→P3, P3→P4, P3→P7, P4→P7 added.
- [x] P5 independence restored (pre-existing dep on P2 removed).
- [x] P2 sub-deps: P2.5→P2.2, P2.3→P2.1, P2.2→P2.1 added; P2 depends on all five subs.
- [x] P3 depends on all five P3 subs.
- [x] All beads priority P1.
- [x] Labels set: `rdr-079` + `phase-pN` (+ `p2-sub` / `p3-sub` where applicable).
- [x] Each bead carries `--design` (files + test command) and `--acceptance` (SCs closed).

## Next Step: plan-auditor

**Task**: Validate the RDR-079 execution plan for operator-dispatch + end-to-end plan execution.
**Input Artifacts**: `docs/plans/2026-04-15-rdr-079-operator-dispatch-plan.md`; epic `nexus-wc3`; phase beads `nexus-bfs`, `nexus-oei` (+ 5 subs), `nexus-wc3.1` (+ 5 subs), `nexus-wc3.2`, `nexus-o5q`, `nexus-rxk`, `nexus-wc3.3`.
**Deliverable**: Plan validation report with go/no-go decision; verify dependency graph matches RDR §Phases and Amendment 1; verify SC coverage.
