---
title: "Collapse the Duplicated Client Transport: One Pooled Connection to One Engine"
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

# RDR-198: Collapse the Duplicated Client Transport: One Pooled Connection to One Engine

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

The nexus client talks to **one** engine process over **fifteen** HTTP client
classes. Fourteen of them build their own `httpx.Client` — their own connection
pool, their own TLS context — and all fourteen resolve to the identical
`base_url`. The fifteenth is built on urllib and shares nothing to begin with.

**Scope of this RDR, deliberately narrow.** This document covers the CLIENT
TRANSPORT only: making the duplicated connection layer one pooled transport,
and fixing what research found blocking that. It does **not** propose engine
endpoints, operation-level atomicity, or a schema-boundary decision. An earlier
draft did; research pass 1 removed the evidence for that scope (see
"Withdrawn scope" below), and the remaining case did not carry it.

The shape is path-dependent. Each client replaced a SQLite store that genuinely
was a separate database file. RDR-152 moved the substrate to Postgres and
RDR-158 deleted the SQLite backends, but the fan-out those backends justified
was never revisited. The code records this: nearly every store in
`src/nexus/db/t2/__init__.py` carries a comment of the form *"COLLAPSED in
nexus-i711w Stage 2 sub-stage A: HttpMemoryStore is the only memory store — the
SQLite MemoryStore it used to select is deleted."* The alternative arm was
removed; the fan-out it existed to select between was kept.

**Be clear about the size of the claim.** This is a code-shape and modest-
efficiency change, not an urgent defect. Measured overhead is ~60 ms against an
`nx_answer` whose p50 is 80 seconds — 0.07%. The honest justification is that
fourteen transports to one host is the wrong shape, that keep-alive cannot
survive a context boundary, and that BULK callers pay this per unit of work
rather than once per call. "Do nothing" is a serious alternative and is
analysed as one.

### Enumerated gaps to close

#### Gap 1: Fourteen httpx clients resolve to one host

Fourteen classes each construct an `httpx.Client` against the identical
`base_url` (all resolve through the same `resolve_service_endpoint()`;
**Verified**). One `nx_answer` call enters five `with _t2_ctx()` blocks, each
building a fresh `T2Database` with eight stores, so a single call performs forty
store constructions. Connection reuse cannot survive a context boundary, so
connections are re-established repeatedly against a remote engine.

Measured cost after the two interim fixes (`888bdee8f` shared SSL context,
`602940b35` config-parse cache): ~39 ms of client construction plus ~19 ms of
endpoint resolution per call. On an 80-second call that is noise. On the
INDEXER, which constructs stores per file, it is paid per file — that is where a
measurable benefit would be, and it has not yet been measured there.

What the fix delivers: one shared pooled transport, with the domain facades kept
as thin typed views over it.

#### Gap 2: Two stores cannot be shared as they stand

**Verified, research pass 1.** The fifteen are not one shape. Twelve use
`RefreshableHttpStoreMixin` and build auth headers fresh per call
(`_auth_headers`); their `httpx.Client` carries no headers or base_url and is
never rebuilt on refresh — safe to share today.

Two do not. `HttpTokenStore` and `HttpScratchStore` bake `Authorization` into
the `httpx.Client` **constructor** and rebuild the client object on token
refresh (`http_scratch_store.py:291,333,401`). Sharing a client across them
naively would send one domain's credential on another domain's request.

This is **not a live bug** — today each owns its own client, so nothing bleeds.
It is a precondition: the shared pool is unsafe until these two are converted.
Stated plainly because an earlier draft assumed all fifteen were uniform, and
that assumption was refuted.

What the fix delivers: both converted to the per-call auth pattern the other
twelve already use — independently correct, independently shippable, and
required before any pool is shared.

#### Gap 3: One customization point disappears under a shared pool

**Verified, research pass 1.** No consumer depends on per-store connection
isolation: no streaming, SSE, cookies, or sticky routing anywhere, and the
aspect worker runs as a genuine OS subprocess that an in-process singleton
cannot reach.

Timeouts do differ (30 s default; 600 s for the catalog combined-write embed
path and vector upsert), but the long paths already pass a per-request
`timeout=` override, which is stateless and survives sharing. One exception:
`HttpAspectQueue` sets its timeout as a **constructor kwarg on the client
default** rather than per call. That customization point vanishes under a shared
singleton.

What the fix delivers: `HttpAspectQueue` migrated to per-call `timeout=`
overrides, as the catalog client already does.

