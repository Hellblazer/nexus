---
title: "One Engine, One Database, N Schemas: Retire the Fifteen-Client Transport Decomposition"
id: RDR-198
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self (solo)
created: 2026-08-23
accepted_date:
related_issues: [nexus-m20mf, RDR-063, RDR-164, RDR-193]
---

# RDR-198: One Engine, One Database, N Schemas: Retire the Fifteen-Client Transport Decomposition

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

The nexus client talks to **one** engine process, which owns **one** Postgres
database containing **three** schemas (`nexus`, `staging`, `t1`). It talks to
that single engine through **fifteen separate HTTP client classes**, most of
which construct their own `httpx.Client` — their own connection pool, their own
TLS context, their own credential resolution.

This is not a design. It is a fossil. Each of those clients replaced a SQLite
store that genuinely was a separate database file, back when the storage tier
really was many independent things. RDR-152 moved the substrate to Postgres and
RDR-158 deleted the SQLite backends, but the *shape* those backends justified
was never revisited. The evidence is written in the code: nearly every store in
`src/nexus/db/t2/__init__.py` carries a comment of the form *"COLLAPSED in
nexus-i711w Stage 2 sub-stage A: HttpMemoryStore is the only memory store — the
SQLite MemoryStore it used to select is deleted."* The alternative arm was
removed; the fan-out it existed to select between was kept.

The consequence is that a **domain** decomposition — memory, plans, taxonomy,
telemetry, aspects, catalog, vectors, scratch — has been mirrored one-to-one
into a **transport** decomposition. Domain separation is good and should stay.
Fifteen transports to one server is not separation; it is the same connection
fifteen times with connection pooling defeated.

This RDR proposes to collapse the transport layer to one client and to express
each logical operation as one engine call, without disturbing the domain
boundaries that make the code readable.

### Enumerated gaps to close

#### Gap 1: Fifteen client abstractions front a single engine

Fifteen classes (`HttpMemoryStore`, `HttpPlanLibrary`, `HttpTaxonomyStore`,
`HttpTelemetryStore`, `HttpChashIndex`, `HttpDocumentAspectsStore`,
`HttpAspectQueue`, `HttpDocumentHighlightsStore`, `HttpCentroidStore`,
`HttpTokenStore`, `HttpCatalogClient`, `HttpScratchStore`, `HttpVectorClient`,
`HttpPipelineDB`, `HttpLadderStore`) each resolve the endpoint, each fetch a
credential, and most construct their own `httpx.Client`.

Today one `nx_answer` call enters five `with _t2_ctx()` blocks; each builds a
fresh `T2Database` with eight stores, so a single call performs forty store
constructions. Keep-alive cannot survive a context boundary, so connections are
re-established repeatedly against a remote engine even though every one of them
targets the same host.

**The fifteen are not one shape** (Verified, research pass 1). They fall into
three groups with materially different mechanics, and an earlier draft of this
RDR treated them as uniform:

| Group | Count | Mechanism | Shareable? |
| --- | --- | --- | --- |
| 1 | 12 | `RefreshableHttpStoreMixin` — auth headers built fresh per call (`_auth_headers`); the `httpx.Client` carries no headers or base_url and is never rebuilt on refresh | **Yes** — safe today |
| 2 | 2 | `HttpTokenStore`, `HttpScratchStore` — bake `Authorization` into the `httpx.Client` **constructor** and rebuild the client object on refresh (`http_scratch_store.py:291,333,401`) | **No** — naive sharing bleeds one domain's header into another |
| 3 | 1 | `HttpVectorClient` — built on **urllib**, not `httpx.Client` at all (`http_vector_client.py:422`) | Inapplicable |

`base_url` is identical across all fifteen (all resolve through the same
`resolve_service_endpoint()`), so there is no per-store endpoint divergence to
preserve.

What the fix delivers: one shared, pooled transport for Group 1 immediately;
Group 2 converted to per-call auth *first*, because sharing them as they stand
is a correctness bug, not an optimisation; Group 3 addressed separately or left
alone, since it does not use the transport being consolidated.

