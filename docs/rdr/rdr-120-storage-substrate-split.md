---
title: "Storage Substrate Split: Substrate-Only Scope, No Co-Shipped Consumers"
id: RDR-120
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-19
accepted_date: 2026-05-21
related_issues: []
related_rdrs: [RDR-004, RDR-041, RDR-105, RDR-108, RDR-112]
supersedes: [RDR-112]
related_tests: []
implementation_notes: ""
---

# RDR-120: Storage Substrate Split: Substrate-Only Scope, No Co-Shipped Consumers

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

T2 and T3 are opened today as **library-mode file handles**. Inside a sandbox
container (Claude Desktop's bundled MCP runtime, ext-app sandboxes, `claude
-p` sub-processes with their own filesystem view), "open the file at
`~/.nexus/t2.sqlite`" silently resolves to a copy or an empty path. Each
container gets its own SQLite, its own chroma collections, its own memory
tier, and knowledge fragments invisibly across silos. T1 is correct as-is
(per-process working memory, RDR-105). T2 and T3 are the cross-session,
cross-instance tiers whose entire value proposition is that co-located
processes see the same state.

This RDR addresses **exactly that problem and nothing else**. The previous
attempt (RDR-110/111/112/113/118/119, scrapped 2026-05-19) bundled the
substrate split with a tuplespace, an event-projection cockpit, a host-trust
model, and a UI fabric. Six weeks in, the substrate's correctness was a
moving target because new abstractions kept landing on top of it. Tombstoned
designs preserved as historical reference; postmortem at
[`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`](../postmortem/2026-05-16-rdr110-113-remediation-chain.md).

### Enumerated gaps to close

#### Gap 1: T2 and T3 leak the storage substrate as a file path

`T2Database` and the T3 chroma client are opened by direct filesystem path.
Any process with read access to the path is a peer writer. Inside a sandbox
container that path either does not exist (silent fork to an empty DB) or
resolves to a bind-mounted copy (silent fork to a stale DB). No enforced
single-writer boundary, no liveness check, no version negotiation.

#### Gap 2: Multi-instance co-work fragments knowledge into silos

When Claude Desktop, a `claude -p` sub-process, and a separate IDE agent all
run on the same host, each opens its own T2/T3 handles. T2 writes from one
instance are invisible to the others until/unless they re-open the file
(and WAL behavior across containers is not guaranteed). Memory put in one
cockpit is unreachable from another. This is the same fragmentation problem
RDR-105 solved for T1 with env passdown, except that T1 fragmentation is
desired and T2/T3 fragmentation is broken by design.

#### Gap 3: No single-writer arbiter for future concurrency primitives

The previous attempt named several primitives this substrate would unlock
(atomic take, blocking take with `data_version` wake, event projection,
subspace registry). Those primitives are **out of scope** for this RDR by
design; the moratorium below blocks them. The relevant fact for *this* RDR
is that the file-handle access pattern leaves no place to hang them later
either. Establishing a daemon owner is a precondition; building anything on
top is a follow-on, after the substrate proves itself.

## Context

### Background

Three RDRs frame the problem:

- **RDR-105** moved T1 from on-disk session records to env-passdown of a
  host-local chroma. Settled the per-process working-memory question and
  proved the host-service-plus-env-discovery pattern.
- **RDR-108** locked the catalog-as-tree / T3-as-content-addressed-blob
  identity model. Doc identity is `Document.tumbler`; chunk natural ID is
  `sha256(chunk_text)[:32]`. Boundary work needs to preserve this.
- **RDR-112 (scrapped)** designed a service split with dual-primary
  discovery, UDS+TCP transport, storage-boundary lint, and an
  `NX_STORAGE_MODE` cutover flag. The design was sound; the arc around it
  was not. This RDR inherits the substrate design wholesale and discards
  the arc.

### Technical Environment

In scope: every persistent shared-state store opened by direct file path
under `src/nexus/`:

- **T2 (seven domain stores)** behind the `T2Database` facade:
  `memory_store`, `plan_library`, `chash_index`, `catalog_taxonomy`,
  `aspect_extraction_queue`, `document_aspects`, `telemetry`. SQLite +
  FTS5, WAL enabled.
- **T3 local mode**: `chromadb.PersistentClient(path)` + local ONNX
  embedder.
- **T3 cloud mode**: `chromadb.CloudClient`. Already service-shaped (remote
  HTTPS). Goes through the same `T3Client` abstraction for symmetry; no
  daemon needed.
- **CatalogDB** (`src/nexus/catalog/catalog_db.py:255`): own SQLite file,
  own `sqlite3.connect()`. Structurally identical to a T2 domain store and
  equally vulnerable to the container silo problem. **In scope** as P5.

Out of scope explicitly:

- **T1** stays per-process. RDR-105's env passdown is correct.
- **Cross-host federation**. TCP listeners are loopback-only by default;
  cross-host is a future RDR after substrate ships.
- **All RDR-110/111/113/118/119 consumer abstractions.** See [§ Scope
  Boundaries](#scope-boundaries) below.

Direct-file-open call sites under `src/nexus/` outside `src/nexus/db/`
(enumerated via grep + verified during A5; full inventory in T2 entry
`120-research-A5`):

- `src/nexus/commands/{plan,catalog,tier_status,upgrade,doctor,index}.py`
  (9 SQLite + 1 chromadb)
- `src/nexus/{health,mcp_infra,collection_audit,pipeline_buffer,_session_end_launcher}.py`
  (5 SQLite + 4 chromadb)
- `src/nexus/console/routes/health.py` (1 SQLite via `_sqlite3` alias)
- `src/nexus/catalog/{catalog_db.py,synthesizer.py}` (2 SQLite, P0-P4
  allowlisted, deleted at P5 cutover)

Three current sites use module-aliased imports (`import sqlite3 as
_sqlite3`): `commands/doctor.py` (lines 615, 720) and
`console/routes/health.py:131`. The lint must track module aliases at
import time, not only direct attribute references.

The lint banlist covers all three chromadb client classes
(`PersistentClient`, `CloudClient`, `EphemeralClient`) outside
`src/nexus/db/`, not only `PersistentClient`. Cloud-mode T3 goes
through the same `T3Client` abstraction for symmetry.

P0's lint pass locks this inventory as the baseline. CI asserts the
expected post-phase count of remaining direct opens at each phase
boundary (P2: chromadb sites drop to 3; P4: SQLite outside `db/` and
`catalog/` drops to 2; P5: SQLite outside `db/` drops to 0).

## Scope Boundaries

This is the most important section of this RDR. It is **load-bearing**: the
2026-05-19 scrap happened because the prior arc lacked an explicit
moratorium and accreted consumer abstractions onto an in-flight substrate.

### In scope (P0 through P6)

- T2 daemon: long-lived process owning the seven domain-store SQLite
  handle(s). Exposes the existing `T2Database` facade API over UDS + TCP.
- T3 daemon: managed `chroma run` subprocess. Exposes the existing chroma
  client API via `HttpClient`.
- Discovery: dual-primary (file at `~/.config/nexus/<tier>_addr.<uid>` and
  env vars `NX_T2_SOCK` / `NX_T2_ADDR` / `NX_T3_ADDR`).
  **Precedence rule (gate critique fix-in-place, 2026-05-21):** env
  var wins when set and non-empty; the file is the fallback when
  the env var is unset. If the env-var-named address is unreachable,
  the client fails loud rather than silently falling through to the
  file — operator-set env vars are an explicit override that we
  honor or report, never quietly route around. Container
  orchestrators set env vars; host-local daemons write files. Both
  can be live simultaneously; the precedence rule determines which
  the client uses, not the daemon's existence.
- Thin `T2Client` / `T3Client` mirroring the existing facade signatures.
  Call sites do not change beyond constructor injection.
- Daemon-owned schema migration. The daemon is the sole migration runner.
- `NX_STORAGE_MODE=direct|daemon` cutover flag. `direct` retains library
  mode for P0-P5 migration safety; deleted in P6.
- Storage-boundary lint (`nx doctor --check-storage-boundary`): AST scan
  that fails CI on `sqlite3.connect` / `PersistentClient` outside
  `src/nexus/db/` daemon-internal code.
- Day-2 introspection minimum: `nx daemon t2 exec --raw <SQL>` for
  read-only SQL inspection.
- CatalogDB collapses into T2 as the eighth domain store (P5).
- MVV: two `claude -p` sub-processes in different working directories share
  a `memory_put` / `memory_get` round trip.

### Out of scope, blocked by moratorium until P6+30 days

All of the following are valid future work. **None lands while substrate
work is in flight.** Each requires its own RDR filed *after* the substrate
has been on `main` continuously for ≥30 days under `NX_STORAGE_MODE=daemon`.

- Tuple-space primitives: atomic `in`/`out`/`rd`, blocking `take`,
  `data_version` polling wake (RDR-110 substance).
- Event-stream RPC: `EventStream(subspace_prefix, since_cursor) →
  Stream<Event>`, glob subspace matching, lifecycle events (RDR-110/111
  substance).
- Subspace registry: schema-validated tuple types, version handshake on
  subspace digest, plugin-supplied subspaces.
- Hook bridge writing into a daemon-owned tuplespace (RDR-111 substance).
- Cockpit panels reading off the event stream (RDR-111/119 substance).
- ORB bindings watcher reacting to tuples (RDR-111 substance).
- Host-trust model: peer-credential check on accept, multi-user UID
  separation, token-based auth (RDR-113 substance).
- Surfaces-as-tuples generalization (RDR-118 substance).
- UI fabric / A2UI realization (RDR-119 substance).
- Adding a new T2 domain store beyond the existing seven plus catalog
  (eight total).
- Adding a new RPC method to the daemon beyond what the existing facades
  already expose.
- Cross-host federation, non-loopback TCP bind, network auth.
- Schema migration helpers beyond `apply_pending`. Includes cross-tier
  migration coordinators, dependent-store migration ordering frameworks,
  and any new abstraction that wraps the migration step itself. The
  daemon owns `apply_pending`; that is the substrate's entire migration
  responsibility. Generalized migration helpers are a consumer concern.
  **Exception**: the daemon's own startup sequence calling
  `apply_pending` across the seven domain stores is substrate-internal
  and not subject to this moratorium — that's the daemon doing its job,
  not an externalized helper.
- Telemetry / breadcrumb instrumentation crossing the daemon boundary.
  The substrate may emit structured logs about its own lifecycle (start,
  stop, migration, RPC handler errors). It must not introduce a
  breadcrumb / request-id / span concept that consumers thread through
  their calls. RDR-115 (T2 daemon request tracing) is the right home for
  that work.

### Enforcement

- **§ Scope Boundaries is part of the Finalization Gate.** The gate's
  Layer 3 substantive-critic relay evaluates "Scope Verification:
  pass/warn/fail" against this section. Layer 1 structurally validates
  that the section exists and is non-placeholder.
- **PR-level**: any PR touching `src/nexus/db/` or the daemon entry points
  that adds a method not present in the pre-RDR `T2Database` /
  `chromadb.api` surface fails review.
- **Bead-level**: no bead may reference RDR-110/111/113/118/119 substance
  while RDR-120 is in flight. Such beads are filed as deferred against a
  future RDR.
- **Calendar-level**: the moratorium lifts P6 + 30 days, marked by a
  follow-on RDR if any consumer is wanted.

The moratorium is not a suggestion. It is the only structural difference
between this attempt and the scrapped one.

### Enforcement backstops (and known tooling gaps)

A7's verification (T2 entry `120-research-A7`) surfaced that the
enforcement chain rests on four working mechanisms and three tooling
gaps. Recording both so the discipline is honest about what it can and
cannot catch.

**Working mechanisms (solo-author context):**

- **Author self-policing.** Hal authored the moratorium. Before
  scaffolding any new substrate-adjacent RDR during P0-P5, Hal reads
  RDR-120 §Scope Boundaries first. Solo-author is single-point-of-
  awareness as well as single-point-of-failure; the moratorium being in
  the title and Problem Statement makes it hard to miss.
- **`/conexus:phase-review-gate` cross-walk (tooled).** At each phase
  closeout, the reviewer runs `/conexus:phase-review-gate 120 --phase N`.
  Pass 1 enumerates all §Approach items; Pass 2 validates each item has
  a closing-bead pointer or explicit `none` deferral. BLOCKED on any
  unaccounted item. This is a hard-enforcement preamble (Python script
  the agent cannot reason past) modeled on the RDR-065 Problem
  Statement Replay gate. Built as bead `nexus-j327` (commit
  `122feaff`, 791 LOC + 312 LOC tests). The regression test covers the
  actual `nexus-52lb` / RDR-112 Phase 1 silent-drop incident; the gate
  would have blocked that close. **Currently lives only on
  `archive/develop-2026-05-19`**; P0 prerequisite is restoring it to
  main before any phase opens.
- **Symmetric scope-gain check (manual).** `/conexus:phase-review-gate`
  catches scope LOSS (§Approach item with no closing bead) by design.
  Scope GAIN (closing bead that implements something from § Out of
  scope) is not currently tooled. RDR-120 adds a manual symmetric
  step: at each phase close, the reviewer also lists every closing
  bead and grep-matches its title and description against the §Out of
  scope banned-topic list. A bead matching a banned topic blocks the
  phase close until it is removed or the moratorium is explicitly
  amended.
- **PR-level review.** Each PR's diff is read against §Scope Boundaries.
  Any new method on `T2Database` / `chromadb.api` beyond the pre-RDR
  surface fails. In solo context, the author re-reads their own diff.
- **Bead-level enforcement.** `bd create` for substrate work requires
  the author to confirm the bead's substance is not in § Out of scope.

**Tooling gaps (block multi-author adoption):**

- **`/conexus:rdr-create` has no awareness of in-flight moratoriums.** A new
  RDR scaffolds without checking other active RDRs' §Scope Boundaries
  for topic matches. Mitigation in solo context: author self-check.
  Follow-on: add a `moratorium_blocks: <topic-list>` frontmatter field
  to active RDRs and have the skill grep new RDR's content against it.
- **`/conexus:rdr-gate` Layer 3 critique is single-RDR-scoped.** The
  substantive-critic receives only the RDR being gated; it does not
  enumerate active in-flight others. A new RDR can land cleanly through
  the gate while violating a peer's moratorium. Follow-on: extend the
  Layer 3 relay's Input Artifacts to include "active in-flight RDRs'
  §Scope Boundaries sections."
- **`/conexus:phase-review-gate` is scope-loss-only.** Catches §Approach
  items missing closing beads (silent scope reduction). Does NOT catch
  closing beads that implement § Out of scope work (silent scope
  expansion). Follow-on: a Pass 3 that enumerates closing beads and
  matches their descriptions against the active RDR's § Out of scope
  list. Until then, the symmetric check is manual reviewer discipline.
- **`bd create` does not grep description against moratorium banned-
  topic lists.** Bead-level enforcement is currently author discipline
  only.

These gaps are dormant during RDR-120's solo-author implementation
window. If substrate-adjacent work passes to another author, or if
moratorium discipline is generalized as a project pattern beyond
RDR-120, the tooling gaps must be closed first.

**P0 prerequisite: restore `/conexus:phase-review-gate` from archive.**
The skill exists at `archive/develop-2026-05-19` commit `122feaff`.
Files to restore: `conexus/commands/phase-review-gate.md` (307 LOC),
`conexus/skills/phase-review-gate/SKILL.md` (128 LOC),
`tests/test_phase_review_gate.py` (312 LOC), entries in
`conexus/registry.yaml` (11 LOC), `tests/test_plugin_structure.py` (4 LOC),
`docs/rdr/AGENTS.md` (2 LOC), `conexus/CHANGELOG.md` (16 LOC). Restoration
is a docs-and-tooling PR independent of any substrate work.

**Residual confidence gap.** A7 carries a 30% residual gap that no
ex-ante verification can close: "moratorium discipline holds across
six phases of actual implementation work." The previous attempt's
baseline was one new substrate-adjacent RDR every 3-5 days; RDR-120's
success criterion is zero such RDRs across P0-P5. Closed only by
doing the work.

## Research Findings

### Investigation

RDR-112's research (conducted 2026-05-12 via deep-research-synthesizer,
preserved in T2 entries `112-research-A1..A4` and `112-research-A3-spike`)
covers the substrate-substantive questions and is cited here rather than
re-conducted. This RDR adds a postmortem-derived findings layer on what
process discipline actually failed.

### Key Discoveries (substrate, cited from tombstoned RDR-112)

- T2 daemon is structurally required for the container case: Docker
  overlayfs blocks the `mmap` semantics SQLite WAL requires, so a
  bind-mounted host path opened from inside a container produces a
  separate WAL per process. Silent silo failure mode.
- T1's existing service shape (chroma HTTP over loopback, spawned at
  `session.py:494-504`, discovered via `~/.config/nexus/t1_addr.<pid>`
  + PPID walk) is a live production precedent.
- `chroma run` is FastAPI + uvicorn with a configurable thread pool
  (default 40). chromadb's "not for production" disclaimer attaches to
  `EphemeralClient` / `PersistentClient`; `HttpClient` is explicitly the
  "recommended production configuration."
- RDR-105's PPID-walk + discovery-file pattern fails in
  PID-namespace-isolated containers. The walk terminates at container
  init (PID 1) and `~/.config` is not bind-mounted in typical sandbox
  configurations. `NX_T2_ADDR` / `NX_T3_ADDR` env-var override is a
  co-primary discovery path, not a fallback.

### Key Discoveries (process, from 2026-05-16 postmortem)

- **`run_until_signal()` hang** (60 minutes lost, 3 CI cycles): a local
  `stop_event` only signal handlers could set wasn't shared with
  `daemon.stop()`. Fix landed in PR #795. Lock-in: any daemon stop signal
  must be hoisted to an instance attribute reachable from both signal
  handlers and direct callers, or replaced with `asyncio.Event` shared
  between them.
- **CLAUDECODE env-var gate**: bridge `emit()` early-returned when env
  var unset. All darwin dev shells had it; no CI runner did. Tests passed
  locally, failed only on CI, looked PR-introduced. Lock-in: any test
  that asserts on env-gated behavior must explicitly set the env via
  fixture.
- **Cross-contamination via worktrees**: parallel agents working on
  adjacent files in worktrees produced sibling PRs (#787, #788, #789)
  carrying portions of each other's code via git 3-way merge auto-resolve.
  Lock-in: substrate work runs on a single branch, serial commits, no
  parallel-agent fan-out across worktrees.
- **The 30-file unrebaseable PR (#786)**: y0nb audit follow-ups grew to
  1870 insertions while develop moved through ten daughter PRs. 70%
  overlap with merged work; extracted (#803) and closed as superseded.
  Lock-in: phase boundary = PR boundary. A PR that's still growing closes
  no phase.
- **Pre-existing develop failures masquerading as PR-introduced** (seven
  tests dragged in by y0nb merge artifacts). Lock-in: every substrate PR
  is gated on green CI on the base branch before the PR opens.
- **Migration race (`nexus-9eaz`)**: `_upgrade_lock` semantics under
  concurrent multi-process daemon startup, un-reproducible on darwin.
  Skipped, instrumented, never bottomed. Lock-in: daemon is the **sole**
  migration runner from day one. Multi-process migration concurrency is
  not a problem the daemon design has to solve, because by design only
  one process ever runs migrations.

### Critical Assumptions

- [x] **A1**: `chroma run` is production-quality enough to be the T3
  service. **Status**: Documented (High confidence). **Method**:
  Source Search of `chromadb/app.py` plus live T1 precedent. **Evidence
  reused from**: tombstoned RDR-112 §A1.
- [x] **A2**: SQLite WAL is not an acceptable substitute for a
  single-writer daemon in the container case. **Status**: Verified
  (High confidence). **Method**: Source Search +
  sqlite.org/wal.html. **Evidence reused from**: tombstoned RDR-112 §A2.
- [x] **A3**: Daemon-per-tier latency overhead is acceptable for common
  reads. **Status**: Verified (High confidence). **Method**: Spike
  (`/tmp/rdr112_a3_spike.py`, 2000 iters on real `MemoryStore`). Results:
  direct p50=85 µs / p99=286 µs; UDS p50=100 µs / p99=300 µs (+15 µs);
  TCP loopback p50=114 µs / p99=620 µs (+29 µs). All sub-ms. **Evidence
  reused from**: tombstoned RDR-112 §A3 (T2 entry
  `112-research-A3-spike`).
- [x] **A4**: Discovery via per-host UID file is universally adequate.
  **Status**: Refuted (High confidence). Mitigation: dual-primary file +
  env-var discovery. **Evidence reused from**: tombstoned RDR-112 §A4.
- [~] **A5** (new): **Storage-boundary lint catches every regression
  vector.** AST scan of `sqlite3.connect` (including module-aliased
  forms like `_sqlite3.connect`), `chromadb.PersistentClient`,
  `chromadb.CloudClient`, and `chromadb.EphemeralClient` outside
  `src/nexus/db/`, with a phase-gated `src/nexus/catalog/` allowlist
  removed at P5 cutover. **Status**: Partially Verified (High
  confidence in feasibility and refinements). **Method**: Source Search
  (ε-lint precedent + grep enumeration of call sites). **Evidence**:
  T2 entry `120-research-A5`. Summary: the existing AST-lint at
  `tests/test_no_direct_catalog_writes_outside_projector.py` is a 1:1
  structural precedent (path-prefix allowlist, `# epsilon-allow:
  <reason>` per-line override, alias-evasion handling). Current
  inventory: 27 SQLite call sites (9 in `db/` allowlisted, 2 in
  `catalog/` P0-P4 allowlisted then deleted at P5, 9 in `commands/`,
  5 top-level, 1 in `console/routes/`, 1 missed in the original §
  Technical Environment list); 8 chromadb call sites (3 in `db/`
  allowlisted, 4 in `health.py`, 1 in `commands/index.py`). Three
  refinements folded into the scope-boundary text above: cover all
  three chromadb client classes (not just PersistentClient), implement
  catalog two-phase allowlist (P0-P4 vs P5), track module-aliased
  imports (`import sqlite3 as _sqlite3` used by 3 current sites).
  Remaining 5% confidence gap: live spike against known-bad and
  known-good fixtures; assigned to P0 implementation.
  **Inventory refresh 2026-05-21 (conexus 4.33.1):** re-ran the
  AST/grep enumeration; counts unchanged (27 SQLite + 8 chromadb +
  3 module-aliased imports, same files). Line numbers of aliased
  imports shifted due to unrelated upstream edits
  (`commands/doctor.py` 615->607, 720->699;
  `console/routes/health.py` 131->122); alias mechanism unchanged.
  No regression vector has appeared that the proposed lint would
  miss. Evidence: T2 entry `120-research-A5-refresh` (id=1395).
- [x] **A6** (new): **A daemon as sole migration runner eliminates the
  `_upgrade_lock` race class.** With a single daemon process holding the
  SQLite handle for the lifetime of the tier, multi-process migration
  concurrency cannot arise; the `nexus-9eaz` instrumentation is
  unnecessary in daemon mode. **Status**: Verified (High confidence).
  **Method**: Source Search. **Evidence**: T2 entry `120-research-A6`.
  Summary: `_upgrade_lock` is `threading.RLock` (process-local) at
  `src/nexus/db/migrations.py:2877`; `_bootstrap_lock` docstring at
  `:2886-2894` explicitly: "Process-level only". Caller pattern at
  `src/nexus/db/t2/__init__.py:178-225` invokes `apply_pending` per
  T2Database construction, so N processes opening T2 concurrently = N
  concurrent migration runners with N independent locks. The nexus-9eaz
  failure is the cross-process case; the thread-case test at
  `tests/test_migrations.py:696-740` passes by construction because
  RLock works within a process. In daemon mode the daemon calls
  `apply_pending` once at startup before accepting connections; clients
  never call it. Cross-process race surface is structurally absent
  **once the cutover lands**.

  **Invariant scope (gate critique fix-in-place, 2026-05-21):** A6's
  cross-process-race-elimination invariant holds from P4+ steady
  state, NOT from P3 ship. P3 ships the T2 daemon but does NOT flip
  call sites: `NX_STORAGE_MODE=direct` remains the default for the
  P3 validation window (stress harness only, no calendar gate), so
  direct-open `T2Database` construction continues
  to call `apply_pending` per the existing pattern at
  `src/nexus/db/t2/__init__.py:178-225`. During the P3 window, daemon
  AND direct-open clients both run `apply_pending` via their own
  RLock instances. The cross-process race surface is therefore NOT
  yet eliminated in P3 — the daemon's `apply_pending` is one more
  concurrent runner alongside the direct-open ones.

  **P3 transition mitigation (revised after gate pass 4, 2026-05-21):**
  the P3 race is NOT prevented during the transition window; it is
  acknowledged and the design is safe under it. systemd / launchctl
  ordering helps for systemd-managed clients but cannot constrain
  `claude -p` subprocesses (forked by an active Claude Code session
  outside systemd's dependency graph) that open T2 directly while
  the daemon is mid-startup. **The actual cross-process safety story
  is:** (a) SQLite's statement-level write serialization — only one
  writer holds a write lock at a time at the OS level, so two
  processes racing on DDL get serialized by SQLite itself with the
  loser receiving `SQLITE_BUSY` and retrying — and (b) migration
  step idempotency — every step in `_MIGRATIONS` uses `IF NOT EXISTS`,
  `PRAGMA table_info`, or `INSERT OR IGNORE` guards, so a step
  already applied by the winner is a no-op when the loser retries.
  Note: `_upgrade_lock` in `src/nexus/db/migrations.py:2877` is a
  `threading.RLock` and provides ONLY within-process serialization;
  the docstring at `:2886-2894` explicitly states "Process-level
  only — cross-process safety relies on `INSERT OR IGNORE`". There
  is no `BEGIN EXCLUSIVE` wrap in `apply_pending`. Step idempotency
  is therefore the load-bearing invariant for cross-process safety
  during the P3 window — any migration step that violates it (e.g.
  a bare `INSERT INTO ... VALUES` without `OR IGNORE` or version
  guard) silently breaks this guarantee. The `nexus-9eaz` stress
  test is RE-ARMED for the P3 window, recast as: "daemon startup
  running concurrently with a direct-open `T2Database` construction
  must converge to the expected schema version without corruption;
  SQLITE_BUSY retries are acceptable; partial migrations are
  unacceptable." The test must verify step-level idempotency, not
  just aggregate convergence. If the test surfaces non-idempotent
  migration steps, those must be fixed before P3 ships (the
  idempotency requirement is on the migration code, not on the
  daemon). A CI lint that flags `INSERT INTO` / `CREATE TABLE` /
  `ALTER TABLE` in migration functions without an accompanying
  guard is a future option, deferred until empirically needed.
  Hard cross-process serialization via
  `_locking.acquire_directory_lock` around the `apply_pending` call
  site is also documented as a deferred option — not adopted now
  because step idempotency + SQLite statement-level locking
  already provides the safety guarantee that matters.

  **P4 implementation: flip `NX_STORAGE_MODE` default to `daemon`,
  remove `apply_pending` from `T2Client` and direct-open `T2Database`
  paths, reframe the skipped stress test as a daemon-startup
  invariant ("daemon refuses second start against the same path").**
  From P4+ the daemon is the sole `apply_pending` caller; the
  cross-process race surface is structurally absent and the
  `nexus-9eaz` invariant holds as the verified state of A6.

  Net: A6 stays `[x]` Verified — but for the P4+ steady state. P3 is
  a transition window with the mitigations above, not a state in
  which the invariant is already true.
- [~] **A7** (new): **The moratorium is enforceable through written
  scope boundaries, per-phase cross-walks (tooled), and author self-
  policing, scoped to solo-author work.** A multi-mechanism enforcement
  chain (§ Scope Boundaries in the RDR + `/conexus:phase-review-gate`
  hard-enforcement cross-walk at each phase closeout + PR/bead-level
  author review) is sufficient to block scope drift across six phases
  under solo authorship. The general (multi-author) case has
  unaddressed tooling gaps (cross-RDR moratorium awareness in
  scaffolding and gating). **Status**: Partially Verified (Medium-High
  confidence at solo-author scope; Unverified for multi-author general
  case). **Method**: Counterfactual Analysis (4 of 4 documented
  scope-drift events from the scrapped RDR-110-119 arc would have been
  blocked by §Scope Boundaries + author self-check before filing) +
  Tooling Inspection (rdr-gate skill at conexus plugin 4.32.12 +
  `/conexus:phase-review-gate` skill at `archive/develop-2026-05-19`
  commit `122feaff`). **Evidence**: T2 entry `120-research-A7`.
  **Tooling that exists**: `/conexus:phase-review-gate` (bead `nexus-j327`,
  791 LOC + 312 LOC tests including a regression for the nexus-52lb
  silent-drop incident) implements a two-pass hard-enforcement
  cross-walk: Pass 1 enumerates §Approach items; Pass 2 validates
  evidence per item; BLOCKED if any item lacks closing-bead pointer
  or explicit `none` deferral. Currently on `archive/develop-2026-05-19`
  only; not on main. P0 prerequisite: restore this skill to main
  before any phase opens. **Tooling gaps remaining**: (i)
  `/conexus:rdr-create` has no awareness of in-flight moratoriums;
  (ii) `/conexus:rdr-gate` Layer 3 critique is scoped to single RDR and
  does not cross-check against active in-flight others; (iii) `bd
  create` does not grep against active moratorium banned-topic lists;
  (iv) `/conexus:phase-review-gate` catches scope LOSS (§Approach item with
  no closing bead) but not scope GAIN (closing bead that lands work
  from § Out of scope). The first three are dormant in solo-author
  context; (iv) is partially mitigated by author re-reading the diff
  but is a real residual gap. **Residual 25% confidence gap** (down
  from 30% with phase-review-gate accounted for): proof of "moratorium
  holds across six phases of actual implementation work" still comes
  only from doing the work without violating it. Captured in §
  Enforcement Backstops below.
- [x] **A8** (new): **The substrate-vs-consumer boundary holds for test
  infrastructure: tests must not depend on prod T3 content as a
  substrate-provided service.** During 4.33.0 release prep (2026-05-20),
  `uv run pytest -m integration` surfaced 7 failures in
  `test_hybrid_plan_factual_qa.py` (5 fixtures) and
  `tests/integration/test_rdr_093_groupby_aggregate_pipelines.py`
  (2 tests) because their `TARGET_COLLECTION` constants pointed at
  `knowledge__hybridrag` and `knowledge__delos` — production corpora
  rotated or removed before the test rerun. The same failures
  reproduced on `v4.32.14`, so the regression rode through the prior
  release; nobody had re-run `-m integration` before tagging.
  **Status**: Verified (High confidence). **Method**: Source Search
  (`grep TARGET_COLLECTION`, `nx collection list`) + counterfactual
  against `v4.32.14`. **Evidence**: T2 entry `120-research-A8`.
  **Implication for the substrate design**: the daemon provides
  storage shape (single-writer arbiter, version negotiation,
  discovery), not content. A test fixture that needs corpus X must
  seed corpus X deterministically at session setup; a fixture pinned
  to a corpus name that "the daemon should make available"
  reintroduces the silo problem that broke RDR-105's container case
  (each container assumes prod state present, each gets a different
  empty / stale view). Captured separately as bead `nexus-ntacy` for
  fixture-corpus standup work; this finding records the architectural
  lesson: substrate provides handle, consumer owns content.

### §A8-exempt substrate-owned writes (authoritative list)

Recorded 2026-05-21 from `nexus-q2t5t` following the A9 content audit
(`120-research-A9-content-audit`, T2 id=1404). This is the canonical
authority list: any future migration that would write rows beyond DDL
must either land on this table with explicit justification or be
rejected at gate-review time. The list is the symmetric companion to
the §A9 remediation beads (`nexus-rv7x6`, `nexus-6y2a9`, `nexus-yulol`):
the remediation beads move 10 substrate-violating migrations out to
consumer verbs; the exemption table below names the writes that stay
in the substrate and why.

| Site (file:line) | Class | Justification |
|---|---|---|
| `src/nexus/db/migrations.py:2900-2938` (`_bootstrap_version` `INSERT OR IGNORE` of the `cli_version` singleton into `_nexus_version`) | substrate-required, deterministic-from-package | `apply_pending` cannot operate without this singleton row. Value is derived from package metadata, not from corpus state, so daemon-startup writes are reproducible across hosts. Singleton + `INSERT OR IGNORE` makes repeated startup writes idempotent. |
| `migrate_document_aspects_pk_to_doc_id` PK-swap body proper (`src/nexus/db/migrations.py` v4.30.0; **excluding** the fixture-DELETE block at lines 1948-1954) | structurally-bound-to-schema | The new primary key cannot be populated without the catalog-JOIN backfill of `doc_id`; the backfill is structurally bound to the schema change in the same step. The fixture-DELETE block carved out separately under `nexus-yulol` is not exempt and ships as `nx aspects gc-fixtures`. |
| `migrate_aspect_extraction_queue_pk_to_doc_id` PK-swap body proper (companion to the above, same release) | structurally-bound-to-schema | Same justification class. The backfill is structurally bound to the PK change. |
| `migrate_hook_failures_chain_column` (`src/nexus/db/migrations.py` v4.14.2): `UPDATE` chain backfill bound to the `ADD COLUMN` step | structurally-bound-to-schema (marginal) | The chain-id backfill computes deterministically from existing row state at the same step that adds the column. Audited as a marginal case; kept exempt because the backfill cannot succeed at a later consumer-verb point: the new column would be NULL for the pre-existing rows and downstream consumers would have to defend against the NULL forever. Idempotent on re-run. |
| `migrate_drop_source_path_column` (`src/nexus/db/migrations.py` v4.31.0): pre-flight audit-only abort | structural-gate-no-mutation | The step reads to detect unmigrated rows and refuses to proceed if any exist; it never mutates content. Counted under §A8 as a structural safeguard rather than a content write. |

**Justification class glossary:**

- *substrate-required*: `apply_pending` cannot complete its own work
  without the write (typically a singleton bootstrap row).
- *deterministic-from-package*: the written value derives from package
  metadata or build artifacts, not from corpus / user / runtime state.
  Reproducible across hosts.
- *structurally-bound-to-schema*: the write is in the same step as a
  DDL change and the DDL cannot ship without the data movement (e.g.,
  populating a new PK column from a JOIN before the constraint is
  enforced).
- *structural-gate-no-mutation*: read-only audit pass that aborts the
  step if a precondition fails. Counted as substrate work because it
  guards substrate operations.

**Authoring rule for future migrations.** A migration that wants to
land a write beyond DDL must:

1. Justify the write against one of the four classes above and PR-
   open a patch to this table naming the site, the class, and the
   reason in the same diff that introduces the migration.
2. If no class fits, the write does not belong in `migrations.py`:
   the work moves to a consumer verb (`nx <area> <verb>`) and the
   migration ships DDL-only.

Storage-boundary lint (`nx doctor --check-storage-boundary`) does not
currently parse this table; enforcement is reviewer discipline at gate
time until tooling closes the gap.

## Proposed Solution

### Approach

Six phases (Phase 3 is split into 3a and 3b internally; see below).
Each phase ships as one branch, one PR, one cutover. Linear, not
parallel. The release of each phase is its own validation point.

**Validation regime (decision 2026-05-21, supersedes earlier
calendar-soak text):** each phase must satisfy two gates before the
next opens:

1. **Stress harness passes**: the per-phase scenario suite in
   `tests/stress/` runs to completion under a containerized daemon
   with all scenarios green. The harness covers concurrency storms,
   connection churn, daemon crash + supervisor respawn, spawn-lock
   contention, malformed input, slow / dead clients, process
   suspend/resume (sleep/wake analogue), memory profile, discovery
   file lifecycle, and HttpClient/HttpServer timeout invariants. CI
   runs the harness on every phase PR; merge is gated on green.
2. **Phase-review-gate cross-walk PASSED**: structural verification
   that every `§Approach` item for the phase has a closing bead (or
   is explicitly deferred) and no closing bead implements anything
   on the `§Out of scope` banlist.

That is the full list. The earlier intermediate amendment kept a
24-hour shakedown window after merge as "residual catch for what
the harness misses"; further reflection retired it. Same critique
as the original 7-day soak: passive observation only catches what
the operator happens to surface. If the harness covers the failure
class, the shakedown adds nothing; if it does not, calendar time
does not fill the gap. The honest move is to make the harness
cover the surface and gate purely on it. Operator-observed
regressions remain a normal `revert or fix forward` event; they
just are not formalised as a phase gate.

**P1 -> P2 soak-gate exception (decision 2026-05-21):** P1 collapses
into P2 (separate amendment). The P1 daemon was dormant by design;
no traffic, no soak value. The stress-harness regime above applies
from P2 onward.

**P3 sub-phase composition (gate critique fix-in-place, 2026-05-21):**
P3a and P3b are bisectability splits, not validation splits. P3b
may land immediately once the P3a harness passes; there is no
calendar interval between them.

**Phase 0: Lint + cutover flag scaffolding**

- Implement `nx doctor --check-storage-boundary`: AST scan of
  `sqlite3.connect` + `PersistentClient` outside an allowlisted prefix
  (`src/nexus/db/`). Modeled on
  `tests/test_no_direct_catalog_writes_outside_projector.py` (RDR-101
  Phase 3 ε-lint).
- Add `NX_STORAGE_MODE` env-var honor as a **no-op** (only `direct` is a
  valid value at this phase; `daemon` is rejected with "not yet
  supported").
- CI step runs the lint with `--fail-on-violation`. Initial run reports
  the existing 30+ direct-open sites; baseline is recorded as the
  allowlist for P1-P5 migration progress.
- **P0 lint also emits the catalog-allowlist count as a structlog
  metric** on each run, recorded to T2 at key
  `120-phase-0-catalog-allowlist-count` (baseline 2). The
  phase-boundary forcing function from P1 onward reads this baseline
  and asserts non-increase. See the catalog-allowlist non-increase
  paragraph after Phase 4 for the schema and per-phase keys.
- No client API changes. No daemon code yet.

**Phase 1: T3 daemon (managed `chroma run`)**

**Soak-gate exception (decision 2026-05-21):** the P1 soak marker
(originally `nexus-6ret6`) is REMOVED. P1 collapses into P2 once
the last P1 PR (#910 / #911) merges; P2.A may open immediately.
Rationale: P1 leaves `NX_STORAGE_MODE=direct` as the only valid
value and call sites do not flip, so the daemon is dormant for the
soak window. A dormant daemon does not surface the lifecycle bugs
(port collision, supervisor flap, sleep/wake races) the soak was
introduced to catch. The scope-discipline class that motivated the
soak gate in the RDR-110-113 postmortem was caught by the
phase-review-gate cross-walk and the 4-pass adversarial critic, not
by calendar exposure. Those gates ran on P1 already (gate PASSED
2026-05-21 / `nexus-ldztl` + cross-PR review on #905 / #906 / #907
caught 4 Significants folded as `cfaab35d`). The soak-gate rule
still applies at the P2 / P3a / P3b / P4 / P5 boundaries, where
real traffic hits the daemon and the soak buys runtime-bug
exposure.

- `nx daemon t3 {start,stop,status,install,uninstall}` CLI verbs.
- Daemon process: spawn-and-supervise `chroma run` with explicit port and
  data path. Loopback TCP only (chromadb upstream constraint).
- Discovery file: `~/.config/nexus/t3_addr.<uid>` containing the chroma
  HTTP address. Env override: `NX_T3_ADDR`. The C2 precedence rule
  applies to `T3Client` constructions: env var wins when set and
  non-empty; file is the fallback; fail-loud on unreachable env-var
  target. **Phasing note**: until P2 migrates the four direct
  `chromadb.PersistentClient` / `CloudClient` call sites in
  `src/nexus/health.py` to go through `T3Client`, those sites
  bypass C2 by construction (they are allowlisted A5 sites awaiting
  migration). C2 is enforced for all callers reaching T3 via
  `T3Client`, not for raw chromadb construction.
- `T3Client` factory: returns an `HttpClient`-backed wrapper whose API
  signature matches the current `T3Database` PersistentClient surface.
- T3 call sites do **not** flip yet. `NX_STORAGE_MODE=direct` remains the
  only valid value; daemon mode is exercised via direct construction in
  the daemon-mode E2E test.
- MVV variant: two `claude -p` sub-processes, both pointing at the
  running T3 daemon via `NX_T3_ADDR`, share a T3 collection round trip.

**Phase 2: T3 cutover**

- All T3 call sites use `T3Client`. The `PersistentClient` direct opens
  inside `src/nexus/db/t3.py` are deleted (or fenced behind
  daemon-internal access).
- `NX_STORAGE_MODE=daemon` becomes valid for T3 reads/writes.
- Full pytest + integration suite green under `NX_STORAGE_MODE=daemon`.
- Stress harness `tests/stress/test_t3_daemon_stress.py` green
  before P3 opens (per §Approach validation regime).

**Phase 3a: T2 daemon ships (transport only)**

- `nx daemon t2 {start,stop,status,install,uninstall}` CLI verbs.
- Daemon process: owns the seven domain-store SQLite handles (one per
  store path, all under the daemon's process). Binds both UDS
  (`~/.config/nexus/t2.sock`) and loopback TCP (announced via discovery
  file).
- Discovery: `~/.config/nexus/t2_addr.<uid>` carries both UDS path and
  TCP host:port. Env overrides: `NX_T2_SOCK`, `NX_T2_ADDR`. Precedence
  rule per §In scope: env var wins when set and non-empty; file is
  the fallback. Fail-loud on unreachable env-var target.
- `T2Client`: thin RPC client mirroring the existing `T2Database` facade.
  UDS preferred, TCP fallback when UDS unreachable.
- **Daemon owns migration on its own path**. On startup, checks schema
  version, applies pending, then accepts connections. Clients carry
  an expected-schema-version constant; handshake fails loud on
  mismatch. **Direct-open `T2Database` construction continues to call
  `apply_pending` per A6's P3 transition mitigation.**
- T2 call sites do not flip yet (`NX_STORAGE_MODE=direct` is still
  the default). Daemon mode exercised via the daemon E2E suite.
- **The P3 MVV runs during the P3a shakedown**: two `claude -p`
  sub-processes in different working dirs construct `T2Client`
  against the live daemon and share `memory_put` / `memory_get`
  (cross-process daemon-mediated state). The MVV validates
  client-traffic against the daemon; only the global call-site flip
  is deferred to P4.
- Validation: stress harness `tests/stress/test_t2_daemon_stress.py`
  green. P3b may land as soon as the P3a harness passes (per §Approach
  P3 sub-phase composition; no calendar interval).

**Phase 3b: Migration ownership transfer**

- `apply_pending` removed from `T2Database.__init__` and `T2Client`
  construction. Daemon is the sole `apply_pending` caller.
- `nexus-9eaz` cross-process stress test reframed as a daemon-startup
  invariant test ("daemon refuses second start against the same
  path"); previously skipped, now re-enabled under the new framing.
- May land within the Phase 3a soak window (no calendar gating) — the
  split is for bisectability, not cadence. A daemon bug surfacing
  after 3a is in transport; a migration bug surfacing after 3b is in
  ownership transfer.

**Phase 4: T2 cutover**

- All T2 call sites use `T2Client`. Direct `sqlite3.connect` outside
  `src/nexus/db/` daemon-internal becomes a hard lint violation (was
  allowlisted in P0; allowlist removed for all migrated sites).
- `NX_STORAGE_MODE=daemon` is the default for new installs; `direct` is
  retained as a debug fallback.
- Stress harness `tests/stress/test_t2_daemon_stress.py` green
  before P5 opens. Full pytest + integration suite green under
  `NX_STORAGE_MODE=daemon`.

**Phase-boundary forcing function: catalog-allowlist non-increase**
(gate critique fix-in-place, 2026-05-21). At each phase-review-gate
from P1 onward, the cross-walk reports the current count of direct
`sqlite3.connect` / `_sqlite3.connect` call sites in
`src/nexus/catalog/`. The count is monotonically non-increasing
across phases (baseline at P0: 2; target at P5: 0). Any increase is
a P5 risk that the phase cannot close without justification — either
the call site is genuinely required and P5 cutover must absorb the
additional removal, or the call site was added in error and must be
removed before phase close. The P0 lint emits the catalog-allowlist
count as a structlog metric on each run; the phase-review-gate
records the metric to T2 and asserts non-increase against the
previous phase's recorded value.

**Metric key schema:** keys are
`120-phase-<id>-catalog-allowlist-count` where `<id>` is the phase
designator (`0`, `1`, `2`, `3a`, `3b`, `4`, `5`). P3a writes
`120-phase-3a-catalog-allowlist-count`; P3b writes
`120-phase-3b-catalog-allowlist-count`. The non-increase assertion
compares P3a against P2's recorded value and P3b against P3a's
recorded value. P4 compares against P3b. P5 expects 0.

**Phase 5: Catalog collapse into T2**

- **Pre-P5 prerequisite (gate critique fix-in-place):** the A9
  content-seeding audit (P0 prerequisite, originally covering the
  seven T2 domain stores) MUST be re-run against CatalogDB and
  `catalog/synthesizer.py` before P5 opens. Any content seeding
  uncovered in the catalog code path follows the same A8 policy:
  either migrate to a consumer RDR (the rule owns its content) or
  demote to DDL-only (the consumer writes the seed on first use).
  Audit output recorded as `120-research-A9-catalog-extension`.
- `CatalogDB` (`src/nexus/catalog/catalog_db.py`) ports its schema and
  read/write surface into T2 as the eighth domain store. Catalog client
  goes through `T2Client`.
- Direct `sqlite3.connect` in `catalog_db.py:255` and `synthesizer.py:792`
  deleted.
- **Catalog-allowlist metric assertion at P5: `count == 0`
  explicitly** (not just "≤ P4's recorded value"). The monotonic-
  non-increase rule plus an explicit zero assertion at P5 catches
  the case where the count has decreased but a residual call site
  remains.
- Catalog test suite plus indexer-pipeline dogfood validates.

**Phase 6: Decommission `direct` mode**

- `NX_STORAGE_MODE` flag removed. Library-mode code paths deleted.
- Release. The substrate ships.
- **The moratorium lifts 30 days after P6 ships on `main`.** Any consumer
  RDR may then be filed.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| `nx daemon t3` | `chromadb` upstream `chroma run` | Wrap and supervise; no new HTTP code. |
| `nx daemon t2` | `nexus.db.t2.T2Database` (seven domain stores) | Extend: wrap existing facade in a socket server; do not rewrite store logic. |
| `T2Client` | `T2Database` facade | New: thin RPC client mirroring facade signature. |
| `T3Client` | `T3Database` PersistentClient surface | New: `HttpClient`-backed wrapper. |
| Discovery file | RDR-105's `~/.config/nexus/t1_addr.<claude_pid>` | Extend pattern; one address file per tier. |
| Storage-boundary lint | `tests/test_no_direct_catalog_writes_outside_projector.py` (RDR-101 ε-lint) | Extend pattern; new check function in `commands/doctor.py`. |
| Migration ownership | `nexus.db.migrations.apply_pending` | Move call site to daemon startup only; remove client-side `apply_pending` invocations. |
| `nx daemon t2 exec --raw` | (none) | New: read-only SQL pass-through over RPC. |
| Catalog into T2 (P5) | `nexus.catalog.catalog_db.CatalogDB` | Migrate schema; reuse store-internal patterns; delete `CatalogDB`. |

### Decision Rationale

**T3 ships first because the upstream gives us a free service.** `chroma
run` is a documented, vendored, production-recommended HTTP server. Our
P1 work is spawn-and-supervise + discovery + client wrapper. Zero novel
concurrency. Zero new SQLite semantics. Zero migration ownership
question. We prove the discovery mechanism, the cutover flag, and the
client-shim pattern against a substrate we don't own. Then T2 ships into
the same shape against a substrate where the concurrency story is ours.

**T2 second because the migration race is solved by construction once
the daemon owns the SQLite handle.** The `nexus-9eaz` `_upgrade_lock`
race that the previous attempt could not bottom is a problem only if
multiple processes can race on `apply_pending`. The daemon model makes
that impossible: one process, one connection, one migration runner. The
race class disappears; the instrumentation is unnecessary.

**Catalog third because it's the largest blast radius.** 22k lines
across the catalog package, the most heavily-used code in the project.
Migrating after T2 daemon is proven means catalog gets a substrate that's
been on `main` for ≥14 days under real usage before the catalog flip
begins.

**Serial PR chain, not parallel-agent fan-out.** Cross-contamination via
shared worktrees is one of the documented failure modes from the
previous attempt. Substrate commits land on one branch in order. Parallel
agents may only work on orthogonal consumers of the substrate, after the
substrate is merged.

**Cutover, not dual-mode forever.** `NX_STORAGE_MODE=direct` is a
migration safety valve, deleted in P6. The previous attempt's open-ended
dual-mode is what kept schema-blindness in the inline planner alive.

**No new primitives.** This is the moratorium. It is the only structural
difference between this attempt and the scrapped one. Without it, the
substrate's correctness becomes a moving target again.

## Alternatives Considered

### Alternative 1: Resurrect RDR-112 as the live design

**Description**: Restore RDR-112 from git history, amend its scope to drop
the RDR-110/111/113 consumer prerequisites, accept and implement against
that document.

**Pros**:
- Less writing upfront.
- Preserves the A1-A4 research findings as part of an accepted RDR.

**Cons**:
- RDR-112's prose is structurally entangled with the scrapped peers.
  Problem Statement, Decision Rationale, Approach §7 (event stream), §8
  (subspace registry), and the "Sequencing constraint vs RDR-110"
  subsection exist specifically to serve RDR-110's atomic-take and
  RDR-111's event projection. Stripping those leaves a different
  document.
- The new attempt's discipline (substrate-only scope, moratorium on
  co-shipped consumers) is the single most important property. It must
  be in the title and Problem Statement, not a §Revision History
  footnote.
- Phase 1/2 placeholders ("to be expanded during /conexus:create-plan") are
  exactly the softness that let scope creep in. The new RDR encodes
  P0-P6 phasing and the per-phase validation regime (stress harness +
  per-phase stress harness, per the 2026-05-21 amendments) as RDR-level
  commitments.

**Reason for rejection**: The most load-bearing change is the
*discipline*. A fresh document carries it visibly; an amended document
relegates it to a revision note.

### Alternative 2: Status quo, hope WAL holds across containers

**Description**: Keep opening T2 and T3 by path; rely on SQLite WAL and
chroma's filesystem locking.

**Cons**: Does not work across container boundaries; overlayfs blocks
WAL `mmap`. Silent fork-into-empty-DB persists. The container fragmentation
class of bugs (every "memory I saved isn't there" report) continues.

**Reason for rejection**: Does not solve the stated problem.

### Alternative 3: Single mega-daemon owning T1+T2+T3

**Cons**: T1 must stay per-session; collapsing it breaks RDR-105's
sibling-visibility model. T2 and T3 have very different scaling and
crash-isolation profiles; a chroma OOM should not take down memory.

**Reason for rejection**: Conflates tiers that RDR-105 deliberately
separated.

### Alternative 4: Cloud-only T3 (skip the T3 daemon, mandate `CloudClient`)

**Cons**: Reintroduces a network-required dependency for local
development; breaks air-gapped use cases; T3 cloud mode requires Voyage
API credentials.

**Reason for rejection**: Local mode is a first-class requirement.

### Briefly Rejected

- **Database-as-MCP-server only**: conflates the storage boundary with the
  tool boundary. The tool layer already exists; we want a layer below it.
- **Co-ship a minimal tuplespace primitive in P3**: this is the previous
  attempt's failure mode in miniature. Hard no.

## Trade-offs

### Consequences

- (+) Closes the cross-container silo class of bugs structurally.
- (+) Forces the discipline future tiers (T4? cross-host?) will need
  anyway.
- (+) Multi-user host isolation is free via UDS file permissions; no
  in-process auth code in v1.
- (+) Migration race class (`_upgrade_lock`) disappears by construction.
- (−) Two new long-lived processes per nexus install.
- (−) Adds an RPC hop to every T2/T3 call: +15 µs p50 (UDS) / +29 µs p50
  (TCP loopback) per A3 spike. Both sub-ms.
- (−) Daemon ↔ client version handshake is a new operational concern.
- (−) Containerised consumers require an orchestration step (UDS
  bind-mount or TCP port allow-list + env-var injection).
- (−) Ad-hoc DB access (`sqlite3 ~/.nexus/t2.db`, DBeaver, Datasette)
  disappears when nexus runs in its own container. `nx daemon t2 exec
  --raw <SQL>` is the supported replacement.
- (−) Six phases × stress harness runtime = ~minutes per phase
  after the substrate work itself completes (the original
  `≥7 days each = ~6 weeks` calendar burden was retired in the
  2026-05-21 stress-harness amendment, and the residual 24h
  shakedown window was retired in a follow-on amendment the same
  day; cadence is now bounded purely by harness runtime, not by
  calendar observation).

### Risks and Mitigations

- **Risk**: Daemon crash leaves clients hanging.
  **Mitigation**: Health-check + auto-respawn; clients fail loud with
  recovery instructions, no silent fallback to direct file. **No
  client-side retry budget within the substrate**: clients see the
  failure and surface it; retry orchestration is a consumer concern
  and belongs in RDR-115 (T2 daemon request tracing) territory, not
  in the substrate.
- **Risk**: Daemon killed (SIGTERM/SIGKILL) mid-`apply_pending` on
  first ever start.
  **Mitigation**: SQLite's WAL durability handles this by construction.
  An interrupted DDL transaction is rolled back on next open; partial
  schema state is not persisted. The daemon needs no signal-aware
  checkpointing for migration safety; idempotent migration steps
  (per A6's recast nexus-9eaz test) make resumption a no-op for
  already-applied steps.
- **Risk**: Auto-spawn races between concurrent first-use clients.
  **Mitigation**: Filesystem-lock-mediated spawn. The spawner calls
  `acquire_directory_lock` (see `src/nexus/_locking.py`) on
  `~/.config/nexus/` — flock on the directory fd on Unix, sentinel
  `.lock` file on Windows — before forking the daemon, releasing
  only after the discovery file is written. The discovery file
  itself does not exist when auto-spawn is needed, so the lock
  target must be the parent directory, not the discovery file.
  **Auto-spawn precondition (gate critique fix-in-place, 2026-05-21):**
  auto-spawn fires only when the env var (`NX_T<n>_SOCK` /
  `NX_T<n>_ADDR`) is UNSET and the discovery file does not exist.
  When the env var IS set but its target is unreachable, the client
  fails loud per the C2 precedence rule rather than attempting
  spawn. Operator-set env vars are an explicit override — an
  unreachable target points at operator misconfiguration, not at
  a missing daemon, and silent spawn would mask the misconfig.
- **Risk**: Sandbox containers with no host filesystem visibility.
  **Mitigation**: `NX_T2_SOCK` / `NX_T2_ADDR` / `NX_T3_ADDR` env-var
  injection as co-primary discovery; daemon binds both UDS and TCP and
  announces both on stdout for orchestrator capture.
- **Risk**: A consumer slips through the moratorium.
  **Mitigation**: § Scope Boundaries is part of the Finalization Gate;
  per-phase cross-walk at each phase closeout; a written rule reviewers
  can cite when rejecting drift.

### Failure Modes

- **Daemon down, client connects**: fail loud, suggest `nx daemon start`.
  Do not silently degrade.
- **Version mismatch**: client refuses to connect; report both versions.
- **Daemon healthy, data corrupt**: same as today (SQLite/chroma surface
  their own errors); daemon does not mask them.
- **Stress harness regression**: do not open the next phase; fix the
  regression or revert the phase commit.
- **Operator-observed regression after merge**: normal `revert or fix
  forward` event; not a phase gate (the 24h shakedown gate that used
  to formalise this was retired same-day as the harness amendment).

## Implementation Plan

### Prerequisites

- [x] RDR-112 tombstoned (this RDR's parent). Done 2026-05-19.
- [x] Postmortem available on `main`. Done 2026-05-19 (this PR).
- [ ] `/conexus:phase-review-gate` skill restored from
  `archive/develop-2026-05-19` (commit `122feaff`, bead `nexus-j327`)
  to main. Required for the per-phase cross-walk discipline encoded in
  A7's enforcement chain. Docs-and-tooling PR; ships before P0 opens.
- [ ] CI baseline: green on `main` at PR-open time. Tooling-level gate.
- [ ] Storage-boundary lint passes against the current main without
  daemon-internal allowlist (P0 baseline).
- [ ] **Daemon-init content-seeding audit** (gate critique fix-in-place,
  2026-05-21). Enumerate the seven T2 domain stores'
  initialization paths (`memory_store`, `plan_library`,
  `chash_index`, `catalog_taxonomy`, `aspect_extraction_queue`,
  `document_aspects`, `telemetry`) for content-seeding. Anything
  that inserts rows beyond DDL — default taxonomy entries,
  reserved-prefix registrations, telemetry-bootstrap seeds, etc.
  — violates A8 when it runs on daemon startup. Each instance is
  either (a) moved to a consumer RDR (the rule that seeds the
  content owns its own initialization, not the substrate) or (b)
  demoted to DDL-only (the consumer must write the seed itself on
  first use). Audit output recorded as T2
  `120-research-A9-content-audit`. Must complete before any daemon
  code lands.

### Minimum Viable Validation

A single end-to-end demo, per phase:

| Phase | MVV |
|---|---|
| P0 | Lint detects all 30+ direct-open sites; `direct` mode unchanged. |
| P1 | Two `claude -p` sub-processes pointing at `NX_T3_ADDR` share a T3 round trip. |
| P2 | Full pytest + integration green under `NX_STORAGE_MODE=daemon` for T3. |
| P3 | Two `claude -p` sub-processes in different working dirs share `memory_put` / `memory_get`. |
| P4 | Full pytest + integration green under `NX_STORAGE_MODE=daemon` for T2. |
| P5 | Catalog read/write round trip through the T2 daemon; indexer pipeline dogfood. |
| P6 | `NX_STORAGE_MODE` removed; full suite green. |

### Day 2 Operations

| Resource | List | Info | Stop | Verify | Backup |
|---|---|---|---|---|---|
| T2 daemon | `nx daemon list` | `nx daemon info t2` | `nx daemon stop t2` | health-check RPC | snapshot of underlying SQLite |
| T3 daemon | `nx daemon list` | `nx daemon info t3` | `nx daemon stop t3` | health-check RPC | snapshot of chroma dir |
| Discovery files | `ls ~/.config/nexus/*_addr.*` | `cat <file>` | rm on daemon stop | daemon startup writes | n/a |

`nx daemon t2 exec --raw <SQL>` is the read-only inspection surface. A
second `mode=ro` SQLite connection is opened by the daemon for `--raw`
execution; SQL pattern matching is brittle and not used. Write surfaces
(`exec --write`, `export`, `import`) are deferred to a follow-on RDR.

### New Dependencies

Probably none. Stdlib only for T2 transport; `chromadb` itself for T3
(already a dep).

**T2 transport decision (locked at P0, gate critique fix-in-place
2026-05-21):** the candidates were stdlib `socketserver` with a
hand-rolled JSON-on-the-wire framing, or
`multiprocessing.connection` with default pickle serialization.
**Pickle is forbidden.** UDS sockets on shared-user hosts are
reachable by any process whose UID can open the socket file; pickle
over that surface is an arbitrary-code-execution primitive. The
chosen transport is `socketserver` with length-prefixed JSON frames.
A T2 transport design note ships alongside the daemon scaffold at
P0 covering: length prefix, JSON envelope shape, error frame
schema, max frame size, timeout semantics, and **binary-column
encoding** (e.g. base64 for SQLite BLOB results from `sqlite_master`
or any future BLOB-typed column — JSON cannot natively represent
bytes). The seven T2 domain stores currently do not store binary
payloads (chash values are hex strings; embedding vectors live in
T3, not T2), so binary encoding is a forward-compatibility concern
rather than a Phase 3 hot-path issue.

## Test Plan

- **Scenario**: Two processes, distinct working dirs, same host:
  `memory_put` in one, `memory_get` in the other. **Verify**: value
  reads back. *(Original MVV, the proof-of-correctness.)*
- **Scenario**: Daemon killed mid-operation. **Verify**: client reports
  a clear error, no silent fallback to direct file.
- **Scenario**: Two clients race to auto-spawn. **Verify**: exactly one
  daemon runs; the loser connects to the winner.
- **Scenario**: Client built against daemon version N+1 connects to
  daemon version N. **Verify**: handshake refuses with clear version
  message.
- **Scenario**: Cross-*container* memory visibility (Claude Desktop ↔
  host `nx` CLI). **Verify**: visible.
- **Scenario**: Daemon restart preserves data and notifies clients.
  **Verify**: clients reconnect transparently or fail-loud per policy.
- **Scenario**: Storage-boundary lint against a known-bad fixture (a
  test file with a fresh `sqlite3.connect` outside `src/nexus/db/`).
  **Verify**: lint fails with `file:line` of the violation. *(A5
  verification.)*
- **Scenario**: Migration race regression test. Spawn two
  daemon-startup attempts in parallel against the same data path.
  **Verify**: exactly one applies the migration; the other refuses to
  start. *(A6 verification.)*
- **Scenario**: Scope-boundary cross-walk per phase. After each
  phase's PR merges, run a diff of merged code against § Scope
  Boundaries. **Verify**: no out-of-scope work landed. *(A7
  verification.)*
- **Scenario**: P3 transition window migration race. Start the T2
  daemon (which calls `apply_pending` at startup) and concurrently
  construct a direct-open `T2Database` (which also calls
  `apply_pending`). **Verify**: no migration collision; both paths
  complete successfully against the same schema; **post-test
  schema_version query returns the expected target version**
  (idempotency, not just non-error). *(A6 P3-transition verification,
  gate critique fix-in-place.)*
- **Scenario**: Discovery happy path (modal usage). Unset
  `NX_T2_ADDR`; the addr file points at a live daemon. **Verify**:
  client connects and completes a `memory_put` / `memory_get` round
  trip. *(C2 happy-path verification.)*
- **Scenario**: Discovery split-brain. Set `NX_T2_ADDR` to a live
  daemon and write `~/.config/nexus/t2_addr.<uid>` pointing at a
  different live daemon. **Verify**: client connects to the env-var
  target (precedence rule). *(C2 verification.)*
- **Scenario**: Discovery stale env var. Set `NX_T2_ADDR` to a dead
  address; the addr file points at a live daemon. **Verify**: client
  fails loud rather than silently falling through to the file.
  *(C2 fail-loud semantics.)*
- **Scenario**: Discovery stale file. Unset `NX_T2_ADDR`; the addr
  file points at a dead address. **Verify**: client fails loud with
  a useful message naming the file path. *(C2 fallback semantics.)*
- **Scenario**: Catalog-allowlist non-increase across phase
  boundaries. Run the storage-boundary lint with the allowlist count
  metric enabled at P0; record baseline. At each phase-review-gate
  from P1 onward, fetch the recorded T2 value
  `120-phase-N-catalog-allowlist-count` and assert the current count
  is ≤ the previous phase's count. **Verify**: monotonically
  non-increasing through P4; 0 at P5. *(S1 forcing function.)*

## Validation

### Testing Strategy

1. **Scenario**: cross-process memory visibility (MVV). **Expected**:
   visible.
2. **Scenario**: cross-*container* memory visibility (Claude Desktop ↔
   host `nx` CLI). **Expected**: visible.
3. **Scenario**: daemon restart preserves data and notifies clients.
   **Expected**: clients reconnect transparently or fail-loud per
   policy.

### Performance Expectations

RPC overhead measured in RDR-112 §A3 spike: direct in-process p50 = 85
µs; UDS p50 = 100 µs (+15 µs); TCP loopback p50 = 114 µs (+29 µs). All
sub-millisecond. T3 store ops dominated by embedding latency (ONNX
10-50 ms, Voyage 100-300 ms); transport hop below noise. Full data in
T2 `nexus_rdr/112-research-A3-spike`.

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

To be completed during gate.

### Assumption Verification

All four pre-existing assumptions (A1-A4) closed by RDR-112's gate; cited
by reference. Three new assumptions added: A5 (lint coverage), A6
(migration race elimination by construction), A7 (moratorium
enforceability). All must reach Verified status before acceptance.

### Scope Verification

The Minimum Viable Validation (two `claude -p` sub-processes on the same
host share memory via `memory_put` / `memory_get`) is in scope, not
deferred.

**§ Scope Boundaries is itself a gate item.** Gate must explicitly verify
that no out-of-scope work has been added to the proposed solution. This
gate item recurs at each phase closeout, not only at RDR acceptance.

### Cross-Cutting Concerns

- **Versioning**: daemon-client handshake required; mismatch fails loud.
- **Build tool compatibility**: no new build-time deps anticipated.
- **Licensing**: no new third-party libs anticipated.
- **Deployment model**: daemons run on host; clients run anywhere with a
  route to the host's UDS path or loopback TCP.
- **IDE compatibility**: N/A.
- **Incremental adoption**: `NX_STORAGE_MODE=direct|daemon`; `direct` is
  the default during P0-P5 and is deleted in P6.
- **Secret/credential lifecycle**: N/A in v1. Localhost-only, UDS uses
  unix file permissions, TCP loopback-only. Multi-user trust deferred to
  a post-substrate RDR.
- **Memory management**: daemon long-lived; needs a budget (set during
  planning based on expected concurrent client count and result-set
  sizes).

### Proportionality

Document covers a structural shift in how every persistent shared-state
store is accessed. Size justified. § Scope Boundaries is the
proportionality control: it bounds what this RDR commits to and what it
explicitly defers. The previous attempt's six-week scope creep is the
counterfactual.

## Open Questions

- **Authentication beyond v1**: localhost-only + UDS permissions covers
  single-user single-host. Multi-user and cross-host trust is deferred
  to a post-substrate RDR. Naming it explicitly here so the deferral is
  documented, not implicit.
- **Daemon-as-MCP-server**: should the T2 daemon eventually *be* the MCP
  server, collapsing two layers? Out of scope for this RDR; flagged for
  a follow-on.
- **Existing on-disk migration**: how do existing on-disk T2/T3 stores
  get picked up by the new daemons? Presumably "the daemon opens the
  same path the old direct client did," verified in P1 and P3.
- **What signals lifting the moratorium**: P6 + 30 days under real
  usage is the floor. Any other criteria? Open for the gate to settle.
- **Cross-RDR moratorium tooling**: should the conexus plugin gain a
  `moratorium_blocks: <topic-list>` frontmatter field on active RDRs
  plus a pre-scaffold check in `/conexus:rdr-create` and a cross-check in
  `/conexus:rdr-gate` Layer 3? Required before multi-author substrate work
  in any future RDR; dormant for RDR-120's solo-author implementation
  window. File as a follow-on bead against the conexus plugin if pursued.

## References

- Tombstoned RDR-112: [`docs/rdr/rdr-112-storage-as-service-container-boundary.md`](rdr-112-storage-as-service-container-boundary.md): substrate design source and A1-A4 research evidence.
- Postmortem: [`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`](../postmortem/2026-05-16-rdr110-113-remediation-chain.md): what failed and why.
- Tombstoned RDR-110/111/113/118/119: same directory: historical record
  of the consumer arc that was scrapped alongside RDR-112.
- RDR-105: T1 chroma architecture; live precedent for the
  host-service-plus-env-discovery pattern this RDR generalizes.
- RDR-108: Catalog/T3 identity model that P5 must preserve.

## Revision History

- 2026-05-19: Draft. Authored on `feature/rdr-120-storage-substrate-tombstones` immediately after tombstoning RDR-110/111/112/113/118/119.
- 2026-05-20: A8 added (substrate-vs-consumer applies to test fixtures) after v4.33.0 release prep surfaced corpus-fixture pinning failures.
- 2026-05-21: A5 inventory refresh against conexus 4.33.1 main. Counts unchanged; lint design holds.
- 2026-05-21: First gate run. BLOCKED with 2 Critical, 4 Significant, 5 Observations (T2 `120-gate-latest` id=1397). All findings folded in-place:
  - **C1** A6 invariant scope clarified to P4+; P3 transition mitigation documented; `nexus-9eaz` test re-armed for the P3 window.
  - **C2** Discovery precedence rule added (env var wins when set and non-empty; file is fallback; fail-loud on unreachable env-var target). Three test scenarios added.
  - **S1** Catalog-allowlist non-increase forcing function added to §Approach as a phase-boundary cross-walk requirement; T2 metric recorded per phase.
  - **S2** T2 transport pickle forbidden; `socketserver` + length-prefixed JSON locked as the P0 design decision.
  - **S3** P3 split into P3a (daemon ships, transport only) and P3b (migration ownership transfer); both share the soak window; the split is for bisectability.
  - **S4** Daemon-init content-seeding audit added as a P0 prerequisite; the seven T2 domain stores' initialization paths enumerated; output recorded as `120-research-A9-content-audit`.
  - **O4** Two moratorium entries added: schema migration helpers beyond `apply_pending`, and telemetry/breadcrumb instrumentation crossing the daemon boundary (RDR-115 is the right home).
- 2026-05-21: Second gate run after first fix-in-place. BLOCKED with 1 new Critical (C3: soak-duration contradiction from S3 split) + 5 Significant (S5-S9: seam issues from P3a/P3b split and C2 fail-loud semantic) + 4 Observations. All folded in this revision:
  - **C3** §Approach amended with the soak-duration exception (P3a ≥3 days minimum; combined P3 soak ≥7 days from P3a ship to P4 open).
  - **S5** P3 MVV explicitly placed in P3a's soak; only call-site flip is deferred to P4.
  - **S6** Missing happy-path discovery test scenario added (env unset + live file → success).
  - **S7** Migration-helpers moratorium bullet got an explicit exception for the daemon's own internal `apply_pending` coordination across the seven domain stores.
  - **S8** Auto-spawn precondition documented: fires only when env var is UNSET and discovery file does not exist; unreachable env-var target is operator misconfig → fail-loud, no spawn.
  - **S9** Metric key schema defined per phase including P3a / P3b sub-phases.
  - **O1** P3 transition test scenario adds schema-version idempotency assertion.
  - **O2** Phase 1 T3 daemon description restates C2 precedence rule for fail-loud semantic.
  - **O3** Transport design note covers binary-column encoding (base64 fallback for SQLite BLOB; forward-compatibility, not Phase 3 hot path).
  - **O4** Phase 0 description explicitly references the catalog-allowlist metric emission and the baseline recorded as `120-phase-0-catalog-allowlist-count`.
- 2026-05-21: Third gate pass (adversarial deep-pass framing, named suspect categories). BLOCKED with 1 new Critical (C4) + 2 Significant (S10, S11) + 4 Observations. All folded:
  - **C4** A6 P3 transition mitigation prose corrected. Previous text credited systemd ordering with blocking the cross-process race — true for systemd-managed clients but not for `claude -p` subprocesses outside the dependency graph. Revised: race is acknowledged and design is safe via SQLite `BEGIN EXCLUSIVE` + idempotent migration steps. nexus-9eaz test recast to verify convergence-to-expected-schema (idempotency), not prevention. Cross-process flock via `_locking.acquire_directory_lock` documented as a deferred option, not adopted now because SQLite already provides the safety guarantee that matters.
  - **S10** Auto-spawn mitigation cites `_locking.acquire_directory_lock` on `~/.config/nexus/` as the actual lock target. Wording corrected from "lock on the discovery file" (which doesn't exist when needed) to "lock on the parent directory."
  - **S11** P5 prerequisite added: re-run the A9 content-seeding audit against CatalogDB and `catalog/synthesizer.py` before P5 opens; output recorded as `120-research-A9-catalog-extension`. P5 catalog-allowlist metric assertion strengthened from monotonic non-increase to explicit `count == 0`.
  - **O5** §Risks adds explicit SIGTERM-during-apply_pending safety note (SQLite WAL durability handles it by construction).
  - **O6** §Risks adds "no substrate-side retry budget; retry orchestration is RDR-115 territory" to the daemon-crash mitigation.
  - **O7** P5 explicit `count == 0` assertion added (alongside the monotonic rule).
  - **O8** Phase 1 T3 description notes the phasing: C2 applies to `T3Client` callers; raw chromadb in `health.py` bypasses by construction until P2 migrates those four sites.
- 2026-05-21: Fourth gate pass. 0 Critical, 1 Significant. The single Significant was a precision error in pass-3's own fix-in-place: the C4 prose asserted `BEGIN EXCLUSIVE` as the cross-process safety mechanism, but the code uses `threading.RLock` (process-local) + step idempotency + SQLite statement-level write serialization. Safety story is real and sufficient; the cited mechanism was wrong. Folded:
  - **C4-correction** A6 P3 transition prose rewritten to cite the accurate safety story: SQLite statement-level write locking serializes DDL writers (loser gets SQLITE_BUSY and retries); step idempotency (every `_MIGRATIONS` entry uses `IF NOT EXISTS` / `PRAGMA table_info` / `INSERT OR IGNORE` guards) makes retries no-ops. `_upgrade_lock` is `threading.RLock`, process-local only, per the docstring at `migrations.py:2886-2894`. Step idempotency is the load-bearing cross-process invariant — a future non-idempotent migration step would silently break P3 safety; CI lint flagging unguarded DDL in migration functions is a deferred option.
  - **A5 inventory accuracy confirmed** by pass 4: the only `sqlite3.connect` sites in `src/nexus/catalog/` are `catalog_db.py:255` and `synthesizer.py:792`. `catalog_sync.py` imports `sqlite3` for exception classes only; not a connect site. P5 `count == 0` assertion is achievable as stated.
- 2026-05-21: P1 soak-gate exception. P1 -> P2 boundary soak removed; P1 collapses into P2 once the last P1 PR merges. Rationale: P1 leaves the daemon dormant (`NX_STORAGE_MODE=direct` only; no call sites flip), so the soak window does not exercise the daemon and cannot surface the lifecycle bugs it was designed to catch. The scope-discipline class motivating the soak in the RDR-110-113 postmortem was caught by the phase-review-gate cross-walk and the 4-pass adversarial critic, not by calendar exposure. Those gates already ran on P1 (gate PASSED via `nexus-ldztl` + cross-PR review folded as `cfaab35d`). Closes `nexus-6ret6`. Rule preserved at every later boundary (P2 / P3a / P3b / P4 / P5) where real traffic does exercise the daemon. §Approach Phase 1 block carries the per-phase note.
- 2026-05-21: Stress-harness validation regime (supersedes the per-phase `≥7 days under real usage` rule for all remaining phases). The calendar soak relied on operators happening to exercise the failure modes during the window; deterministic stress scenarios in a containerized harness catch the same bugs in minutes and add coverage for cases passive observation never reaches (concurrency storms, process kill recovery, spawn-lock contention, malformed-input edge cases, suspend/resume sleep-wake analogue, memory profile over insert/delete cycles, discovery file lifecycle, HttpClient timeout invariants). Each phase ships a section of the harness covering its new code paths; merge gates on the harness passing in CI plus 24h of shakedown on `main`. P3 sub-phase composition simplified: P3b may land inside the 24h P3a shakedown window once the P3a harness passes; combined P3 shakedown clock starts at P3b ship. The `nexus-ow1ao` / `nexus-89pp7` / `nexus-4901f` / `nexus-qeywv` / `nexus-b5ezj` / `nexus-o3s8i` beads retain their gate role with the new acceptance (harness + 24h). §Approach intro carries the validation-regime paragraph; per-phase blocks updated to cite the harness file name. §Trade-offs cadence row updated. §Failure Modes regression entries split into stress-harness and shakedown columns.
- 2026-05-21: 24h shakedown retired (same-day follow-on amendment). Same critique as the original 7-day soak applied: passive operator observation only catches what the operator happens to surface; if the harness covers the failure class the shakedown adds nothing, and if it does not, calendar time does not fill the gap. Phase gates are now purely (1) stress harness passes + (2) phase-review-gate cross-walk PASSED. Operator-observed regressions remain a normal `revert or fix forward` event; they are not formalised as a phase gate. The six soak beads (`nexus-ow1ao` / `nexus-89pp7` / `nexus-4901f` / `nexus-qeywv` / `nexus-b5ezj` / `nexus-o3s8i`) become redundant: each phase already has a phase-review-gate bead that performs the cross-walk against the harness deliverable. The soak beads close as deprecated by this amendment; only the P6 moratorium-lift portion of `nexus-o3s8i` (the 30-day post-P6 window before consumer RDRs may file) is a separate governance concern and remains tracked. §Approach intro / per-phase blocks / §Trade-offs / §Failure Modes all updated.