### Withdrawn scope

An earlier draft of this RDR also proposed engine endpoints for operation
atomicity (its Gap 2) and a schema-boundary decision (its Gap 3), and framed
Liquibase usage as tactical (its Gap 4). Research pass 1 and the Layer 3 gate
critique removed the support for all three:

- **Operation atomicity**: the mechanism is real (`increment_run_started` and
  `increment_run_outcome` sit in separate `_t2_ctx` blocks,
  `mcp_infra.py:391-392`), but a spike found **zero** orphaned records across
  all five plans with `use_count > 0` — 29 runs, `use_count == success +
  failure` everywhere. Latent, not observed. It remains a genuine gap and
  belongs in its own RDR, argued on its own evidence, sequenced against RDR-193.
- **Liquibase**: the claim that three changelogs opt out of transactional DDL
  was **false** — there are zero `runInTransaction` usages and no `CONCURRENTLY`
  anywhere. Every changeset already runs in a transaction, deliberately. Nothing
  to fix.
- **Schema boundary**: a real open question, but it is not a client-transport
  concern and had no evidence gathered for it.

## Relationship to Prior RDRs

**Three prior RDRs bear on this one, and none of them justifies it.** Two are
the origin and the precedent for scope this RDR has now WITHDRAWN; the third is
an adjacent draft it no longer overlaps. They are recorded here because a reader
who finds them will otherwise assume a relationship that the narrowed scope has
dissolved — and because the deferred work will need them.

An earlier, wider draft opened this section by claiming RDR-198 was "the third
member of an established family". That claim is withdrawn with the scope that
supported it.

### RDR-063 — the origin of the split (closed)

RDR-063 created the T2 domain decomposition. It lists **six** numbered problems:
mixed concurrency regimes, cross-schema assumption leaks, opaque disk footprint,
incompatible retention policies, unclear ownership for future features, and
migration coupling.

**CORRECTED 2026-08-23 (Layer 3 gate critique).** An earlier draft of this
section quoted only the first and asserted that "every clause of that rationale
is now void". That was **false**, and it was selective in the direction that
strengthened this RDR's case. Only the first problem is substrate-specific:

> *"`memory` sees interactive writes… `relevance_log` sees automated writes…
> They share a single SQLite file and a single `threading.Lock`."*

That clause is genuinely obsolete — neither the file nor the lock exists after
RDR-152/158. But the draft also dropped RDR-063's own hedge on it: RDR-063
labels that problem **"(forward-looking)"** and says it *"has not been observed
as a bottleneck"*, describing it as a preemptive concern rather than a measured
one. Quoting it as a live justification that has since expired misrepresents
the source twice over.

The other five problems are **not** substrate-specific and are not addressed
here. Cross-schema assumption leaks, retention policy, and ownership are
domain-modelling concerns that survive any transport change.

**What this means for RDR-198, honestly stated**: RDR-063 is weaker support
than the earlier draft claimed. It shows the *store* split had one reason that
has expired — but this RDR does not propose undoing the domain split at all. It
proposes collapsing the TRANSPORT beneath it, which RDR-063 never argued for in
the first place. RDR-063 is therefore context, not justification.

### RDR-164 — precedent for the withdrawn scope, not for this one (closed)

RDR-164 reached the diagnosis behind this RDR's **WITHDRAWN** engine-side
scope — non-atomic client-side cross-store orchestration — for the collection-
and document-lifecycle domain, and shipped the fix. Its words:

> *"This shape is a **SQLite-era artifact**… there was no way to express
> 'deleting a collection purges all its derived state' as one transaction…
> That constraint is now largely gone."*

> *"Both are symptoms of the same disease: lifecycle integrity maintained by
> hand, in the client, across stores, non-atomically."*

It names two real bugs caused by that non-atomicity — `nexus-tquoj` (collection
delete never purged `aspect_extraction_queue`, so the aspect worker churned on
rows whose collection was gone) and `nexus-cugrk` (an orphan centroid kept
attracting chunks to a deleted topic).

**What this means now that the scope is narrowed.** RDR-164 is strong support
for the atomicity work — the orphan-state bug class is documented, with two
named instances, and the atomic-cascade remedy is in production for one domain.
It is **not** support for THIS RDR, which proposes no atomicity work at all.