#### Gap 2: One logical operation is not one transaction

Because the client orchestrates across facades, an operation that is one unit
of work server-side becomes several independent round trips client-side.

Recording an `nx_answer` run touches the telemetry and plans domains across
multiple contexts: `increment_run_started`, the run record, the per-step
records, `increment_run_outcome`. There is no transaction spanning them. A
crash or a network fault between calls leaves a partially recorded run, and
nothing reconciles it afterwards.

This is a **correctness** gap, not a performance one, and it is the strongest
reason to do this work. Server-side, that sequence is one `BEGIN … COMMIT`.

What the fix delivers: engine endpoints that express the operation, so partial
state is not representable.

#### Gap 3: The schema layout is path-dependent rather than designed

Three schemas exist — `nexus`, `staging`, `t1` — and 104 Liquibase changelog
files group under fifteen informal prefixes (`catalog` ×32, `taxonomy` ×14,
`vectors` ×7, `fk` ×7, `telemetry` ×6, `service-tokens` ×4, and so on). Those
prefixes are the real domain map, but they are a naming convention, not a
schema boundary. Whether a domain deserves its own schema, and what belongs in
`nexus` versus `staging` versus `t1`, has never been decided as a question —
each table landed wherever its originating RDR put it.

What the fix delivers: an explicit decision about which schemas exist and what
belongs in each, so the boundary is chosen rather than inherited.

#### Gap 4: No changeset-granularity policy for a migration this size

Postgres has transactional DDL: a schema change either fully applies or fully
rolls back, and Liquibase runs each changeset in a transaction by default
(**Verified** — `ChangeSet.java:425` and its javadoc, liquibase-core 4.29.0).

**CORRECTED 2026-08-23 (research pass 1).** An earlier draft of this gap
claimed three of 104 changelogs opt out of the transaction via
`runInTransaction="false"` for `CREATE INDEX CONCURRENTLY`. That is **wrong**.
There are **zero** actual `runInTransaction` attribute usages across all 104
changelogs, and **no changeset issues `CONCURRENTLY` at all**. The two apparent
matches are prose inside XML *comments* in `vectors-002` and `vectors-003`,
explaining why those files deliberately chose a plain `CREATE INDEX` in order to
*avoid* needing the opt-out; a third comment says that if `CONCURRENTLY` is ever
required, do it manually outside Liquibase. The original claim came from
misreading a grep that did not distinguish attribute from comment text.

So the real position is *better* than the draft asserted: **every changeset in
this repository runs inside a transaction today, deliberately, with the
reasoning written down.** Liquibase is not being used tactically. That framing
is withdrawn.

What actually remains is narrower, and it is a forward-looking policy question
rather than a defect: a consolidation of this size will want multi-object
changes that must land atomically, and there is no stated rule for when a step
may be split across changesets versus when it must not be. The mechanism for
that rule is **Verified to work** — all statements within one changeset share a
single transaction (one `autoCommit` set, one `commit()` after the full change
loop, one catch-all `rollback()` on failure), so "group atomically-required
changes into one changeset" is real rather than wishful.

What the fix delivers: a written changeset-granularity policy for this
migration, so each step is atomic by construction rather than by accident.

#### Gap 5: The consolidation must not erode the existing raw-SQL discipline

A gate already enforces generated-jOOQ-DSL discipline in the engine:
`service/src/test/java/dev/nexus/service/db/RawSqlGateTest.java`. It is
statement-granular, carries a sanctioned-exception registry, tracks raw-SQL
assembly sentinels and wrapper call sites, and includes meta-tests proving the
gate itself flags violations rather than passing vacuously.

Six raw-SQL sites remain in `service/src/main/java`. **Verified** against
jOOQ 3.20.11 sources (the version `service/pom.xml` pins), not inferred from our
own sanction registry:

