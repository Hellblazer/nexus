---
title: "Single-Ladder Convergent Upgrade: One Version, Auto-Applied Data Migrations, Delete the Upgrade Ceremony"
id: RDR-185
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: ""
created: 2026-07-16
related_issues: ["GH #1408", "GH #1405"]
related_rdrs: [RDR-076, RDR-142, RDR-143, RDR-144, RDR-155, RDR-159, RDR-162, RDR-170, RDR-174, RDR-176, RDR-178, RDR-180]
supersedes: []
related_tests: [tests/e2e/migration-rehearsal/run.sh, tests/e2e/upgrade-shakeout.sh]
---

# RDR-185: Single-Ladder Convergent Upgrade

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Upgrading a nexus install is fundamentally a simple operation: get current
code; when current code meets older data, migrate the data. The project
already implements this correctly for exactly one axis — T2 schema
migrations apply automatically on open, keyed by a version row
(`apply_pending`, RDR-076/RDR-170). No user has ever run a T2-schema verb.

Every transition since has instead been shipped as an EVENT with its own
ceremony, and the accumulated result is a graph the user must hold and
traverse — which edges apply depends on which ERA their install is from:

- package: `uv tool upgrade` / the RDR-143 lockstep hook (deferred a
  session; the download-vs-swap coupling is nexus-blao6)
- T2 schema + daemon cycling: `nx upgrade` (+ `--auto` SessionStart form)
- process freshness: `nx daemon restart-stale`
- engine binary: `converge_engine` + `PINNED_SERVICE_TAG`
- provisioning: `nx init --service` (RDR-174)
- T3 substrate: `nx migrate-to-service` + `--dry-run` pregate +
  `nx guided-upgrade` + the `/conexus:upgrade` skill (RDR-159/176/178)
