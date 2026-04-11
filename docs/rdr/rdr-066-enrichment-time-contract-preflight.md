---
id: RDR-066
title: "Enrichment-Time Contract Pre-Flight"
type: process
status: draft
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
accepted_date:
related_issues: ["RDR-065"]
---

# RDR-066: Enrichment-Time Contract Pre-Flight

> Stub. Do not start until RDR-065 has shipped and produced at least one
> close-time funnel measurement. This RDR has explicit dependencies on
> RDR-065's findings about which failures actually slip through.

## Problem Statement

The "silent scope reduction under composition pressure" failure mode
(documented by ART, canonical: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`,
T2: `rdr_process/failure-mode-silent-scope-reduction`) has root causes
spread across multiple lifecycle stages. RDR-065 closes the **close-time
funnel** subset. This RDR scopes the **enrichment-time** subset.

ART's RC-1 and RC-2 identify two enrichment-time failures:

- **RC-1**: Unit-scope tests are isolationist by design. Composition is
  never exercised until the final integration bead, where N-1 beads of
  sunk cost make reopens prohibitively expensive.
- **RC-2**: Bead enrichment is structural (classes, methods, fields)
  rather than mechanical (input shapes, output shapes, preconditions,
  caller-side invariants). The dimensional questions that would catch
  composition failures in 5 minutes are not asked because the enrichment
  template does not require them.

ART proposed two interventions for these:

- **INT-1**: Dimensional / contract pre-flight in bead enrichment. The
  enrichment template gains a mandatory `contracts:` section per touched
  method. The plan-enricher agent cannot complete enrichment without
  filling it in.
- **INT-2**: Composition smoke probe at coordinator beads. After any
  coordinator/root bead is enriched, a 30-50 line end-to-end smoke runs
  BEFORE the next bead begins. Composition failures surface at IMPL-M+1
  instead of IMPL-N, bounding reopen cost.

### Enumerated gaps to close

#### Gap 1: Plan enricher does not produce dimensional contracts

The `nx:enrich-plan` skill and plan-enricher agent produce structural
plans (file paths, function names, acceptance criteria) without
requiring per-method input/output shape declarations or precondition
contracts. This means dimension mismatches, type incompatibilities, and
caller-side invariant violations are discoverable only when the code
runs — typically at the final integration bead.

#### Gap 2: No "coordinator bead" concept exists in beads or plans

The composition smoke probe (INT-2) requires the system to know which
beads are coordinator/root beads — the ones that compose earlier work.
Today there is no metadata flag, no naming convention, and no plan
field that distinguishes a coordinator from a leaf. Without this, the
probe trigger has nothing to fire on.

#### Gap 3: No `nx:composition-probe` skill exists

INT-2 requires a new skill that takes a coordinator bead's main entry
point, generates a minimal end-to-end smoke against realistic input,
and runs it before the next bead begins. This skill does not exist.

## Context

### Background

This RDR is one of three siblings to RDR-065, all responding to ART's
documented failure mode. The three siblings split by lifecycle stage:

- **RDR-065** (close-time funnel): INT-4, INT-7, INT-5 wrapper, template
  change. Already drafted.
- **RDR-066** (this RDR — enrichment-time): INT-1, INT-2, plus the
  coordinator-bead concept. Stub.
- **RDR-067** (cross-project observability): the 5 metrics + the
  `rdr_process` T2 collection convention + new `nx:rdr-audit` skill.
  Stub.
- **RDR-068** (composition failure detection): INT-3 mid-session
  workaround gating, beginning as a research RDR mining ART incidents
  for a regex bank. Stub.

### Why deferred

Two reasons. First, RDR-065 must ship and produce a baseline measurement
of which failures actually pass through the close-time funnel. Without
that measurement, RDR-066's interventions are speculative — we don't
know whether enrichment-time fixes will catch failures the close-time
funnel already caught for free. Second, the "coordinator bead" concept
is a structural change to how plans relate to beads, and that change
deserves its own design pass once we understand what RDR-065 has and
hasn't fixed.

### Drift condition

**If RDR-066 has not moved from `draft` to a state where Phase 1
investigation has started within 120 days of RDR-065 closing as
`implemented`, reopen RDR-065 and re-evaluate whether the enrichment-
time scope should be folded back into RDR-065 or explicitly
abandoned.** 120 days gives RDR-065 time to produce meaningful
baseline data. If the data shows the close-time funnel is catching
the failure mode effectively, RDR-066 may become lower priority or
be abandoned — which is fine, as long as the decision is made
explicitly and not by drift.

### Technical Environment

- **`nx:enrich-plan`** (`nx/skills/enrich-plan/SKILL.md`): the skill
  that produces enriched plans from sketches
- **plan-enricher agent**: the agent the skill dispatches
- **bead metadata**: managed by external `bd` tool; we cannot add fields
  directly. May need a wrapper convention or a "coordinator" tag in the
  description text that the probe skill greps for
- **No existing composition-probe machinery**: would need to design from
  scratch, drawing on RDR-068's regex bank for failure detection

## Research Findings

### Investigation

#### Finding 1 (2026-04-11): bd 1.0.0 has native first-class custom metadata

**Source Search** against `bd create --help` and live JSON roundtrip
verification on bd 1.0.0 (Homebrew). T2: `nexus_rdr/066-research-1-ca3-verified`.

The stub assumed "we cannot modify beads internals" and scoped Gap 2
around a tag/naming convention. That assumption is wrong in a good way:

- **`--metadata string`** — bd accepts arbitrary JSON (`--metadata
  '{"coordinator":true,...}'` or `@file.json`). Stored at top-level
  `metadata` in `bd show --json` output, no key collisions with bd's
  own fields. Roundtrips losslessly.
- **`--waits-for-gate all-children`** — bd natively has a "wait until
  all child beads complete" gate (the default). This is literally the
  coordinator-bead semantic Gap 2 sketches.
- **`--design`, `--context`, `--acceptance`, `--notes`, `--spec-id`,
  `--external-ref`** — structured fields that can host per-bead
  contracts without abusing `--description`.

**Design implications** for when Phase 1 of this RDR begins:

- **Gap 2** (coordinator bead concept) — no hack needed. Set
  `metadata.coordinator=true` on coordinator beads; keep
  `--waits-for-gate all-children` (default); probe trigger is
  `bd list --json | jq '.[] | select(.metadata.coordinator == true)'`.
- **Gap 1** (dimensional contracts) — contracts can live in
  `--design` / `--context` / `metadata.contracts` rather than jammed
  into free-form `--description`. This preserves description as
  narrative and contracts as structured data.
- **Gap 3** (`nx:composition-probe` skill) — still build from scratch,
  but its trigger mechanism is now trivial (single `bd list --json`
  query against the metadata key).

**Risk noted**: bd's `metadata` is freeform JSON with no schema
enforcement. A downstream tool relying on `metadata.coordinator`
has no guarantee the key exists or is a bool. Same risk applies to
contracts stored in metadata. Address in Phase 1 design review.

### Critical Assumptions

- [ ] **CA-1**: RDR-065's close-time funnel does NOT catch the
  enrichment-stage failures that ART RC-1 and RC-2 describe. If it does,
  this RDR may be unnecessary.
  — **Status**: Unverified — **Method**: Wait for RDR-065 baseline data
- [ ] **CA-2**: The plan-enricher agent can produce dimensional contracts
  given a structured template prompt without requiring runtime symbol
  resolution. If contracts must be verified by Serena/JetBrains lookup
  at enrichment time, the cost may be prohibitive.
  — **Status**: Unverified — **Method**: Spike
- [x] **CA-3**: A "coordinator bead" concept can be expressed as a tag
  or naming convention without modifying the `bd` schema. (We cannot
  modify beads internals.)
  — **Status**: **VERIFIED (2026-04-11)** — stronger than assumed: bd 1.0.0
  has first-class `--metadata` JSON support plus native `--waits-for-gate
  all-children` coordinator semantic. No tag/convention hack needed.
  — **Method**: Source Search (bd CLI help + live JSON roundtrip) —
  see Finding 1 above. T2: `nexus_rdr/066-research-1-ca3-verified`

## Proposed Solution

[Pending — full design work deferred. Stub only.]

### Sketched approach

1. Update the `nx:enrich-plan` skill template to require a `## Contracts`
   section per enriched bead, listing per-method input shape, output
   shape, and caller-side preconditions.
2. Update the plan-enricher agent prompt to populate the contracts
   section by reading actual signatures via Serena/JetBrains symbol
   tools where available, falling back to "ASSUMED" markers where not.
3. Add a coordinator-bead convention: any bead whose enrichment names a
   composition point (e.g., a method that calls into ≥2 prior beads'
   outputs) gets a `coordinator: true` tag in T2 metadata or bead body.
4. Build `nx:composition-probe` as a new skill that, given a coordinator
   bead, generates a 30-50 line smoke test from the bead's stated entry
   point and runs it against realistic input.
5. Block coordinator-bead acceptance if the probe fails. Probe failure
   re-opens the dependency-chain bead at the cause of the failure, not
   at the coordinator.

## Alternatives Considered

[Pending.]

## Trade-offs

[Pending.]

## Implementation Plan

[Pending.]

## References

- ART canonical writeup: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`
- T2 entry (cross-project): `rdr_process/failure-mode-silent-scope-reduction`
- RDR-065 (close-time funnel sibling)
- RDR-067 (cross-project observability sibling — provides the
  measurement framework this RDR depends on)
- RDR-068 (composition failure detection sibling — provides the regex
  bank `nx:composition-probe` may need)
- Skills to modify: `nx/skills/enrich-plan/SKILL.md`,
  `nx/skills/composition-probe/SKILL.md` (new)

## Revision History

- 2026-04-10 — Stub created as deferred sibling to RDR-065.