| Construct | Site | jOOQ DSL form |
| --- | --- | --- |
| `ALTER TABLE … [NO] FORCE ROW LEVEL SECURITY` | `SchemaMigrator:239,248` | **Does not exist** (full `AlterTableStep` inventory + `PostgresDSL`) |
| `VACUUM (ANALYZE)` | `TenantScope:389` | **Does not exist** (zero hits in the jar) |
| `SET CONSTRAINTS … DEFERRED` | `CatalogRepository:6040` | **Does not exist** (session command) |
| `pg_advisory_xact_lock(hashtext(…))` | `CatalogRepository:5303` | **Exists but unsuitable** — `DSL.function(String, Class<T>, Field<?>…)` is a genuine typed generic function-call builder that could express it |

The fourth row is a correction to an earlier draft, which claimed no DSL form
existed for any of the six. For the advisory lock a form *does* exist; the
sanction rests on readability and precision, not impossibility. Saying so
matters, because "jOOQ cannot express this" and "we chose not to use what jOOQ
offers" are different claims and only one of them is true here.

The gap is not that discipline is missing. It is that a migration of this size
is exactly the pressure that produces "just this once" exceptions.

What the fix delivers: new engine endpoints written in generated jOOQ DSL, with
the existing gate as the enforcement mechanism and no new sanctioned entries
except where jOOQ demonstrably has no DSL form.

## Relationship to Prior RDRs

**This RDR is not a novel diagnosis. It is the third member of an established
family, and it must be read against the other two.**

### RDR-063 — the origin of the split (closed)

RDR-063 created the T2 domain decomposition, and its stated reasons are worth
quoting because they are now void:

> *"`memory` sees interactive writes… `relevance_log` sees automated writes…
> **They share a single SQLite file and a single `threading.Lock`.** A long
> `find_overlapping_memories` scan will block `relevance_log` inserts for the
> duration."*

Every clause of that rationale is about one SQLite file and one Python lock.
Neither exists now. The split was a correct answer to a question that stopped
being asked in RDR-152/158. **This is the path-dependence claim, sourced.**

Note carefully what does NOT follow: RDR-063's *domain* boundaries remain good
and are not under review here. What expired is the reason those boundaries had
to be separate **stores**.

### RDR-164 — the same diagnosis, already proven and shipped (closed)

RDR-164 reached this RDR's Gap 2 independently, for the collection- and
document-lifecycle domain, and shipped the fix. Its words:

> *"This shape is a **SQLite-era artifact**… there was no way to express
> 'deleting a collection purges all its derived state' as one transaction…
> That constraint is now largely gone."*

> *"Both are symptoms of the same disease: lifecycle integrity maintained by
> hand, in the client, across stores, non-atomically."*

It names two real bugs caused by that non-atomicity — `nexus-tquoj` (collection
delete never purged `aspect_extraction_queue`, so the aspect worker churned on
rows whose collection was gone) and `nexus-cugrk` (an orphan centroid kept
attracting chunks to a deleted topic).

**This materially raises the confidence of Gap 2.** The orphan-state bug class
is not hypothesised here; it is documented, with instances, and the atomic-
cascade remedy is already in production for one domain. RDR-198 argues the same
remedy generalises from lifecycle cascades to operations at large.

### RDR-193 — the adjacent draft (draft, UNMERGED)

RDR-193 moves index-time catalog reconcile and taxonomy compute onto the engine
as transactional SQL and Java jobs. Its Part A introduces
`nexus.catalog_reconcile_commit(...)` — a plpgsql function performing the whole
reconcile in ONE transaction.

That is precisely this RDR's Layer 2 pattern, designed in detail for one
pipeline. **RDR-198 must not re-derive it and must not duplicate its scope.**

Scope boundary, explicit:

| Concern | Owner |
| --- | --- |
| Index-time catalog reconcile, housekeeping, linking | RDR-193 Part A |
| Taxonomy discover / clustering on the engine | RDR-193 Part B |
| Collection & document lifecycle cascades | RDR-164 (shipped) |
| **HTTP transport collapse (fifteen clients → one pool)** | **RDR-198 Layer 1** |
| **Operation endpoints for everything not owned above** | **RDR-198 Layer 2** |
| **Schema boundary decision (`nexus` / `staging` / `t1`)** | **RDR-198 Layer 3** |

