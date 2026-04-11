# RDR-066 Execution Plan: Composition Smoke Probe at Coordinator Beads

**RDR file**: `docs/rdr/rdr-066-composition-smoke-probe-at-coordinator-beads.md`
**Status**: accepted (gate iteration 5, 0 Critical + 0 Significant, 2026-04-11)
**T2 metadata**: `nexus_rdr/RDR-066`
**T2 gate record**: `nexus_rdr/066-gate-latest`
**Author**: strategic-planner
**Created**: 2026-04-11

## Epic

- **nexus-qdj** — RDR-066: Composition Smoke Probe at Coordinator Beads (P1, epic)

## Plan structure

This plan decomposes RDR-066 implementation into **six phases plus one deferred follow-on**. The phase boundaries are lifted verbatim from the RDR's §Implementation Plan section; the dependency topology encodes the parallel 1a || 1b structure and the disjoint blocking relationships Phase 2 ← 1b and Phase 3 ← 1a.

### Phase summary table

| Phase | Bead | Priority | Scope | Blocks | Blocked by |
|---|---|---|---|---|---|
| 1a | nexus-9ps | P1 | Runtime hard-case spike (CA-1/CA-2/CA-3 resolution, Read-only vs Read+Serena decision) | nexus-2pl (Phase 3) | — |
| 1b | nexus-ssp | P1 | CA-5b retrospective ART plan graph lookup (~30 min, no runtime) | nexus-3k9 (Phase 2) | — |
| 2 | nexus-3k9 | P1 | Plan-enricher prompt diff (detection + tagging + post-write verification + probe-run step emission) + Scenario 5/5b tests | nexus-5vn (Phase 4), nexus-b2i (CA-5 full follow-on) | nexus-ssp (Phase 1b) |
| 3 | nexus-2pl | P1 | `nx/skills/composition-probe/SKILL.md` creation, py/java/ts runners, MVV against RDR-073 retrospective | nexus-5vn (Phase 4) | nexus-9ps (Phase 1a) |
| 4 | nexus-5vn | P2 | Plugin release (next patch after 3.8.4), version bump + smoke test | nexus-gxr (Phase 5a) | nexus-3k9 (Phase 2), nexus-2pl (Phase 3) |
| 5 | nexus-txl | P2 | Recursive self-validation (parent — tracks 5a, 5b, 5c) | — | nexus-gxr (5a), nexus-n1k (5b), nexus-tfb (5c) |
| 5a | nexus-gxr | P2 | Synthetic retcon injection → probe catches | nexus-n1k (Phase 5b), nexus-txl | nexus-5vn (Phase 4) |
| 5b | nexus-n1k | P2 | Independent critic on RDR-066 pre-close (substantive-critic dispatch) | nexus-tfb (Phase 5c), nexus-txl | nexus-gxr (Phase 5a) |
| 5c | nexus-tfb | P2 | Real self-close via RDR-069 gate (first real RDR close under active RDR-069 critic) | nexus-txl | nexus-n1k (Phase 5b) |
| CA-5 full follow-on | nexus-b2i | P2 (may escalate) | Cross-bead method-ownership detection (deferred — see CA-5b outcome rule) | — | nexus-3k9 (Phase 2) |

All ten children are linked to the epic via `parent-child` dependency type. Phase ordering uses the `blocks` default type.

## Dependency graph (ASCII)

```
                              nexus-qdj (epic)
                                     |
                      +--------------+--------------+
                      |                             |
                  nexus-9ps                     nexus-ssp
               (Phase 1a spike)            (Phase 1b retrospective)
                      |                             |
                  nexus-2pl                     nexus-3k9
              (Phase 3 skill)            (Phase 2 enricher diff)
                      \                             |
                       \                            +--> nexus-b2i
                        \                           |  (CA-5 full, deferred)
                         \                          |
                          +-------+------+----------+
                                  |
                              nexus-5vn
                          (Phase 4 release)
                                  |
                              nexus-gxr
                         (Phase 5a synthetic)
                                  |
                              nexus-n1k
                        (Phase 5b critic)
                                  |
                              nexus-tfb
                         (Phase 5c real close)
                                  |
                              nexus-txl
                       (Phase 5 parent, tracks 5a/5b/5c)
```

## Critical path

The critical path is:

```
Phase 1a (spike) → Phase 3 (skill) → Phase 4 (release) → Phase 5a → 5b → 5c
```

Phase 1b and Phase 2 run alongside but feed Phase 4 earlier — **Phase 4 cannot ship without Phase 2 complete**, so Phase 2 is a parallel constraint on the critical path length, not a shortening.

Phase 1a is the load-bearing Phase 1 task because it resolves three unverified gating assumptions (CA-1/CA-2/CA-3) and the skill shape decision (Read-only vs Read+Serena). Phase 1b is cheap (~30 min) and resolves one gating assumption (CA-5b).

## Parallelization opportunities