Recorded here deliberately: when the deferred atomicity RDR is written, RDR-164
is its precedent and this is where to start. An earlier draft of RDR-198 leaned
on RDR-164 to raise confidence in a gap that has since been withdrawn — a
transfer of confidence from a sibling domain to an instance that had not been
observed in this one. The gate flagged that as overstated, and it was.

### RDR-193 — the adjacent draft (draft, UNMERGED)

RDR-193 moves index-time catalog reconcile and taxonomy compute onto the engine
as transactional SQL and Java jobs. Its Part A introduces
`nexus.catalog_reconcile_commit(...)` — a plpgsql function performing the whole
reconcile in ONE transaction.

That pattern — one operation, one server-side transaction — is what an earlier,
wider draft of RDR-198 proposed generally. **That scope is now withdrawn**, so
the overlap has disappeared rather than needing to be negotiated.

Scope boundary, explicit:

| Concern | Owner |
| --- | --- |
| Index-time catalog reconcile, housekeeping, linking | RDR-193 Part A |
| Taxonomy discover / clustering on the engine | RDR-193 Part B |
| Collection & document lifecycle cascades | RDR-164 (shipped) |
| **Client HTTP transport collapse** | **RDR-198 (this RDR)** |
| Operation endpoints / atomicity generally | a future RDR — see "Deferred" |
| Schema boundary (`nexus` / `staging` / `t1`) | a future RDR — see "Deferred" |

**These two RDRs no longer conflict and can proceed in either order.** RDR-198
is entirely client-side and changes no server behaviour; RDR-193 is entirely
server-side. If RDR-198 lands first, RDR-193 inherits a shared transport for
free. One dependency worth recording for later: RDR-193 Part A *grows* the
`staging` schema, so whenever the deferred schema-boundary question is picked
up, RDR-193's outcome is an input to it.

### The family this does NOT belong to

RDR-154 and RDR-156 established the "lean on Postgres" line — put integrity and
computation in the database that can express them. RDR-164 applied it to
lifecycle; RDR-193 proposes applying it to indexing.

**RDR-198 is NOT a member of that family**, and an earlier draft's claim that it
was is withdrawn along with the scope that supported it. Those RDRs move work
INTO the database. This one collapses a duplicated client transport and changes
no server-side behaviour at all. The atomicity work that WOULD belong to that
family is explicitly out of scope here — see "Withdrawn scope" — and should be
argued in its own RDR, on its own evidence, sequenced against RDR-193.

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
  *(Finding retained for the record; the scope it supported is withdrawn.)*
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
  jOOQ table below. NOTE: the raw-SQL gap this supported is WITHDRAWN with
  the engine-side scope; the finding is retained because it corrects a false
  claim, not because this RDR still acts on it.

- [x] Liquibase wraps each changeset in a transaction by default on Postgres —
  **Status**: **VERIFIED** — **Method**: Source Search (`ChangeSet.java:425`,
  liquibase-core 4.29.0), upgraded from Docs Only. Also verified: all statements
  in ONE changeset share ONE transaction. This pass **corrected a factual
  error** in the draft: there are ZERO `runInTransaction` opt-outs in this repo
  and no `CONCURRENTLY` anywhere, so the "Liquibase used tactically" framing was
  false and is withdrawn along with the schema scope it belonged to.

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

Two layers, client-only, each independently shippable and reversible. No engine
change, no release-lifecycle coupling, no wire-contract change.

**Layer 0 — Make the stores shareable.** Convert `HttpTokenStore` and
`HttpScratchStore` from constructor-baked `Authorization` to the per-call
`_auth_headers` pattern the other twelve stores already use, and migrate
`HttpAspectQueue`'s constructor-time timeout kwarg to per-call `timeout=`
overrides. Each is correct on its own merits regardless of whether Layer 1 ever
happens, and Layer 1 is unsafe without them.

**Layer 1 — One pooled transport.** Introduce a single process-wide pooled
`httpx` transport for the fourteen httpx stores. The facades are unchanged from
every caller's point of view; they stop owning connections.
`mcp_infra.t2_index_write` already runs against a refcounted process singleton
and returns its callable's value, so it serves reads as well as writes, and
`t2_ctx`'s own docstring already lists these call sites as "not yet converted".
The catalog tier already has a shared handle — T2 is the outlier, not the
pattern.

`HttpVectorClient` is out of scope: it is built on urllib, not `httpx.Client`
(`http_vector_client.py:422`), so it shares nothing to begin with.

### Technical Design

Interfaces only; signatures verified at implementation.