Two consequences for sequencing. RDR-193 is a *draft*: if it is accepted and
implemented first, its `catalog_reconcile_commit` becomes the reference
implementation for Layer 2 and this RDR should follow its conventions rather
than invent parallel ones. If RDR-198 Layer 1 lands first, RDR-193 inherits a
shared transport for free. **Layer 1 and RDR-193 do not conflict and can
proceed in parallel; Layer 2 should not start until RDR-193's fate is decided.**

### The family this belongs to

RDR-154 and RDR-156 established the "lean on Postgres" line — put integrity and
computation in the database that can express them. RDR-164 applied it to
lifecycle. RDR-193 proposes applying it to indexing. RDR-198 applies it to the
transport layer and to operation atomicity generally, and proposes finishing the
job by deciding the schema boundary that all three have so far worked around.

## Context

### Background

The origin is bead `nexus-m20mf`, filed 2026-08-22 after a latency
investigation into `nx_answer`. That investigation measured forty `httpx.Client`
constructions per call and attributed roughly 90% of the call's cost to them.

An interim fix shipped separately (commit `888bdee8f`): a shared module-level
`ssl.SSLContext`, so the CA bundle is parsed once instead of forty times. Its
own commit message names itself a band-aid — *"it makes 40 clients cheaper
without questioning why there are 40."* A second interim fix landed
2026-08-23 (`602940b35`): caching the `config.yml` parse behind
`get_credential`, which every store construction calls.

**The 90% figure must not be used to scope this work.** It is accurate for the
harness it was taken from and misleading for production. See Key Discoveries.

### Technical Environment

- **Engine**: one Java service under `service/`, `dev.nexus.service`, shipping
  on its own `engine-service-vX.Y.Z` release lifecycle, paired to the client by
  `REQUIRED_ENGINE_VERSION`.
- **Database**: one Postgres instance. Schemas `nexus`, `staging`, `t1`.
  pgvector for T3 chunk vectors (RDR-155), unified into a single `nexus.chunks`
  table by RDR-191.
- **Schema migration**: Liquibase, 104 changelog files under
  `service/src/main/resources/db/changelog/`.
- **Data access**: jOOQ, generated DSL, gated by `RawSqlGateTest`.
- **Client**: Python, `httpx`, fifteen HTTP store classes; most inherit
  `RefreshableHttpStoreMixin` from `src/nexus/db/t2/_refreshable_client.py`.
- **Deployment**: local (embedded PG) and managed cloud
  (`api.conexus-nexus.com`) modes from the same client.

## Research Findings

### Investigation

Performed 2026-08-23 against `develop` at `a5d7d73fe`. Symbol-level structure
was read with Serena (`get_symbols_overview`, `find_symbol`); file inventories
and changelog counts with targeted shell queries. Latency figures were measured
on this machine, not inherited.

- `T2Database.__init__` (`src/nexus/db/t2/__init__.py`) read in full — the
  eight-store construction and its `COLLAPSED` provenance comments.