**Phase 1**: Phase 1a and Phase 1b are fully parallelizable. They share no tool budget, no files, and no CA dependencies. Dispatch both simultaneously.

**Phase 2 and Phase 3**: CAN run in parallel *after* their respective Phase 1 tasks complete. Phase 2 needs Phase 1b; Phase 3 needs Phase 1a. They do not block each other.

**Phase 5 sub-tasks**: 5a → 5b → 5c is serialized by design — each feeds evidence into the next.

## Risks captured in bead descriptions

The following gate-time risks are embedded in the relevant bead descriptions for the implementer:

- **OBS-3**: RDR-067 deferral risk. The fallback heuristic's unbounded false-positive rate if RDR-067 is deferred is documented in nexus-3k9 (Phase 2).
- **OBS-C**: Phase 1b source (a) accessibility not verified at gate time. Documented in nexus-ssp (Phase 1b) with fallback source order (archive → post-mortem grep → plan doc).
- **CA-5b outcome rule**: The CA-5 full follow-on (nexus-b2i) has an explicit priority-escalation rule keyed to CA-5b outcome — stays P2 on PASS, rises to P1 on PARTIAL, rises to P0 on FAIL.
- **Scenario 5b load-bearing test**: The silent-omission negative test is explicitly flagged as load-bearing in nexus-3k9 (Phase 2). The bead description includes the "dead code" framing so the implementer understands why the test is non-negotiable.

## CA disposition reference (from T2 `nexus_rdr/066-gate-latest`)

| CA | Status | Resolved by |
|---|---|---|
| CA-1 (hard-case probe generation) | Unverified — spike | Phase 1a (nexus-9ps) |
| CA-2 (failure message interpretability) | Unverified — spike | Phase 1a (nexus-9ps) |
| CA-3 (latency bound) | Unverified — spike | Phase 1a (nexus-9ps) |
| CA-4 (reliable tagging) | FEASIBLE-WITH-DIFF (Finding 4) | Phase 2 Scenario 5 + 5b (nexus-3k9) |
| CA-5 fallback (≥2 deps heuristic) | ZERO-COST (Finding 4) | Phase 2 Scenario 5 (nexus-3k9) |
| CA-5 full (method-ownership lookup) | DEFERRED | nexus-b2i (not on critical path) |
| CA-5b (fallback-to-full equivalence on 4/4) | Unverified — retrospective | Phase 1b (nexus-ssp) |
| Retained CA: bd metadata.coordinator substrate | VERIFIED (id 714) | Already satisfied |
| Retained CA: LLM contract generation easy case | VERIFIED (id 715) | Already satisfied |

## Prerequisite status

RDR-069 shipped as PR #147 merged to main as commit `5a7fa60` on 2026-04-11. The `bd dep add` ordering constraint between RDR-066 and RDR-069 implementation epics from the RDR prose is trivially satisfied at plan time and has not been encoded as a hard dependency in the new bead graph (documented in the epic bead description).

## Out of scope / explicitly not in this plan

- **CA-5 full** implementation — tracked in nexus-b2i, may or may not be un-deferred based on CA-5b outcome
- **Structural hook for Gap 3** — the RDR §Scope honest note (2026-04-11, line 65) explicitly calls this out as a future RDR. Gap 3 is closed convention-based in Phase 2 (probe-run text step in enriched bead description). A structural `bd` state-transition hook is out of scope.
- **RDR-067 audit loop** — the RDR references RDR-067 as the correction channel for coordinator over-tagging. RDR-067 is a separate RDR tracked outside this plan.
- **RDR-068 dimensional contracts** — belt-and-suspenders layer, separate RDR, not coupled to this plan.

## Quality gates

- [x] Epic bead created (nexus-qdj)
- [x] Phase task beads created (nexus-9ps, nexus-ssp, nexus-3k9, nexus-2pl, nexus-5vn, nexus-txl)
- [x] Phase 5 sub-task beads created (nexus-gxr, nexus-n1k, nexus-tfb)
- [x] CA-5 full follow-on bead created (nexus-b2i)
- [x] Parent-child edges linking all phases to the epic
- [x] Phase sequencing blocks encoded (Phase 2 ← 1b; Phase 3 ← 1a; Phase 4 ← 2+3; Phase 5a ← 4; 5b ← 5a; 5c ← 5b; Phase 5 parent tracks 5a+5b+5c)
- [x] `bd ready` shows exactly Phase 1a and Phase 1b as the only ready tasks (plus the epic itself)
- [ ] Plan audited by plan-auditor (next step)
- [ ] Plan enriched by plan-enricher (after audit)

## Next step

Dispatch **plan-auditor** to validate this plan against the codebase before enrichment. Plan-auditor will check that the bead descriptions match the RDR's stated scope, that no steps are missing, and that the TDD-first discipline is embedded in each implementation task. After audit, plan-enricher will add execution context (file paths, symbols, test commands).