```text
// Illustrative — verify against RefreshableHttpStoreMixin during implementation
shared_transport() -> httpx.Client      # process-wide, refcounted
StoreFacade._request(method, path, **kw)  # uses shared_transport(); resolves
                                          # credentials PER CALL, never per client
```

The invariant Layer 0 establishes and Layer 1 depends on: **no credential, and
no per-store default, may live on the client object.** Everything domain-specific
travels per request.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Shared pooled transport | `src/nexus/db/t2/_refreshable_client.py` | **Extend** — it already holds the shared `ssl.SSLContext` from `888bdee8f`; the pool is the next thing to share, in the same place, with the same refcounting. |
| Process-singleton access | `mcp_infra.t2_index_write` | **Reuse** — refcounted singleton already exists and already returns values, so it serves reads. |
| Per-call auth | `RefreshableHttpStoreMixin._auth_headers` | **Reuse** — twelve stores already do this; Layer 0 brings the other two to it rather than inventing a mechanism. |
| Per-call timeout | `HttpCatalogClient`'s `timeout=` override | **Reuse** — the pattern `HttpAspectQueue` must adopt already exists and works. |

### Decision Rationale

The narrow scope is the point. Everything here is client-side, verifiable
locally, and reversible by reverting a commit. The two things research actually
proved — that twelve stores can share safely and that two cannot — are exactly
what Layers 0 and 1 act on.

The engine-side work an earlier draft proposed is not abandoned, but it is not
justified by anything measured here, and bundling it would put a
release-lifecycle dependency and a wire-contract change behind a 0.07% latency
argument. It belongs in its own RDR when there is evidence for it.

## Alternatives Considered

### Alternative 1: Do nothing — close this as won't-fix

**Description**: Accept fourteen clients. The two interim fixes already removed
most of the measurable cost; leave the shape alone.

**Pros**:

- Zero risk and zero work. Nothing is broken today: each store owns its client,
  so no credential bleeds and no request is mis-timed.
- The measured benefit is 0.07% of an `nx_answer` call. That is not a number
  anyone would fund work against.
- The two interim fixes (`888bdee8f`, `602940b35`) already captured the cheap
  wins — 40 client constructions fell from 0.361 s to 0.039 s, and the config
  parse from 0.248 ms to 0.009 ms per call.

**Cons**:

- The bulk path is unmeasured. The indexer constructs stores **per file**, so
  it pays construction per unit of work rather than once per call. If the
  benefit is anywhere, it is there, and nobody has looked.
- Keep-alive genuinely cannot survive a context boundary, which matters more
  against a remote managed engine than against a local one. This is asserted
  from the code, **not measured** in cloud mode.
- The Group 2 stores stay in a shape where a future author who tries to share a
  client — a natural, tempting optimisation — introduces cross-domain credential
  bleed. Layer 0 removes that trap whether or not Layer 1 follows.

**Reason for rejection — partial, and stated as such.** This alternative is
**accepted for Layer 1** as a live option pending measurement, and rejected for
Layer 0. Layer 0 is worth doing on its own: it removes a credential-bleed trap
and fixes an inconsistency where two stores diverge from a pattern the other
twelve follow. Layer 1 should not proceed until the indexer's per-file cost is
measured. If that measurement comes back as small as the `nx_answer` one, Layer
1 should be dropped and this RDR closed with Layer 0 shipped.

### Alternative 2: Leave the fan-out; keep optimising construction cost

**Description**: Continue the `888bdee8f` / `602940b35` approach — make each of
the fourteen clients cheaper to build.

**Pros**:

- Zero risk, no shape change, already partially done and working.

**Cons**:

- Each fix must be rediscovered per construct; two have been needed already.
- It does not remove the Group 2 credential-bleed trap.

**Reason for rejection**: it is the status quo with extra steps. Where it was
the right call — the two interim fixes — it has already been taken.

### Alternative 3: Collapse the domain facades into one client class

**Description**: Replace the fifteen classes with a single `EngineClient`.

**Pros**:

- Fewest moving parts.

**Cons**:

- Destroys a domain decomposition that is genuinely useful and that RDR-063
  argued for on grounds this RDR does not contest.
- Enormous call-site churn for no correctness gain.

**Reason for rejection**: the domain split is not the defect. The transport
mirroring of it is.

### Briefly Rejected

- **Engine endpoints for operation atomicity**: out of scope — see "Withdrawn
  scope". Latent gap, no measured instance, and it belongs sequenced against
  RDR-193.