- All fifteen `class Http*` declarations enumerated across `src/nexus/`.
- `RawSqlGateTest` structure read via Serena symbol overview.
- Raw-SQL call sites enumerated in `service/src/main/java`.
- Liquibase changelog inventory and `runInTransaction` / `CONCURRENTLY` usage.
- `get_credential` and `resolve_service_endpoint` timed directly.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| httpx 0.28.1 | Yes (via `888bdee8f` review) | `Client.__init__` builds an `ssl.SSLContext` eagerly and unconditionally, before the scheme is known. One context per client is the library's own internal model for a pool. |
| jOOQ 3.20.11 | **Yes — sources** | Three of four sanctioned constructs have no typed DSL form; `pg_advisory_xact_lock` **does**, via `DSL.function(String, Class<T>, Field<?>…)`. Corrects an earlier "none of them" claim. |
| Liquibase 4.29.0 | **Yes — sources** | Per-changeset transaction is the default (`ChangeSet.java:425`). All statements in ONE changeset share ONE transaction. **Correction**: this repo has ZERO `runInTransaction` opt-outs and issues no `CONCURRENTLY` — the earlier "three opt-outs" reading was comment text, not attributes. |
| Postgres | Documented | Transactional DDL; `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. |

### Key Discoveries

- **Verified — the fan-out is fifteen client classes, not eight.** The eight in
  the `T2Database` facade are the visible subset. Catalog, scratch (T1), vector
  (T3), pipeline, ladder, centroid and token stores are additional, each
  resolving the same engine independently.

- **Verified — the shape is path-dependent, and the code says so.** Every store
  in the T2 facade carries a `COLLAPSED in nexus-i711w` comment recording that
  its SQLite alternative was deleted. The selector died; the fan-out it selected
  between did not.

- **Verified — the "90% of steady-state cost" figure does not describe a real
  call.** `nexus-m20mf` states it plainly: *"99.4% of that call's **mocked-I/O**
  wall time is T2Database construction."* Measured on this machine after the
  two interim fixes: `resolve_service_endpoint` 0.482 ms/call (~19 ms ×40),
  `get_credential` 0.256 ms → 0.009 ms after caching, `httpx.Client`
  construction ~39 ms for 40 after the SSL fix. Total remaining construction
  overhead ≈ **60 ms**. A real `nx_answer` has a measured p50 of **80 seconds**
  (n=142, T2 `nexus/nx-answer-capability-analysis-2026-08-19`). The fan-out is
  therefore ≈ **0.07%** of a real call.

  **This matters for scoping.** Anyone justifying this work on latency will be
  optimising 0.07%. The justification is shape and atomicity.

- **Verified — the jOOQ discipline already exists and is non-vacuous.**
  `RawSqlGateTest` carries `SANCTIONED_STATEMENTS`, `INLINE_NONLITERAL_SANCTIONED`,
  `RAW_SQL_ASSEMBLY_SENTINELS`, `RAW_SQL_WRAPPER_METHODS`, and meta-tests
  (`*_isFlagged`) proving it catches what it claims to catch. Six raw sites
  remain, all no-DSL-form constructs.

- **Documented — three schemas, fifteen changelog prefixes.** The prefixes are
  the de-facto domain map; the schemas are not aligned to them.

- **Verified (REFUTED) — the atomicity gap has NOT manifested in production.**
  The `nx_answer` run-recording sequence has no spanning transaction, so a fault
  mid-sequence *should* leave partial state. Research pass 1 spiked it: partial
  state is arithmetically observable as `use_count > success + failure`, and
  across all five plans with `use_count > 0` (29 runs) the counts balance
  exactly — **zero orphaned starts**. `failure_count` is 0 everywhere, so the
  outcome path has never been exercised under stress either.

  The gap is therefore **latent, not observed**: the code path is genuinely
  non-atomic and nothing prevents the orphan, but it has not fired in the
  recorded window. Gap 2's argument rests on the mechanism plus RDR-164's
  proof of the same class in a sibling domain, NOT on an observation in this
  one. A reader should not take Gap 2 as reporting a live bug.

### Critical Assumptions

Discharged in research pass 1, 2026-08-23 (T2 `nexus_rdr/198-research-1`,
`198-research-jooq-liquibase`, `198-research-transport-isolation`).

- [x] A partial `nx_answer` run record is observable in production data —
  **Status**: **REFUTED** — **Method**: Spike. Zero orphaned starts across all
  five plans with `use_count > 0` (29 runs; `use_count == success + failure`
  everywhere). The mechanism is real — `increment_run_started` and
  `increment_run_outcome` sit in separate `_t2_ctx` blocks
  (`mcp_infra.py:391-392`), so nothing is atomic between them — but it has not
  fired in the recorded window, and `failure_count` is 0 everywhere, so the
  outcome path has never been exercised under stress. **Gap 2 is therefore a
  LATENT correctness gap, not an observed defect.** The class is nonetheless
  proven in a sibling domain by RDR-164 (nexus-tquoj, nexus-cugrk).

- [x] jOOQ has no typed DSL form for the four sanctioned construct classes —
  **Status**: **PARTIALLY VERIFIED** — **Method**: Source Search against
  jooq-3.20.11 sources. Three genuinely have no form; `pg_advisory_xact_lock`
  **does** have one via `DSL.function(String, Class<T>, Field<?>…)`. See the
  Gap 5 table.

- [x] Liquibase wraps each changeset in a transaction by default on Postgres —
  **Status**: **VERIFIED** — **Method**: Source Search (`ChangeSet.java:425`,
  liquibase-core 4.29.0), upgraded from Docs Only. Also verified: all statements
  in ONE changeset share ONE transaction, so Gap 4's proposed mechanism works.
  This pass also **corrected a factual error** in the draft — see Gap 4.

- [x] Collapsing to one shared client does not break credential refresh —
  **Status**: **PARTIALLY VERIFIED / REFUTED FOR 2 OF 15** — **Method**: Source
  Search. Safe for the 12 mixin stores; **unsafe as-is** for `HttpTokenStore`
  and `HttpScratchStore`, which bake auth into the client constructor and
  rebuild it on refresh. Layer 1 must convert those two before sharing, or scope
  the shared pool to Group 1. See the Gap 1 table.

- [x] No consumer depends on per-store connection isolation —
  **Status**: **PARTIALLY VERIFIED** — **Method**: Source Search. No streaming,
  SSE, cookies, or sticky routing anywhere. The aspect worker is a genuine OS
  subprocess, so an in-process singleton cannot reach it and Layer 1's scoping
  is safe. Timeouts DO differ (30s default vs 600s for the catalog combined
  write and vector upsert), but the long cases already use per-request
  `timeout=` overrides, which are stateless and survive sharing. **One real
  gap**: `HttpAspectQueue` sets its timeout as a constructor kwarg on the client
  default rather than per call, and that customization point disappears under a
  shared singleton — it must migrate to per-call overrides, as the catalog
  client already does.

**Net effect on the design**: Layer 1 is still sound but is no longer a single
uniform step. It has a required precursor (convert Group 2 to per-call auth,
migrate `HttpAspectQueue`'s timeout) that must land before any pool is shared.

## Proposed Solution

### Approach

Three separable layers, sequenced so each is shippable and reversible on its
own. **This RDR does not commit to the design of layers 2 and 3 in detail** —
it commits to the decomposition and to doing layer 1 first.

**Layer 0 — Make the stores shareable (client-only, REQUIRED PRECURSOR).**
Research pass 1 refuted the assumption that all fifteen can share a pool as they
stand. Two of them (`HttpTokenStore`, `HttpScratchStore`) bake `Authorization`
into the `httpx.Client` constructor and rebuild the client on refresh, so
sharing them naively bleeds one domain's credential into another — a
correctness bug, not a slow path. Convert both to the per-call `_auth_headers`
pattern the other twelve already use, and migrate `HttpAspectQueue`'s
constructor-time timeout kwarg to a per-call `timeout=` override (the catalog
client already does this). None of that requires a shared pool; it is
independently correct and independently shippable.

**Layer 1 — One transport, typed facades (client-only).**
Introduce a single process-wide pooled HTTP transport for the twelve mixin
stores plus the two converted in Layer 0. `HttpVectorClient` is out of scope: it
is built on urllib, not `httpx.Client`, so it shares nothing to begin with. The
facades stay exactly as they are from the caller's point of view; they stop
owning connections. `mcp_infra.t2_index_write` already runs against a refcounted
process singleton and returns its callable's value, so it serves reads as well
as writes; `t2_ctx`'s own docstring already lists these call sites as "not yet
converted". The catalog tier already has a shared handle. **T2 is the outlier,
not the pattern.** No engine change, no release lifecycle, no wire-contract
change.

**Layer 2 — Operations as engine endpoints (engine + client).**
For each operation that currently fans out, add one engine endpoint that
performs it as one transaction, and reduce the client to one call. Start with
`nx_answer` run recording, which is the clearest case and the one with the
atomicity gap. Written in generated jOOQ DSL under the existing gate.

**Layer 3 — Schema boundary decision (engine + migration).**
Decide which schemas exist and what belongs in each, then move what needs
moving using transactional DDL, with a stated changeset-granularity policy.
This is last because it is the least reversible and because layers 1 and 2
reduce the number of callers that care.

### Technical Design

Interfaces only; signatures verified at implementation.

**Layer 1 transport seam.** A module-level accessor returning a shared
`httpx.Client`, refcounted like the existing singleton, with the per-store
credential and endpoint resolution kept per-request rather than per-client so
token refresh semantics are unchanged.

```text
// Illustrative — verify against RefreshableHttpStoreMixin during implementation
shared_transport() -> httpx.Client          # process-wide, refcounted
StoreFacade._request(method, path, **kw)    # uses shared_transport(); resolves
                                            # credential per call, not per client
