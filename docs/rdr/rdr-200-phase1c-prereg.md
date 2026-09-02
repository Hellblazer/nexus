---
title: "RDR-200 Phase 1c Pre-Registration: re-gate on the scoped plan runner, held-out set"
parent_rdr: RDR-200
parent_bead: nexus-4e75w
supersedes_result: docs/rdr/rdr-200-phase1b-gate-result.md
inherits_protocol: docs/rdr/rdr-200-phase1b-prereg.md
question_set: HELD OUT (not in the repo until the arms finish; sha256 of the frozen text list below)
created: 2026-09-02
status: frozen-before-arms
---

# RDR-200 Phase 1c Pre-Registration

Fresh pre-registration, written and committed before any Phase 1c arm
runs. Inherits every protocol value from Phase 1b (and through it Phase
1) except as stated here. Sam's decision (2026-09-01, after the Phase 1b
FAIL): fix nexus-rl59s and re-gate with a held-out set.

## 1. Substrate pinned

- Client develop `28d47fdb3` (nexus-rl59s), reinstalled; MCP servers to
  be reconnected before any arm. On top of Phase 1b's pins (tox2m
  `b390840ea`, 0bmhd `a9b229e7f`) it adds: plan retrieval steps default
  to `knowledge,code,docs,rdr` in `plan_run` and in the single-step
  reroute; the two builtin single-query templates widened to that
  default; the inline planner carries scoping rules (one scoped
  retrieval step per named artifact, content-shaped queries) and a
  prefix-sampled collection-name hint.
- Engine 0.1.93, tiering unchanged, `MAX_CONTINUATION_PROMPT_CHARS`
  unchanged. Arm model `claude-fable-5-1`; judge `claude-opus-5`.

## 2. Question set: HELD OUT

24 questions, assembled 2026-09-01 from the two benefiting shapes
(paper-corpus over indexed knowledge__ papers; RDR-research over this
repo), each anchored to a specific indexed document verified by a search
probe. Zero overlap with the Phase 1/1b frozen 24 and their overflow
pool. 35 of 36 candidates were authored for this set (the answer_runs
history had one unused answerable question left); provenance per
question names the anchor collection and title and the probe rank.
**The texts are NOT committed** (the Phase 1/1b set self-retrieved as
evidence in every arm); they live in the session gate artifacts and are
committed with the result document after the arms and judging finish.
Commitment: sha256 of the JSON list of the 24 frozen texts, in id order:
`48f5b4f2f07cf089284d265b5d5c14068b6d83bb0bf5248a2a009098316535db`.

Crowding scored by the inherited procedure (flat `search`, default
fan-out, `limit=10`, one blind `claude-opus-5` labeling each; crowded =
irrelevant fraction >= 0.5) over the 36-candidate pool: 17 crowded / 19
clean, labeling $14.04; self-retrieval of any gate artifact 0/36.
Selection rule (pre-committed): up to 12 per stratum in pool-id order.
Frozen set 12 crowded / 12 clean; 12 overflow candidates held in reserve
in pool-id order.

| id | pool | stratum | score | shape |
|---|---|---|---|---|
| q01 | h01 | crowded | 0.6 | paper-corpus |
| q02 | h02 | crowded | 0.7 | paper-corpus |
| q03 | h03 | crowded | 0.8 | paper-corpus |
| q04 | h04 | crowded | 0.6 | paper-corpus |
| q05 | h05 | crowded | 0.8 | paper-corpus |
| q06 | h06 | crowded | 0.6 | paper-corpus |
| q07 | h07 | crowded | 0.6 | paper-corpus |
| q08 | h08 | crowded | 0.7 | paper-corpus |
| q09 | h09 | crowded | 0.8 | paper-corpus |
| q10 | h10 | crowded | 0.6 | paper-corpus |
| q11 | h14 | crowded | 0.7 | paper-corpus |
| q12 | h15 | crowded | 0.6 | paper-corpus |
| q13 | h11 | clean | 0.0 | paper-corpus |
| q14 | h12 | clean | 0.0 | paper-corpus |
| q15 | h13 | clean | 0.1 | paper-corpus |
| q16 | h19 | clean | 0.1 | rdr-research |
| q17 | h20 | clean | 0.1 | rdr-research |
| q18 | h21 | clean | 0.0 | rdr-research |
| q19 | h22 | clean | 0.0 | rdr-research |
| q20 | h24 | clean | 0.0 | rdr-research |
| q21 | h25 | clean | 0.1 | rdr-research |
| q22 | h26 | clean | 0.2 | rdr-research |
| q23 | h27 | clean | 0.1 | rdr-research |
| q24 | h28 | clean | 0.0 | rdr-research |

Shape confound carried over from Phase 1: crowded is paper-heavy, clean
is RDR-heavy; this is the corpus's own structure on the default fan-out
and is reported, not corrected.

## 3. Caveats declared

Phase 1b caveats 1-3 stand (catalog-routed tools uncalibrated; legacy
2-segment names inert here; crowding measures the presentation
channel). Caveat 4 (self-retrieval) is REMOVED by construction for this
set; the crowding probe measured 0/36. New: the four-prefix default
widens the plan arms' fan-out from 48 to 57 collections on this install,
with incremental latency; a plan-arm degradation attributable to budget
exhaustion under the wider fan-out is reported per question, not
excused.

## 4. Arms, judging, pass/fail, telemetry

Unchanged from Phase 1b §§5-6, including the MCP-only restriction on the
caller-only arm (now in the brief from the first pass) and the per-
question retrieval-reach measurement. Pass = continuation >= headless
AND > caller-only, ties against continuation, INCONCLUSIVE is not a
pass.

## Revision History

- 2026-09-02 (before any arm ran) — **Plan library reseeded.** The two
  widened builtin templates are shipped as files but served from the T2
  plan library; `nx plan reseed` reconciled 2 drifted rows (471
  document-discovery, 468 corpus-coverage-check) so the live library
  matches `28d47fdb3`. Verified by a smoke probe: the Phase 1b q19 shape
  now fans out over 58 collections and returns RDR-070 and the
  search_engine/scoring sources; before the reseed it hit 11
  knowledge__ collections only. No other value changed.