- chunk-identity era: `nx collection reindex` / `backfill-hash` / the
  proposed `rewrite-ids` (GH #1408)
- embedder era: the RDR-162 384→768 re-index→migrate chain, `embed_migrate`
- hooks/config stanzas: `nx hooks update` + doctor drift checks

Field evidence, 2026-07-16 (the incident that forced the question): a
long-lived work install ran package upgrades faithfully for months, then
attempted the substrate migration and was BLOCKED on 18 collections
carrying pre-RDR-108 chunk ids — debt that (a) nothing surfaced before
migration day, (b) the guided path could not fix, and (c) the printed
remedy (re-index from source) was IMPOSSIBLE for the ~2,000 store_put-only
chunks that have no source files. The `/conexus:upgrade` skill additionally
crashed on the dry-run's designed non-zero exit. The maintainer's verdict:
"I'm the one who built this and I don't even know how to run the 67 steps"
— and the id remedy itself is a pure function of stored data
(`chash = sha256(chunk_text)`), requiring no re-index at all.

The structural defect: **detection is centralized — doctor already knows
every one of these states — but remediation is scattered across verbs the
user must discover, sequence, and re-run.** The product can see the whole
path and refuses to walk it.

## Constraints

- **Immutable-source rollback stands (RDR-176).** A substrate rung never
  mutates its source store; identity conformance is computed ON THE WIRE
  during ETL (the id is derivable from the chunk text being carried), so
  the Chroma source remains a byte-untouched rollback target.
- **GH #1390 stands.** Destination constraints are never weakened. Wire
  re-id computes the CORRECT content address for existing content; it does
  not force wrong ids through.
- **RDR-142's lesson binds the ladder:** a rung is recorded complete only
  when its VERIFY passed — the version pointer must never advance past
  deferred or failed work.
- **Unattended-capable (RDR-178).** Long rungs are resumable, batched, and
  report progress; the ladder as a whole is idempotent (re-run converges,
  never duplicates).
- **Consent only at genuine decisions.** The only prompts permitted are
  choices the product cannot make: a source that no longer exists
  (re-acquire vs drop), an explicit rollback. Everything derivable is
  automatic.
- Applies across local, service, and managed-cloud modes; a mode where a
  rung is N/A detects-and-skips (the f0pmd version-gate pattern: detect →
  current? skip → converge → verify).
- The engine-service release lifecycle stays separate (its version rides
  `REQUIRED_ENGINE_VERSION`); the ladder CONSUMES the pin as one rung, it
  does not replace the engine release process.

## Proposed Decision (draft — the gate test is that this stays one paragraph)

Upgrade is: update the code. All data transitions live in ONE ordered
migration ladder spanning every axis (T2 schema, T3 substrate, chunk
identity, embedder era, hooks/config), keyed by one stored data-version;
rungs auto-apply when newer code meets older data, each rung
detect→converge→verify, idempotent and resumable, with the source of any
substrate move kept immutable for rollback. `nx doctor` reports pending
rungs read-only; `nx upgrade` (and its SessionStart `--auto` form) is the
single trigger that walks the ladder; every other upgrade-cycle verb is
demoted to an internal primitive or deleted from the user-facing story.

## Decision Space

1. **One ladder, auto-applied (proposed).** Extend the proven T2
   `apply_pending` model to all axes. Big rungs (substrate ETL, embedder
   re-index) are resumable steps INSIDE the ladder, inheriting the
   RDR-176/178 machinery (batching, verify, reports, immutable source).
2. **Keep the graph, add an orchestrator verb** that shells the existing
   commands in order. Rejected as an end state (the graph remains the
   documented surface; ceremony is wrapped, not deleted) but acceptable as
   the FIRST implementation increment — the orchestrator's legs then
   migrate into ladder rungs one by one.
3. **Status quo + better docs.** Rejected: today's incident happened WITH
   the docs; the maintainer could not hold the graph.

Open sub-decisions for research:
- Trigger point: `nx upgrade` explicit + SessionStart auto (current
  `--auto` semantics) vs migrate-on-open for cheap rungs only (T2 already
  does this) with long rungs deferred to the explicit trigger. Where is
  the line — wall-clock budget per rung class?
- The single data-version: one scalar ladder position vs a version vector
  per axis reduced to "pending rungs". (RDR-142 argues for per-rung
  completion records either way.)
- Wire re-id specifics: identical-text collapse semantics on the
  destination (RDR-108 defines the end state; manifest position rows
  preserve composition) and the old→new mapping cascade for chash-span
  links and chash-keyed aspects riding the same ETL.
- RDR-180 alignment: the 16-byte→32-byte binary chash move is a FUTURE
  rung of this same ladder; the wire-re-id mapping machinery must be
  built as the reusable remap primitive 180 will need.
- What survives as internal primitives (surgical/dev use, tested, out of
  user docs) vs deleted outright.

## Success Criteria

- A fresh-or-ancient install converges with `nx upgrade` alone: the
  2026-07-16 work-instance shape (pre-RDR-108 ids, store_put-only
  collections, Chroma substrate) reaches current UNATTENDED — the
  18-collection report becomes a progress line, not homework.
- Zero re-embedding and zero source-file requirements for pure id-scheme
  conformance (wire re-id; the Chroma source untouched).
- `nx doctor` shows pending rungs; the user-facing upgrade documentation
  is one paragraph; the command inventory in the Problem Statement is
  demoted or deleted.
- The migration-rehearsal suite drives ONLY `nx upgrade` end-to-end across
  an era-spanning hop (old release + old engine + legacy ids → current)
  and stays green.
- No rung records complete without its verify (RDR-142 regression class
  pinned by test).

## Research

(To be filled during rdr-research: inventory every existing
detection+remediation pair and its current owner; the rung ordering /
dependency graph; trigger-point budget measurements; wire-re-id collision
census on real legacy collections — the 2026-07-16 footprint is 18
collections / ~6,026 chunks; what RDR-159/162/176/178 machinery lifts
directly into rungs vs needs rework; version-vector vs scalar ladder.)

## Decision

(Open — draft.)