```

**Layer 2 endpoint contract.** One endpoint per operation, not per table. The
request carries everything the operation needs; the engine performs it in one
transaction and returns the operation's result.

```text
POST /v1/answer-runs            # replaces increment_run_started + run record
                                # + step records + increment_run_outcome
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Shared pooled transport | `src/nexus/db/t2/_refreshable_client.py` | **Extend** — it already holds the shared `ssl.SSLContext` from `888bdee8f`; the pool is the next thing to share in the same place. |
| Process-singleton access | `mcp_infra.t2_index_write` | **Reuse** — refcounted singleton exists and already returns values, so it serves reads. Its docstring already names these call sites as unconverted. |
| Raw-SQL enforcement | `RawSqlGateTest` | **Reuse unchanged** — do not extend, do not add sanctions except for demonstrable no-DSL-form constructs. |
| Config parse caching | `nexus.config._load_global_config` | **Reuse** — landed `602940b35`, mtime-keyed. |
| Schema migration | Liquibase changelogs | **Extend** — add a changeset-granularity policy; do not replace the tool. |

### Decision Rationale

The alternative shapes were rejected for reasons that survive the measurement
correction. Collapsing the domain facades themselves would trade a real
readability asset for nothing — the eight-way domain split is good code
organisation and is not what is broken. Leaving the fan-out and optimising
around it is what the two interim fixes already did; they were correct as
interim measures and neither addresses partial-state.