- **Per-domain databases**: contradicts the premise; there is one engine and one
  database.
- **Bridge/compat layer for the old client shape**: this project evolves forward
  and does not bridge to its own past.

## Trade-offs

### Consequences

- Connection reuse becomes possible across context boundaries. This matters most
  against a remote managed engine; the size of the gain is **not yet measured**.
- The Group 2 credential-bleed trap is removed by Layer 0, permanently, whether
  or not Layer 1 follows.
- A shared pool means one saturated domain can slow another. Pool sizing becomes
  a real parameter rather than an accident of having fourteen pools — see Risks.
- No server-side behaviour changes. Nothing in this RDR touches the engine, the
  schema, or the wire contract.

### Risks and Mitigations

- **Risk**: Layer 1 lands before Layer 0 and a shared client sends one domain's
  credential on another's request.
  **Mitigation**: Layer 0 is a hard precondition, not a preference. Add a test
  asserting two stores with different tokens never observe each other's header;
  it should be written to FAIL against the current `HttpScratchStore` before
  Layer 0 converts it, so the guard is known to detect the thing it guards.

- **Risk**: A shared pool starves one domain under load from another.
  **Mitigation**: size `httpx.Limits` explicitly rather than inheriting the
  default, and record the chosen `max_connections` / `max_keepalive_connections`
  with the reasoning. Unquantified today — the concurrency ceiling is
  `QUOTAS.MAX_CONCURRENT_READS/WRITES` = 10 each per collection, which bounds
  the problem but has not been translated into a pool size.

- **Risk**: Layer 0's rewrite of the credential-refresh path breaks token
  renewal in a way unit tests miss.
  **Mitigation**: the refresh path is exercised by real token expiry, not by
  construction. Test against a short-TTL token so renewal actually fires, rather
  than asserting on the code shape.

### Failure Modes

- Layer 0 ships and Layer 1 never does. **This is an acceptable outcome**, not a
  failure — Layer 0 is independently correct. Recorded here so nobody treats a
  stalled Layer 1 as debt.
- Layer 1 ships without measuring the indexer path, and delivers 0.07%.
  **Detection**: the measurement is a precondition in Sequencing, not a
  follow-up.
- A future author shares a client across Group 2 stores before Layer 0 lands.
  **Detection**: the header-isolation test above, which is why it must be
  written to fail first.

## Sequencing and Logistics

1. **Layer 0** — convert `HttpTokenStore` and `HttpScratchStore` to per-call
   auth; migrate `HttpAspectQueue`'s timeout to per-call overrides. Ship it.
   Independently correct; no dependency on anything below.
2. **Measure the bulk path before committing to Layer 1.** The indexer
   constructs stores per file. Measure construction cost per indexed file, in
   BOTH local and cloud mode — the keep-alive argument is cloud-specific and is
   currently asserted from code, not measured. This is a gate on Layer 1, not a
   follow-up.
3. **Decide Layer 1 on that measurement.** If the per-file cost is material,
   implement the shared pool with explicit `httpx.Limits`. If it is as small as
   the `nx_answer` figure, **drop Layer 1 and close this RDR with Layer 0
   shipped** — Alternative 1 becomes the answer.

No engine work, no tag, no `REQUIRED_ENGINE_VERSION` coupling, no deploy
choreography. That is the point of the narrowed scope.

## Open Questions

- What is the per-file store-construction cost in the indexer, local and cloud?
  This decides Layer 1 and is the only question blocking it.
- What `httpx.Limits` should a shared pool carry? `QUOTAS.MAX_CONCURRENT_READS`
  / `MAX_CONCURRENT_WRITES` (10 each per collection) bound it but do not
  determine it.
- Should `HttpVectorClient` move from urllib to the shared httpx transport at
  some point? Out of scope here; it shares nothing today and changing it is a
  separate, larger change.

## Deferred to Their Own RDRs

Recorded so the withdrawn scope is recoverable rather than lost:

- **Operation atomicity via engine endpoints.** The mechanism is real
  (`mcp_infra.py:391-392`); the spike found no instance in 29 runs. Needs its
  own evidence and must be sequenced against RDR-193, whose Part A already
  designs `catalog_reconcile_commit` as one transaction.
- **Schema-boundary decision** (`nexus` / `staging` / `t1`). A real question with
  no evidence gathered. Note RDR-193 Part A grows the `staging` schema, so its
  outcome changes the input to this question.
