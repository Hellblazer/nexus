---
id: RDR-067
title: "Cross-Project RDR Observability"
type: process
status: draft
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
accepted_date:
related_issues: ["RDR-065"]
---

# RDR-067: Cross-Project RDR Observability

> Stub. RDR-065 ships first; this RDR depends on having data to measure.

## Problem Statement

The "silent scope reduction" failure mode is by definition invisible
within a single closed RDR — every gate is green, the post-mortem is
honest, and the close ships as `implemented`. The pattern only becomes
visible **across** RDRs and **across** projects. ART filed the canonical
writeup after observing at least 3 instances in its own project memory.
Nexus has not yet audited its own RDR corpus for the same pattern, and
has no infrastructure to detect it across projects.

ART's writeup proposes 5 observability metrics that, if tracked
continuously, would surface the pattern at scale:

1. **Close-reason distribution**: ratio of `implemented` to
   `partial`/`superseded`/`reverted`. Healthy probably 70-90% implemented;
   >95% suggests `partial` is underused (the pattern's signature).
2. **Follow-up bead aging**: for RDRs closed as `implemented`, median
   age of P2+ beads created within 48h of close. >90 days = parking-lot
   signal.
3. **Problem-statement closure rate**: for each closed RDR, fraction of
   problem-statement gaps that have a code pointer in close artifacts.
4. **Composition-failure reopen rate**: number of beads reopened after
   integration test surfaced a composition failure. Near-zero = workarounds
   dominate.
5. **Divergence-language density**: grep density of "divergence,
   workaround, deferred, follow-up" in post-mortems per project per
   quarter.

Plus a cross-project sharing mechanism: ART filed `rdr_process/failure-mode-silent-scope-reduction`
to the nexus T2 store with `ttl=0`. The convention should generalize:
every project that hits the pattern files a sibling entry under the
`rdr_process` namespace, building a cross-project corpus that the
metrics run against.

### Enumerated gaps to close

#### Gap 1: No metric collection mechanism exists

The 5 metrics above are not currently computed for any project. Nexus
has the storage layer (T2 memory, T3 catalog) and the index (post-
mortems are already in `knowledge__rdr_postmortem__{repo}`) but no
skill or CLI command that walks them and produces a metric report.

#### Gap 2: No `rdr_process` collection convention exists

ART filed one entry under `rdr_process`. The convention is not
documented, not advertised in nexus skills, and there is no template
for what an `rdr_process` entry should contain. Without a convention,
sibling entries from other projects will be inconsistent and hard to
aggregate.

#### Gap 3: No `nx:rdr-audit` skill exists

Even if the metrics existed and the collection convention existed, no
skill currently exposes them as a runnable audit. Users would have to
write ad-hoc queries.

## Context

### Background

This RDR is one of three siblings to RDR-065 — see RDR-065 §Context
and RDR-066 §Background for the full sibling landscape. This sibling
is the **observability** track: it does not change any agent behavior,
but it makes the failure mode measurable across projects so the other
interventions can be evaluated for effectiveness.

### Why deferred

Two reasons. First, the metrics need data to measure. Until RDR-065
ships and produces at least one cycle of close-time data, there is no
baseline to compare against. Second, ART currently has the only entry
under `rdr_process`. Building a cross-project audit infrastructure
around a corpus of size 1 is premature — wait until at least one other
project files an entry to validate the convention.

### Drift condition

**If RDR-067 has not moved from `draft` toward Phase 1 within 120
days of RDR-065 closing as `implemented`, reopen RDR-065 and re-
evaluate its close reason.** The observability infrastructure is the
measurement layer that tells us whether RDR-065 actually worked. If
it drifts indefinitely, RDR-065's success claim becomes unfalsifiable
— an RDR that fixes a failure mode without being able to measure the
fix is in the same category as an RDR that claims `implemented` with
an unclosed gap. The drift condition exists because the measurement
is load-bearing, not ornamental.

An early-trigger condition also applies: if within the first 6 months
of RDR-065's operation, three or more RDR closes produce ambiguous
outcomes (replay fires but nobody can tell if the flagged divergence
is real), RDR-067 priority escalates from P3 to P2 and Phase 1
should start immediately. Ambiguity is the signal that measurement
is needed now.

### Technical Environment

- **T2 memory** (SQLite + FTS5): the storage layer for `rdr_process`
  collection entries
- **`knowledge__rdr_postmortem__{repo}` collections**: where post-mortems
  are already archived; the divergence-language density metric runs over
  these
- **Catalog link graph**: could be used to track follow-up bead
  relationships explicitly (each follow-up bead becomes a catalog node
  with a `forwards-from: RDR-NNN` edge)
- **No existing audit skill machinery**: would need to design from
  scratch

## Research Findings

[Pending. Do not start until RDR-065 has shipped baseline data.]

### Critical Assumptions

- [ ] **CA-1**: ART's 5 metrics are the right metrics. Some may be
  redundant; some may need refinement based on what nexus's own corpus
  reveals.
  — **Status**: Unverified — **Method**: Spike — compute all 5 against
  the nexus corpus and inspect the results
- [ ] **CA-2**: The `rdr_process` collection convention is sufficient
  with one entry per project incident. May need richer structure
  (per-incident sub-namespaces, link graph between incidents) once the
  corpus grows.
  — **Status**: Unverified — **Method**: Wait for second project to
  file an entry
- [ ] **CA-3**: A `nx:rdr-audit` skill is the right surface (vs. a CLI
  command, vs. a recurring scheduled task). May change after seeing
  RDR-065 close-time data and how often the audit needs to run.
  — **Status**: Unverified — **Method**: Wait for usage signal

## Proposed Solution

[Pending. Stub only.]

### Sketched approach

1. Define the `rdr_process` T2 collection convention: required fields,
   tag schema, naming pattern. Document in `nx/skills/rdr-create/SKILL.md`
   as the recommended channel for filing failure-mode reports.
2. Build `nx:rdr-audit` skill that computes the 5 metrics against a
   project's RDR corpus and post-mortem collection.
3. Add a recurring trigger (CronCreate or manual) that runs the audit
   weekly and writes results to a project-scoped T2 entry.
4. Once the corpus has ≥3 projects' entries, add cross-project
   aggregation: a separate skill or report that runs the metrics over
   the entire `rdr_process` namespace.
5. Surface anomalies (e.g., projects with >95% `implemented` close
   reason, or median follow-up bead age >90 days) to the user.

## Alternatives Considered

[Pending.]

## Trade-offs

[Pending.]

## Implementation Plan

[Pending.]

## References

- ART canonical writeup: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`
- T2 entry (cross-project, ttl=0): `rdr_process/failure-mode-silent-scope-reduction`
  — currently the only entry in the `rdr_process` namespace
- RDR-065 (close-time funnel sibling — produces the data this RDR
  needs to measure)
- RDR-066 (enrichment-time sibling)
- RDR-068 (composition failure detection sibling)
- T3 collections: `knowledge__rdr_postmortem__nexus` (post-mortem
  archive — divergence-language density runs over this)
- T2 collection (proposed): `rdr_process` (cross-project incident
  reports)
- Skills to create: `nx:rdr-audit` (new)

## Revision History

- 2026-04-10 — Stub created as deferred sibling to RDR-065.