Sequencing layer 1 first is a logistics decision: it is client-only, needs no
engine tag, and removes the pressure that would otherwise tempt someone to rush
layer 2 onto a release lifecycle it is not ready for.

## Alternatives Considered

### Alternative 1: Leave the fan-out; keep optimising construction cost

**Description**: Continue the `888bdee8f` / `602940b35` approach — make each of
the fifteen clients cheaper to build.

**Pros**:
- Zero risk, no wire-contract change, already partially done.

**Cons**:
- Does not address partial-state, which is the correctness gap.
- Each fix must be rediscovered per construct.

**Reason for rejection**: It optimises a shape rather than fixing it, and the
remaining headroom is ~60 ms on an 80-second call.

### Alternative 2: Collapse the domain facades into one client class

**Description**: Replace the fifteen classes with a single `EngineClient`.

**Pros**:
- Fewest moving parts.

**Cons**:
- Destroys a domain decomposition that is genuinely useful.
- Enormous call-site churn for no correctness gain.

**Reason for rejection**: The domain split is not the defect. The transport
mirroring of it is.

### Briefly Rejected

- **Per-domain databases**: contradicts the premise; we have one engine and one
  database, and the point is to stop pretending otherwise.
- **Rewrite without Liquibase**: the tool is not the problem; its tactical use is.
- **Bridge/compat layer for the old client shape**: explicitly out of scope —
  this project evolves forward and does not bridge to its own past.

