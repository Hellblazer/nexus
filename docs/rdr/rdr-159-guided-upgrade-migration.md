---
title: "Guided Chroma-to-Service Upgrade Migration: One Survivable Command over the Proven ETL Primitives"
id: RDR-159
type: Architecture
status: closed
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-13
accepted_date: 2026-06-13
closed_date: 2026-06-13
related_issues: [nexus-luxe6]
related: [RDR-152, RDR-153, RDR-155, RDR-156]
related_external: [conexus:RDR-001, conexus:RDR-002]
---

# RDR-159: Guided Chroma-to-Service Upgrade Migration

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> Relocated from a conexus draft (conexus:RDR-002): the orchestration engine is
> nexus-owned (the migration primitives, validation, detection, and the
> `nexus-luxe6` blocker all live here); conexus adds only a thin `conexus upgrade`
> UX veneer that calls this engine. The conexus draft is superseded by this RDR.

## Problem Statement

Since RDR-155 P4a (2026-06-10) T3 vector serving routes exclusively through the
PG16 + pgvector + Java nexus-service stack; the Chroma serving paths are retired
(the surviving Chroma read client exists only for the migration ETL). A PyPI
release cut from `develop` today is **not user-survivable**: an existing user
upgrading into it would find their permanent knowledge unreadable until they
hand-assemble PG16, the pgvector extension, the service JAR, the service stack,
the right embedding credentials, and then *manually sequence* a Chroma-to-pgvector
data migration.

This is the standing release blocker `nexus-luxe6`. The engine half is done — the
ETL is built and was proven in production (RDR-155 run, 2026-06-10: 115,716 chunks,
zero lost, copy-not-move). What is missing is the **conductor**: a single guided
flow that detects a user's existing Chroma data and drives the proven primitives in
the correct order, with the validation, serving-window handling, and rollback gating
that make the cutover safe — across **both** upgrade paths (cloud and local-only).

Without it, the manual path cost ~1 hour of expert diagnosis and hit 5 distinct
first-run failures **on the author's own machine** (nexus-jdpn9). A normal user
would dead-end. This RDR designs the engine that turns ~8 ordered manual steps into
one survivable command, the load-bearing prerequisite to lifting `nexus-luxe6`.

## Context

### Ownership

**nexus owns the orchestrator engine; conexus adds only a thin entry-point/UX shim.**
The orchestration state machine — detect → provision → key-gate → T2 → T3 → validate
→ unlock/rollback — lives in nexus as one tested code path (`nexus.migration` +
`nx upgrade --migrate` / `nx migrate-to-service`), co-located with the primitives it
sequences, the validation it runs, and the blocker it lifts. conexus exposes a thin
`conexus upgrade` veneer (prompts, cost/duration preview, plugin-install surface)
that calls this engine. Rationale and rejected alternatives in §Alternatives.

### The two upgrade paths (embedding-model axis)

There are **two orthogonal axes**, and both must be handled:

- **Source leg** — where the Chroma data lives: on-disk local `PersistentClient`
  (`~/.config/nexus/chroma`) and/or ChromaCloud (REST). A user may have either or
  both; running only one leg is "a silent half-migration."
- **Embedding model** — encoded in every collection name
  (`<content_type>__<owner>__<model>__v<n>`): `minilm-l6-v2-384` (ONNX, 384-dim,
  **local-only**) vs `voyage-context-3` / `voyage-code-3` (1024-dim, **cloud**).
  The migration already dim-dispatches per-collection on this segment
  (`vector_etl._dim_for_collection` → `chunks_384` vs `chunks_1024`), and the
  service re-embeds per-collection accordingly (ONNX for 384 collections, Voyage
  for voyage collections — `service/.../Main.java` `EmbedderRouter`).

These axes are independent. The user-facing **upgrade paths**, keyed on the
service's wired embedders (`EmbedderRouter.modelEmbedders`), are:

1. **Cloud / Voyage user** — collections `*__voyage-*__v1` (1024-dim). Re-embed is
   server-side via Voyage; **requires `NX_VOYAGE_API_KEY`**.
2. **Local-only / ONNX user** — collections `*__minilm-l6-v2-384__v1` (384-dim).
   Re-embed is server-side via local ONNX; **requires NO Voyage key**.
3. **Unsupported-model collections** — a model segment the service wires in **no**
   mode in the current deployment (e.g. `bge-base-en-v15-768`: present in the
   Python `vector_etl._MODEL_DIMS` registry but absent from `EmbedderRouter` in both
   local and cloud mode, so the service would 422-reject its upserts). These MUST be
   **detected and BLOCKED pre-migration** with a diagnostic listing the affected
   collections (re-index required) — never run into a `migrated-failed` dead end
   (gate S1). This is why detection (RF-2) classifies model per-collection and
   PRE-GATE checks each model against the live service's wired embedders, not just a
   static ONNX-vs-Voyage assumption.

A user may also be **mixed** (local Chroma holding voyage collections, or a mix of
supported + unsupported models). The gate and re-embed path are therefore
**per-collection-model**, resolved against the live service's embedder registry,
not global. (This corrects gap #4 below, which an earlier draft hardened into an
*unconditional* Voyage gate that would have blocked every local-only user.)

### What already exists (the proven primitives)

| Capability | nexus surface | State |
|---|---|---|
| Provision PG16 + pgvector + service | `nx init --service` | shipped (nexus-pebfx) |
| **T2** SQLite → PG, 7 stores | `nx storage migrate all` | shipped (RDR-153 P3) — ladder order, one report, verification gate (`total_failed == 0`) |
| **T3** Chroma → pgvector | `nx storage migrate vectors [--cloud] [--rollback] [--dry-run]` | shipped (RDR-155 P5) — per-leg, per-collection-model dim-dispatch, idempotent on `(tenant, collection, chash)`, copy-not-move, rollback |
| Count / taxonomy verification | `vector_etl.verify_counts` / `verify_taxonomy_consistency` | shipped |
| Migration report / triage | `nx storage migration-report show` | shipped |
| Embedding-mode fail-loud | service refuses voyage-token collections in ONNX mode | shipped (nexus-pebfx.2) |

The dangerous part — the ETL that moves 115k chunks across both models — is done and
de-risked.

### What is missing (verified by code read 2026-06-13)

No top-level guided flow, and no Chroma-data detector wired to migration (the
`embed_migrate` logic in `nx init` re-embeds stale-*dimension* collections, a
different concern). Each gap is a footgun the production run hit:

1. No detect-and-drive entry point (the user must independently sequence ~8 steps).
2. T2 and T3 are unsequenced and unjoined; **T2-catalog must land before T3
   manifest-orphan validation is non-vacuous** (the production run's manifest
   validation was a VACUOUS-PASS because T2 had not run).
3. Two-leg vector footgun (local and cloud run separately; nothing detects which
   legs hold data).
4. **Embedding-model gate is not modeled** — the Voyage key is needed only for
   voyage-model collections; a local-only user needs none. Must be per-collection,
   not an unconditional precondition.
5. No serving-window handling — for an upgrading user, pgvector is empty at
   migration start and Chroma serving is retired; the mid-migration experience is
   undefined (see Proposed Solution; gate finding C1).
6. No unlock/rollback gating tying "serving cutover complete" to "migration
   validated clean."
7. Stale-aspects debt: `document_aspects` is 100% stale at cutover (nexus-f1m8s).

## Research Findings

Verified by code read of the nexus engine, 2026-06-13, hardened by the finalization
gate critique (2026-06-13, T2 `nexus_rdr/rdr002-gate-2026-06-13`).

### RF-1 (VERIFIED): production-hardened settings are baked-in defaults