## Trade-offs

### Consequences

- Connection reuse becomes possible across context boundaries, which matters
  most against a remote managed engine.
- Partial-state on operation faults stops being representable (layer 2 only).
- A shared pool means one saturated domain can starve another; pool sizing
  becomes a real parameter rather than an accident of having fifteen pools.
- Layer 3 touches schema boundaries and is the least reversible step.

### Risks and Mitigations

- **Risk**: A shared transport leaks credentials or tenant scope across domains.
  **Mitigation**: Resolve credentials per request, never per client; add a test
  asserting two domains with different tokens do not observe each other's.

- **Risk**: Layer 2 ships a client half against an engine that does not serve
  the endpoint, leaving it inert.
  **Mitigation**: Engine-first sequencing with `REQUIRED_ENGINE_VERSION`, per
  AGENTS.md. Non-negotiable.

- **Risk**: The migration's size invites raw-SQL exceptions.
  **Mitigation**: `RawSqlGateTest` stays as-is; new sanctions require a written
  justification that jOOQ has no DSL form.

- **Risk**: Scoping from the 90% figure produces effort out of proportion to
  benefit.
  **Mitigation**: This RDR records the corrected measurement in Key Discoveries;
  the bead carries the same correction as a comment.

### Failure Modes

- Layer 1 lands and layer 2 never does, leaving the atomicity gap open while the
  latency symptom is gone — the pressure to finish disappears with the symptom.
  **Detection**: the Critical Assumptions spike for orphan run rows should be
  run *before* layer 1, so the gap is documented independently of the symptom.
- A schema move in layer 3 partially applies. **Detection**: transactional DDL
  makes this structurally impossible per changeset; the risk is a multi-changeset
  step that should have been one changeset. That is what Gap 4's policy exists
  to prevent.

## Sequencing and Logistics

1. ~~**Spike the atomicity assumption first.**~~ **DONE, and it came back
   REFUTED** (research pass 1). Zero orphaned starts in 29 runs. The original
   rationale for sequencing it first — "Layer 1 removes the symptom and the
   pressure leaves with it" — is now void, because there is no symptom. Gap 2
   stands as a latent gap on a proven-elsewhere class, not an observed one, and
   the sequencing argument rests on Layer 0/1 being cheap and reversible instead.
2. **Layer 0** (client-only, REQUIRED before any sharing): convert
   `HttpTokenStore` and `HttpScratchStore` to per-call auth; migrate
   `HttpAspectQueue`'s timeout to a per-call override. Independently correct,
   independently shippable, and the thing that makes Layer 1 safe.
3. **Layer 1** (client-only): shared transport for the fourteen httpx stores,
   facades unchanged. Ships on the normal client cadence.
4. **Layer 2, first endpoint only**: `nx_answer` run recording. Engine-first,
   then client, paired by `REQUIRED_ENGINE_VERSION`.
5. **Census before widening layer 2.** Which operations genuinely fan out, and
   how many round trips each costs. Pick the next one or two on evidence.
6. **Layer 3 last.** Schema decision, then transactional moves under the Gap 4
   policy.

**Delivery constraint, non-negotiable**: engine endpoints are Java under
`service/` and ship on the `engine-service-vX.Y.Z` lifecycle with its own
pre-tag battery. A client half calling an endpoint no deployed engine serves is
inert at best. Sequence engine-first.

## Open Questions

- Should `t1` remain a separate schema, or is it a table group within `nexus`?
- Does `staging` survive layer 3, or is it an artefact of the migration era?
- Which of the fifteen facades have callers outside `nx_answer` that would
  benefit more from layer 2 than `nx_answer` does? The indexer constructs stores
  **per file**, so it pays this far more than `nx_answer` does — it may be the
  better first target for a latency argument, if one is wanted at all.