The 600s upsert timeout is the call-site default in
`http_vector_client.upsert_chunks` (`timeout=600`, http_vector_client.py:465). NUL
sanitization is service-side (Java, every upsert; nexus-rvfwj / PR #1152). The
orchestrator inherits both by construction; it passes no special settings.

### RF-2 (VERIFIED — primitives): detection is reliable; classifier is new code

`chroma_read.open_local_read_client` / `open_cloud_read_client` exist; cloud
credentials resolve from config (`chroma_api_key` / `chroma_tenant` /
`chroma_database`). **The classifier must report two axes per collection: source leg
AND embedding model** (the model is in the collection name — RF for the two-paths
design). It must `list_collections()` + non-empty-probe per leg (distinguish
"configured but empty" from "has data").

### RF-3 (RESOLVED — answered NO; prerequisite required): manifest-orphan callable does not exist in Python

Two validation checks, different sources:
- `verify_taxonomy_consistency(t2_db_path, vector_client)` reads the **source
  SQLite** T2 and checks against pgvector `list_collections()` — **non-vacuous
  regardless of order** (the available floor).
- The **manifest-orphan** check reads the **migrated PG catalog**; it is vacuous
  until `migrate all` (T2, incl. catalog) populates those tables — confirming the
  T2-before-T3 ordering (§Approach P2).

**RESOLVED (gate C2):** the stored functions `nexus.manifest_orphans(dim)` /
`nexus.manifest_backfill()` ship in the JAR (`catalog-004-manifest-functions.xml`)
but there is **NO REST endpoint and NO `HttpVectorClient` method**; the only Python
artifacts (`vector_etl.manifest_orphan_sql` / `manifest_backfill_sql`) are
**deprecated, psql-superuser-only**. The orchestrator cannot call the manifest check
as written. **Prerequisite:** a nexus bead exposing the manifest functions as a
Python-callable via a service REST endpoint (e.g. `GET /v1/catalog/manifest/orphans?dim=`)
or an `HttpVectorClient` method (§Approach P3). Until then, the taxonomy check is the
non-vacuous floor.

### RF-4 (RESOLVED — prerequisite required, ~300 LOC, not a small refactor): T2 orchestration is CLI-bound

T3 is a clean importable library API (`migrate_local` / `migrate_cloud` /
`rollback_collections` / `verify_counts` / `verify_taxonomy_consistency`, structured
returns + `on_result` progress). **T2 `migrate all` orchestration is CLI-bound**:
`_build_store_etls` + the run loop + `build_report` + `_run_verification` (incl. a
psql subprocess) live in `commands/storage_cmd.migrate_all_cmd` (~300 LOC), not the
library. **Prerequisite (locked at accept, gate S2):** extract the T2 orchestration
into `nexus.migration` as a callable returning the report dict (the orchestrator and
the conexus veneer both consume it). The shell-out-and-parse fallback is explicitly
NOT chosen (it re-introduces CLI-output parsing + superuser-psql coupling).

### RF-5 (VERIFIED): rollback is free and complete

`rollback_collections` deletes from pgvector exactly the chashes present in the
source Chroma collections, never modifies the source (copy-not-move keeps it
immutable so the rollback id-set equals the migration id-set), returns exact counts,
and has a reachability probe so an unreachable service cannot read as a clean
"deleted 0". A failed validation returns the user to a fully-working pre-upgrade
state.

### RF-6 (VERIFIED — gate S3): the T3 count verification requires a quiescent write window

`vector_etl.migrate_collections` documents that the post-write count check assumes a
QUIESCENT window: concurrent serving/indexing writes into a collection during the
ETL inflate the target count and read as a (conservative) FAILURE. The orchestrator
**must drain or suspend background indexing** before T3 (or warn loudly and gate on
confirmation) — else an upgrading user with active indexing hits a spurious
count-mismatch failure and rolls back correct data. The production run avoided this
only because it ran in a controlled window.

## Proposed Solution

A single guided engine (`nx upgrade --migrate`, with a conexus `conexus upgrade`
veneer) that **wraps and sequences** the proven primitives:

```
1. DETECT      classify the Chroma footprint per collection: source leg (local /
               cloud) AND embedding model (onnx-384 / voyage-1024). Fresh user
               (no Chroma) → no-op success.
2. PROVISION   ensure the service stack is up (delegate to nx init --service /
               nx daemon service start); idempotent.
3. QUIESCE     CROSS-PROCESS suspend of background indexing via the migration-state
               sentinel (every aspect worker + nx index polls it); required for the
               T3 count check (RF-6). PRE-GATE verifies no live cross-process
               aspect-worker write-locks remain; BLOCK with offending pids if they do.
4. PRE-GATE    PER-COLLECTION-MODEL, resolved against the live service embedder
               registry: (a) unsupported-model collections (no wired embedder, e.g.
               bge-768) → BLOCK with a re-index diagnostic; (b) voyage-model
               collections → HARD-FAIL unless NX_VOYAGE_API_KEY present; (c)
               onnx-384 → proceed with NO key. Mixed: evaluate each subset.
5. SERVE-MODE  enter "migrating" state: the CLI/MCP serve degraded-with-LOUD-warning
               ("knowledge migrating — results incomplete until upgrade completes"),
               NEVER silent empty results (gate C1). Chroma is not re-served (it is
               retired) — the warning + a progress surface stand in for it.
6. T2          run migrate all (incl. catalog) FIRST; require total_failed == 0
               (so T3 manifest validation is non-vacuous).
7. T3          run migrate vectors for EVERY detected leg; refuse partial-leg
               success. Per-collection dim-dispatch is already in the ETL.
8. VERIFY      non-vacuous: verify_taxonomy_consistency (floor) + manifest-orphans
               (after T2; via the RF-3 prerequisite) + verify_counts. Indeterminate
               / mismatch BLOCKS unlock (warning, never a pass).
9. UNLOCK /    on clean validation: clear the "migrating" marker → serving normal.
   ROLLBACK    on failure: surface the report, offer migrate vectors --rollback,
               keep Chroma intact, leave the marker set (still degraded-loud).
```

### Cross-process migration state (gates C1 + S2 — one mechanism)

Two needs share one root: the serving-window banner (C1) and the indexing quiesce
(S2) both require the **separate, long-lived MCP processes** to observe a state the
CLI migration process sets. `aspect_worker.drain_worker` is process-local (it stops
only the calling process's worker; resident MCP-process workers keep indexing), and
`mcp/core.py` read surfaces have no migration awareness. A CLI-local flag is
therefore insufficient — the mechanism MUST be cross-process.

**Design:** a single migration-state record polled by every process —
a `~/.config/nexus/migration.state` sentinel file (atomic write; cheap stat-poll;
survives the CLI process exiting) holding `{phase, started_at, collections_total,
collections_done, failure?}`. Both the read surfaces and the aspect workers poll it:

- **Read-surface coverage (enumerated, locked for P2):** `mcp/core.py` `search()`,
  `store_get()`, `store_get_many()`, the `nx_answer` plan runner, and the `nx search`
  CLI. Each, when `phase == migrating`, prepends a LOUD banner ("knowledge
  migrating: N/M collections done, results incomplete") and returns whatever has
  landed (monotonically improving) — never a bare empty result. `phase ==
  migrated-failed` keeps the banner + points at the report and rollback.
- **Indexing quiesce (S2):** each aspect worker (and `nx index`) checks `phase` at
  the top of each work cycle and **suspends** while `migrating` — a cross-process
  suspend the process-local `drain_worker` cannot provide. A PRE-GATE check verifies
  no live aspect-worker write-locks remain across processes before T3 starts; if any
  persist, BLOCK with the offending pids (do not silently run into the false-failure
  count mismatch of RF-6).

**State values:** `not-migrating` (default / post-unlock, serve normally) →
`migrating` (set at step 5, banner + suspend) → `migrated`/cleared at step 9
(UNLOCK) or `migrated-failed`. `collections_done/total` drives the progress surface;
on a resumed run the orchestrator recomputes done-vs-total by source-vs-target count
at detection (the upsert is idempotent on `(tenant, collection, chash)`, so re-runs
are safe and the progress is derived, not trusted from a stale marker).

**Atomicity + crash recovery (gate-3 condition):** writes to `migration.state` use a
`.tmp` sibling + `os.rename()` (POSIX-atomic) — NOT a bare `write_text` (the existing
`phase_review_sentinel` precedent is non-atomic and would let a poller read a partial
payload). Because the marker outlives the writing process, a CLI crash between a
valid T3 completion and the UNLOCK clear would strand a permanent `migrating`/
`migrated-failed` state; a named escape hatch (`nx migration --clear-state`, tracked
as a bead) lets the user recover — after re-running detection, which recomputes
done-vs-total from live counts, so clearing is safe.

The sentinel is the single source of truth for "has this install completed
migration" and is the load-bearing new mechanism this RDR introduces; everything
else wraps existing primitives.

Design principles: wrap-don't-reimplement (zero new ETL); idempotent + resumable
(re-running skips completed legs/collections); fail-loud + gate-hard at every step;
rollback is a first-class exit; one migration report artifact is the triage + unlock
evidence.

### Components

- **nexus (engine):** the orchestration state machine + per-collection detection
  classifier + the migration-state marker + the manifest-callable (RF-3) + the T2
  orchestration extraction (RF-4), as a callable `nexus.migration` API. CLI:
  `nx upgrade --migrate` / `nx migrate-to-service`.
- **conexus (veneer):** a thin `conexus upgrade` — prompts, `--dry-run`
  cost/duration preview, plugin-install surface — calling the nexus engine. No
  orchestration logic of its own.

## Alternatives Considered

- **nexus core engine + conexus thin shim (CHOSEN).** One tested code path in nexus,
  co-located with primitives/validation/blocker; conexus UX veneer on top.
- **Pure conexus wrapper.** *Initially chosen, then rejected (research RF-4):* puts
  thin orchestration in a different repo from everything it orchestrates and from
  the blocker it lifts — paying a cross-repo API contract, dual release cadences,
  and two-repo E2E tests for a flow that is mostly sequencing over nexus primitives.
- **Fully engine-only (no conexus surface).** Simplest, but leaves the
  consumer/plugin audience without the guided surface RDR-001 productizes. CHOSEN =
  this + a thin veneer.
- **Do nothing — document the manual steps.** Rejected: documentation does not make
  the gauntlet (5 first-run failures) survivable.
- **Auto-migrate silently on first post-upgrade call.** Rejected: a multi-hour,
  cost-bearing re-embed must be explicit and consented.
- **Unconditional Voyage-key precondition.** Rejected (the two-paths catch): blocks
  every local-only / ONNX user, who legitimately needs no Voyage key.

## Trade-offs

- **Engine in nexus, veneer in conexus** — a stable `nexus.migration` API the veneer
  pins; mitigated by a contract test (P4).
- **Detection can misclassify.** Mitigation: detection is advisory + confirmed;
  nothing is deleted (Chroma untouched), so a wrong call is recoverable by re-run.
- **Cost + duration are user-visible** (Voyage path). Mitigation: `--dry-run`
  surfaces per-leg counts + a cost/time estimate before commit; the per-model
  pre-gate prevents a wasted wrong-model run.
- **Quiescence requirement** (RF-6) interrupts active indexing. Mitigation: the
  drain is part of the flow; resumable so indexing restarts after unlock.

## Approach — Implementation Plan

> Phased, draft — lock at gate. Each phase folds `/conexus:phase-review-gate` +
> stacked review (code-review-expert + substantive-critic + test-validator).
> Engine phases P0–P3 land in nexus; P4 adds the conexus veneer.

0. **P-1 — prerequisites (named, locked at accept).** Two nexus beads BLOCK the
   engine: (a) **RF-4** extract the T2 `migrate all` orchestration into
   `nexus.migration` (~300 LOC, structured report return). This includes a
   verification-path decision (gate S3): `migrate_all_cmd._run_verification` shells
   out to psql, which the library call cannot keep (RDR-152 bars a direct PG
   connection in Python). Lock at accept: route count verification through a service
   REST endpoint, OR accept an INDETERMINATE verification surfaced as a non-blocking
   warning (the `total_failed == 0` gate still holds on the report). (b) **RF-3**
   expose `nexus.manifest_orphans(dim)` / `manifest_backfill()` as a Python-callable
   (service REST endpoint or `HttpVectorClient` method).
1. **P0 — Detection + dry-run preview (nexus).** Per-collection classifier (source
   leg × embedding model, model resolved against the live service embedder
   registry) + `--dry-run` reporting what would migrate (per-leg, per-model counts;
   unsupported-model collections flagged; cost/time estimate) without touching
   anything.
2. **P1 — Cross-process state mechanism + pre-gate + provisioning (nexus).** The
   `migration.state` sentinel (atomic write, cross-process poll) — the load-bearing
   new mechanism; wire the **read-surface banner** (enumerated: `mcp/core.py`
   `search`/`store_get`/`store_get_many`, `nx_answer` plan runner, `nx search` CLI)
   AND the **aspect-worker / nx-index suspend** to poll it (C1 + S2). Per-collection
   pre-gate (unsupported→block, voyage→key, onnx→proceed; C3/S1); cross-process
   write-lock audit (RF-6); idempotent service-stack ensure; fresh-user no-op.
3. **P2 — T2-then-T3 sequencing (nexus).** Set `phase=migrating`; drive `migrate
   all` (require `total_failed == 0`), then `migrate vectors` for every detected
   leg; refuse partial-leg success; update `collections_done/total` for the progress
   surface.
4. **P3 — Non-vacuous validation + unlock/rollback (nexus).** Both checks
   (taxonomy floor + manifest-orphans via the P-1 prerequisite) + counts;
   indeterminate/mismatch BLOCKS unlock; rollback path (RF-5); clear the marker on
   unlock. Stale-aspects (nexus-f1m8s): **advisory-only, never blocks unlock** —
   the report names the stale `document_aspects` count and points at re-extraction
   (`nx enrich aspects`); enrichment degrades until then but knowledge is served.
5. **P4 — conexus veneer + release sequencing.** Thin `conexus upgrade` calling the
   engine; a contract test pinning the consumed entry points; the two-release
   deprecation-window runbook (release N = both paths + this tool; release N+1 =
   RDR-155 P4b Chroma deletion). Lifting `nexus-luxe6` is gated here.

External gates (not engine code): conexus:xr7.8.9 production-scale recall /
hybrid-parity go-live; the deprecation-window release cadence.

## Test Plan

- **Detection unit tests** — per-collection classification across {local, cloud} ×
  {onnx-384, voyage-1024} × {empty, has-data}; malformed store → loud error.
- **Path end-to-end (sandbox, marked)** — (a) **cloud/Voyage**: key required,
  voyage-1024 collections, full detect→migrate→verify→unlock; (b)
  **local-only/ONNX**: NO key, minilm-384 collections, same flow succeeds; (c)
  **mixed**: local Chroma with multiple models — gate fires only on the voyage
  subset; (d) **unsupported-model** (bge-768): detected and BLOCKED pre-migration
  with a re-index diagnostic, no data touched, no `migrated-failed` dead end (S1).
- **Pre-gate tests** — voyage collection + absent key BLOCKS before any ETL call;
  local-only + absent key PROCEEDS; unsupported-model BLOCKS with the affected
  collection list.
- **Cross-process tests (C1 + S2)** — a separate process polling `migration.state`
  observes `migrating` and (read surface) degrades-LOUD / (aspect worker) suspends;
  the pre-gate write-lock audit BLOCKS when a foreign aspect-worker lock is live; a
  count-mismatch under simulated concurrent write is attributed, not a silent
  rollback. Cover EACH enumerated read surface (`search`, `store_get`,
  `store_get_many`, plan runner, CLI) — not CLI-only.
- **Serving-window tests (C1)** — `migrating` makes reads degrade-LOUD (never
  bare-empty); `migrated-failed` persists the warning; unlock clears it.
- **Sequencing tests** — T2 `total_failed > 0` BLOCKS T3; single leg ≠ multi-leg
  success.
- **Validation-gate tests** — indeterminate/mismatch BLOCKS unlock; rollback returns
  the user to a working pre-upgrade state.
- **Cross-repo contract test (P4)** — the consumed `nexus.migration` entry points
  exist with the expected signatures (the RDR-152 parity-tripwire discipline).

## Validation

> The MVV: a fresh checkout of the released artifact, given a representative
> pre-upgrade Chroma store, reaches a served-on-pgvector state via ONE command with
> zero manual sequencing — verified for BOTH a cloud/Voyage store and a
> local-only/ONNX store — and a forced-failure run returns to a fully working
> pre-upgrade state via the documented rollback, with the user never seeing a bare
> empty index mid-migration.

## Finalization Gate

**PASSED (2026-06-13)** after three rounds (substantive-critic, code-verified each
round). R1 (vs conexus:RDR-002): serving-window, manifest-callable, two-paths,
T2-extraction, quiescent-window. R2: cross-process marker+quiesce unified into one
sentinel, third (unsupported) model class, psql-verify decision. R3 (convergence):
all prior fixes verified coherent; the sole remaining condition — sentinel
atomic-write + crash escape hatch — is closed in §Proposed Solution; the two
Significants were explicitly downgraded by the gate to phase-plan beads (not RDR
conditions). Records: T2 `nexus_rdr/rdr002-gate-2026-06-13`,
`nexus/critique-rdr159-re-gate-2026-06-13`.

## References

- `nexus-luxe6` — the release-blocker marker this RDR exists to lift.
- T2 `nexus/release-boundary-since-p4a`; T2 `nexus_rdr/155-production-migration-complete`.
- RDR-153 (T2 migration), RDR-155 (T3 migration), RDR-156 (manifest validation
  vehicle), nexus-pebfx (install collapse), nexus-jdpn9 (first-run defects),
  nexus-f1m8s (stale aspects), nexus-rvfwj (NUL sanitization).
- conexus:RDR-001 (multitenant productization), conexus:RDR-002 (superseded draft).

## Revision History

- 2026-06-13 — Created in nexus (RDR-159), relocated from conexus:RDR-002 (draft).
  Ownership: nexus core engine + conexus thin shim. Folds the first gate's findings:
  C1 serving-window state machine, C2 manifest-callable prerequisite (RF-3 resolved
  NO), C3 two-upgrade-paths / per-collection-model Voyage gate, S2 T2-extraction
  prerequisite, S3 quiescent-window step, O1 stale-aspects disposition.
- 2026-06-13 — Re-gate (2nd) folded in: 3 new findings — C1' cross-process
  serving-window marker (CLI-local flag insufficient; MCP processes are separate) +
  S2 cross-process aspect-worker quiesce UNIFIED into one `migration.state` sentinel
  polled by read surfaces AND workers; S1 third model class (unsupported, e.g.
  bge-768) detected+BLOCKED pre-migration; S3 T2-extraction psql-verification
  decision locked to P-1a; stale-aspects = advisory-only. Read surfaces enumerated.
