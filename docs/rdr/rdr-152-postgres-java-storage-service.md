---
title: "Postgres + Java Storage Service + Thin HTTP Bridge: Replace the SQLite Single-Writer Daemon Class with a Multi-Tenant Postgres Substrate Owned by One Strict Java Service"
id: RDR-152
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-06
accepted_date: 2026-06-06
related_issues: [nexus-mk73z]
related_rdrs: [RDR-105, RDR-112, RDR-113, RDR-120, RDR-128, RDR-129, RDR-140, RDR-141, RDR-146, RDR-149, RDR-151]
related_tests: []
implementation_notes: ""
---

# RDR-152: Postgres + Java Storage Service + Thin HTTP Bridge

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **Supersedes-on-accept:** epic `nexus-mk73z` and the still-open RDR-151
> remediation beads (`nexus-u2vmv`, `nexus-nob1q`, epic `nexus-5kcsq`). Those
> patch a model this RDR removes. Do not implement them once this RDR is
> accepted; mark them superseded at planning time.

## Problem Statement

Every persistent shared-state store in nexus except the ChromaDB vector store
is a **process-local SQLite file fronted by a single-writer daemon**. That
model has been patched continuously and still fails in production:

- **~12 RDRs** target this one cluster: RDR-105, 112, 113, 120, 128, 129, 140,
  141, 146, 149, 151 (and 010/037/038/094 adjacent).
- RDR-151 shipped four root-cause fixes (taxonomy write serialize, Phase 3
  write routing, the u2vmv spin guard, xmohw full write serialization) across
  5.10.0–5.10.5. The **2026-06-06 shakeout of 5.10.5 still found**: (1) a lone
  idle daemon pegging ~99% CPU with the root never captured; (2) a spin-guard
  detector blind spot that never fired on the actual empty-immediate-return
  spin shape; (3) a spawn **stampede** where N MCP clients all `ensure-running`
  against no daemon, race the spawn lock, and sometimes leave a 3-daemon
  no-owner limbo.

This is the `feedback_root_cause_after_repeated_patches` trigger taken to its
limit: the subsystem has been patched across **five-plus releases** and the
peg/stampede/version-skew class persists. The root cause is structural, not a
bug: **SQLite has exactly one writer**, so every concurrency property nexus
needs (concurrent writers, fair scheduling, no spawn election, no version-skew
double-writer) has to be *simulated* by a hand-rolled daemon. That simulation
is the bug factory.

**Decision (Hal, 2026-06-06):** abandon the SQLite single-writer-daemon model.
Move all non-Chroma storage to a single multi-schema **Postgres** database
owned by one strict **Java** service. Postgres provides real MVCC concurrent
writers natively, dissolving the entire peg/stampede/version-skew class at the
substrate level rather than patching it at the application level.

### Enumerated gaps to close

#### Gap 1: No concurrent-writer substrate

SQLite serializes all writes through one lock; nexus simulates concurrency with
a single-writer daemon that has pegged CPU and starved foreground work
(RDR-146 #1046). **Fix:** Postgres MVCC — multiple sessions write concurrently
with row-level locking; no application-level single-writer election exists to
race or peg.

#### Gap 2: Storage I/O is scattered across many processes

Today the nx CLI, the MCP server, `claude -p` subprocesses, and the daemon all
open storage handles directly (`sqlite3.connect`, `chromadb.PersistentClient`,
Voyage clients). "No DB access outside the service" is enforced only by a lint
(RDR-112). **Fix:** one Java service owns 100% of storage I/O — Postgres *and*
ChromaDB+Voyage. MCP and CLI hold **zero** storage libraries; they speak
HTTP/JSON to the service. The boundary is a process boundary, not a linter.

#### Gap 3: Migrations are ad-hoc and advance the version row ahead of reality

`src/nexus/db/migrations.py` is a hand-rolled forward-only runner; RDR-142
documents `apply_pending` advancing `_nexus_version` while gated steps remain.
**Fix:** Liquibase changesets with checksum-validated, ordered, idempotent
application; the version is the changelog state, not a hand-bumped row.

#### Gap 4: Raw string SQL with no compile-time schema binding

Eleven T2 stores plus the catalog hand-write SQL strings. **Fix:** JOOQ —
type-safe SQL generated from the live schema; schema drift is a compile error.

#### Gap 5: No tenancy isolation

All state is implicitly single-user (RDR-113 made "single-user v1" load-bearing
and then was scrapped). There is no boundary preventing one owner's data from
being read/written under another's context. **Fix:** Postgres Row-Level
Security from the first changeset, keyed on a workspace/user principal GUC, with
`owner_id` as a sub-scope. Multi-tenant is a schema invariant, not a later
retrofit.

## Context

### Background

The pain is measured, not hypothesized — see Problem Statement and the
RDR-151 forensic postmortem (`docs/postmortem/2026-06-05-daemon-concurrency-forensics.md`).
The architectural lesson that governs this RDR is **RDR-120**: the 110–119 arc
died because the storage substrate shipped intertwined with new consumer
abstractions (tuplespace, ORB, cockpit, host-trust). RDR-120 succeeded by
shipping **substrate-only, no co-shipped consumers**. This RDR inherits that
discipline as a hard constraint.

RDR-112/113 are scrapped but their research is reusable: the storage-as-service
boundary, the transport spike (sub-ms RPC overhead), the storage-boundary lint,
the `NX_STORAGE_MODE` cutover flag, and the single-user trust baseline that this
RDR now extends into real RLS tenancy.

### Technical Environment

**What moves to Postgres (everything but Chroma vectors):**

| Domain | Current location | Tables (representative) |
|---|---|---|
| T1 scratch | chroma-ephemeral per MCP | (new) session-scoped scratch |
| T2 memory | `db/t2/memory_store.py` | memory entries |
| T2 plans | `db/t2/plan_library.py` | plan library |
| T2 telemetry | `db/t2/telemetry.py` | `tier_writes`, `nx_answer_runs`, `search_telemetry`, `hook_failures` |
| T2 taxonomy | `db/t2/catalog_taxonomy.py` | `topics`, `taxonomy_meta`, `topic_assignments`, `topic_links` |
| T2 aspects | `db/t2/document_aspects.py` | `document_aspects` |
| T2 highlights | `db/t2/document_highlights.py` | `document_highlights` |
| T2 aspect queue | `db/t2/aspect_extraction_queue.py` | `aspect_extraction_queue`, `aspect_promotion_log` |
| T2 chash | `db/t2/chash_index.py` | `chash_index` |
| T2 frecency | `db/migrations.py` | `frecency` |
| Catalog | `catalog/catalog_db.py` + 8 modules | documents, links, spans, `document_chunks` manifest |
| Version | `db/migrations.py` | `_nexus_version` → Liquibase changelog |

**What stays in ChromaDB:** T3 vectors + Voyage/ONNX embeddings — but reachable
**only through the Java service** (per the locked Chroma-boundary decision).

**New runtime:** a JVM service (Postgres via JOOQ, Liquibase migrations,
embedded HTTP server) plus thin Python HTTP clients replacing the current
`db/`, `daemon/`, and direct-Chroma call sites.

## Research Findings

### Investigation

Prior art read: RDR-112 (§Approach storage-as-service boundary + A1–A4 research),
RDR-113 (trust model / single-user baseline), RDR-120 (substrate-only scope +
phased cutover P0–P6), RDR-146 (catalog starvation forensics), RDR-149 (unified
service-registry substrate), RDR-151 (daemon peg forensics). Module map
confirmed via `src/nexus/db/`, `src/nexus/db/t2/`, `src/nexus/catalog/`,
`src/nexus/daemon/`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
|---|---|---|
| Liquibase | **Yes — delos** | Proven in `~/git/delos`: XML and YAML changelogs, `<include>` master-file composition (witness-service), `create schema` + `createTable schemaName=` + `rollback` blocks, schema-per-tenant changesets (`examples/multi-tenant-demo`). Version **4.8.0**. |
| JOOQ | **Yes — delos** | Proven in `delphinius`/`sql-state`: `liquibase update → jooq-codegen` bound to Maven `generate-sources` (schema applied to a DB, table classes generated from the **live** schema → drift is a compile error). `DSL.using(dataSource, SQLDialect)`, static generated-table imports, DataSource-pool ctor ("JOOQ manages connection lifecycle per operation"). Version **3.18.15**. |
| PostgreSQL (RLS, GUC, MVCC) | No — **pending spike** | **Not covered by delos** — delos targets H2 (`SQLDialect.H2`), which has no row-level security. RLS policy + `current_setting()` GUC is genuinely new; must be verified against PG before lock. |
| ChromaDB HTTP API (from JVM) | No — **pending** | Confirm full server-API parity for the ops the Python client uses (query/upsert/get with quotas). |
| Voyage AI (from JVM) | No — **pending** | Confirm REST contract + retry semantics currently in `nexus.retry._voyage_with_retry`. |

#### Prior Art: delos (`~/git/delos`) — the Liquibase+JOOQ reference

delos is Hal's Java distributed-systems framework and already runs the exact
Liquibase+JOOQ machinery this RDR proposes. Reusable patterns:

- **Build loop** (`delphinius/pom.xml`): `liquibase-maven-plugin` (goal `update`)
  then `jooq-codegen-maven` (goal `generate`), both in `generate-sources`;
  generated sources added via `build-helper`. The changelog is the schema
  authority; JOOQ codegens from it. **This is the RDR's "schema drift = compile
  error" claim, already in production.**
- **Schema module split** (`schemas/src/main/resources/delphinius/*.xml`): DDL
  changelogs (`delphinius.xml`, `delphinius-temporal.xml`,
  `delphinius-functions.xml`) live in a dedicated `schemas` artifact, consumed
  by multiple service modules. Maps onto nexus's "one schema per domain".
- **delphinius = a Zanzibar-style ReBAC oracle on JOOQ** (`AbstractOracle.java`,
  `DirectOracle.java`): Subject / Object / Relation / Namespace / Edge /
  Assertion model with recursive-CTE authorization queries. **Namespaces are
  effectively tenants.** This is a candidate *authorization* layer that could
  complement (or, for app-level checks, partially substitute for) Postgres RLS —
  to be weighed in the tenancy spike.
- **Tenancy pattern in delos is schema-per-tenant + a `tenant_id` column**
  (`examples/multi-tenant-demo/multi-tenant-changelog.xml`), **not** RLS. So the
  consolidation choice (RLS-via-GUC in one DB vs schema-per-tenant) is a real
  fork the spike must settle; delos leans schema-per-tenant because H2 forced it.

### Key Discoveries

- **Documented** — The single-writer constraint is intrinsic to SQLite; every
  nexus daemon peg/starvation incident traces to simulating concurrency around
  it (RDR-146, RDR-151 forensics).
- **Documented** — RDR-120 proved substrate-only scope is the only way this
  class of change lands; co-shipped consumers killed RDR-110–119.
- **Assumed** — Postgres RLS keyed on a session GUC is sufficient to enforce
  tenant isolation across all 11 stores + catalog without per-store bespoke
  filtering. *Needs source verification.*
- **Assumed** — Under **Seam B** (Technical Design), the JVM owns only
  *embedding + quota + Chroma write*; chunking/extraction stays in the Python
  indexer client. The dominant risk therefore reduces from full-pipeline parity
  to **embedding equivalence** (server-side Voyage/ONNX vectors must match
  today's). Still the largest scope item, but a single verifiable property.
  *Needs a spike.*

### Critical Assumptions

- [x] **RLS-via-GUC isolation is complete and tamper-resistant** — a thin client
  cannot escape its tenant scope because the service sets the GUC per request
  and the DB role has no `BYPASSRLS`. **Status: VERIFIED** (Spike S0.1, 2026-06-06,
  PostgreSQL 16.13) — **Method:** Spike. Proved: per-tenant SELECT isolation,
  zero-rows-without-GUC safe default, cross-tenant write rejected by `WITH CHECK`,
  and isolation holds even when the client supplies an explicit foreign-tenant
  predicate. Operational findings folded into design: (1) `SET LOCAL` is a no-op
  outside an explicit transaction → the JOOQ wrapper must own the txn boundary;
  (2) bind params are illegal in `SET`, so the GUC stamp uses
  `set_config('nexus.tenant', ?, true)` (bind-safe, txn-local); (3) the service
  role must be neither superuser (bypasses RLS) nor rely on owner exemption
  (`FORCE ROW LEVEL SECURITY` gates the owner too).
- [x] **JVM embedding equivalence (Seam B)** — server-side embedding (Voyage REST
  cloud / ONNX-in-JVM local) produces vectors equivalent to today's Python path.
  **Status: VERIFIED** (Spike S0.2, 2026-06-06) — **Method:** Spike. **Local ONNX:
  cosine = 1.00000000** on a fixed corpus — onnxruntime-java 1.20.0 + DJL HF
  tokenizer 0.30.0, loading the *identical* chromadb artifact
  (`~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/{model.onnx,tokenizer.json}`)
  and reproducing chromadb's exact pipeline (HF tokenize → ONNX
  input_ids/attention_mask/token_type_ids=0 → masked mean-pool → L2-normalize).
  **Cloud Voyage: cosine = 1.0** (voyage-code-3) once the JVM request envelope
  matches the SDK. **Load-bearing finding:** the voyageai SDK defaults
  `truncation=true`; omitting it shifts vectors to cosine **0.99995** — looks fine,
  silently degrades retrieval, evades a loose threshold. The JVM client MUST mirror
  the SDK envelope (`truncation`, `input_type`, `output_dtype`, `output_dimension`)
  and the parity harness MUST assert **exact** equivalence, not a tolerance
  (`feedback_exact_assertions_for_fixture_regression`).
- [x] **HTTP/JSON bridge latency is acceptable** — per-call overhead for the hot
  paths stays within the RDR-112 A3 sub-ms-class envelope under loopback.
  **Status: VERIFIED** (Spike S0.3, 2026-06-06) — **Method:** Spike. JDK-builtin
  `HttpServer` + Python client, 2000 calls, 246B payload: **p50=0.162ms,
  p90=0.231ms, p99=0.377ms, max=1.32ms, 99.9% sub-1ms** — and that's worst-case
  (urllib opens a fresh connection per call; a pooled client would be lower). The
  HTTP hop is not a regression vs the old UDS-RPC daemon. Note: the production
  Python client should use a keep-alive connection pool (httpx/requests Session).
- [x] **Liquibase + JOOQ compose at the Postgres dialect** — confirm the delos
  build loop works targeting `SQLDialect.POSTGRES`, that Liquibase declares RLS
  policies as changesets, and JOOQ-generated SQL executes under a per-transaction
  RLS GUC. **Status: VERIFIED** (Spike S0.1, 2026-06-06) — **Method:** Spike.
  Proved on **JDK 25 / Maven 3.9 / jOOQ 3.20.11 / liquibase-maven-plugin /
  PostgreSQL 16.13**: a Liquibase changeset created schema+table+`ENABLE`/`FORCE
  ROW LEVEL SECURITY`+`CREATE POLICY` (confirmed via `pg_policies` /
  `relrowsecurity`); JOOQ codegen ran against the live PG schema; generated-table
  queries under `set_config(...,true)` isolated per tenant and blocked
  cross-tenant writes. The codegen-from-live-schema → compile loop runs clean on
  JDK 25 (no toolchain blocker).

## Proposed Solution

### Approach

Four locked decisions (Hal, 2026-06-06) frame the design:

1. **Java fronts Chroma too.** The Java service owns ALL storage I/O including
   vector search/upsert; it calls ChromaDB's HTTP API and Voyage. MCP/CLI carry
   zero storage libraries.
2. **T1 scratch → Postgres** (unlogged / per-session, tenant-scoped). The
   chroma-ephemeral T1 lifecycle is removed entirely.
3. **Tenant = a workspace/user principal** above `owner_id`; `owner_id` is a
   sub-scope. RLS policies key on a session GUC set to the principal.
4. **Bridge = HTTP/REST + JSON**, loopback + token auth for v1.
5. **Deployment = a self-contained service binary** (Hal, 2026-06-06): the JVM
   service ships as a **GraalVM native-image** (preferred) or a **jlink** custom
   runtime image — no user-installed JRE/JDK. Prior art: delos's `delphinius`
   already ships `META-INF/native-image/reachability-metadata.json` for the JOOQ
   stack, so native-image is proven against JOOQ+Postgres; the residual risk is
   onnxruntime/DJL JNI under native-image, which the jlink fallback fully covers.
   The native-vs-jlink choice is resolved by an early build spike (S0.4); both
   are self-contained, so neither changes the Phase-1 skeleton. **Postgres is
   nx-managed/local** — supervised by the service the way nx manages `chroma run`
   today (RDR-149 lifecycle heritage), not a user prerequisite. There is **no
   direct-mode fallback** (the boundary is the point), so service+Postgres
   supervision is a v1 reliability requirement, addressed in Phase 5.

**Single Postgres DB, multiple schemas.** One database; schemas partition
domains (e.g. `t1`, `memory`, `plans`, `telemetry`, `taxonomy`, `aspects`,
`catalog`). RLS policies live on every tenant-scoped table from the first
changeset.

**Substrate-only scope (RDR-120 law).** This RDR ships the Postgres substrate,
the Java service, the HTTP bridge, and the thin Python clients — and migrates
the *existing* stores to it at behavioral parity. It ships **no new consumer
features**. The four AskUserQuestion forks are settled; nothing else expands.

### Technical Design

Component relationships:

```text
  nx CLI (thin)  ─┐
                  ├─ HTTP/JSON ──▶  Java Storage Service ──▶ Postgres (JOOQ, RLS)
  MCP server  ────┤   (loopback,        │
  (thin client)   │    token + tenant)  ├──────────────▶ ChromaDB HTTP (vectors)
  indexer client ─┘                     └──────────────▶ Voyage / ONNX (embed)
```

#### The service API surface (maps 1:1 onto today's tools)

The HTTP contract is the union of the current MCP tool set and `nx` CLI verbs —
nothing new. Resource families and their backing store:

| Family | Operations (from MCP core + catalog + CLI) | Backing store |
|---|---|---|
| scratch (T1) | put/search/list/get/delete, flag/promote | Postgres `t1` schema |
| memory (T2) | put/search/get/delete/consolidate | Postgres `memory` |
| plans | save/search | Postgres `plans` |
| catalog | register/link/links/link_query/resolve/show/list/stats/update/traverse | Postgres `catalog` |
| aspects/highlights/queue | extract-queue, promote, get | Postgres `aspects` |
| taxonomy | topics/assignments/links | Postgres `taxonomy` |
| telemetry | tier_writes, nx_answer_runs, search_telemetry, hook_failures | Postgres `telemetry` |
| vectors (T3) | search, query, store_put, store_get(_many), store_list, store_delete | ChromaDB (via service) |
| operators / nx_answer | extract/filter/rank/compare/check/verify/groupby/aggregate/generate/summarize, nx_answer, plan_audit | compute-only (read via service) |

**Operators and `nx_answer` stay where they are** — they are compute over
retrieved data, not storage I/O. They become thin-client callers of the
service's read endpoints; they do not move into Java. Only storage I/O crosses
the boundary.

#### The vector seam — resolving the dominant risk

"Java fronts Chroma" was the locked decision, but the *granularity* matters
enormously. The full ingest pipeline (`code_indexer`, `prose_indexer`,
`md_chunker`, `pdf_chunker`, `doc_indexer`, `chunker`, `index_context`) is
large and language/format-specific: tree-sitter AST chunking over the 31-language
`LANGUAGE_REGISTRY`, Docling/MinerU PDF extraction, llama-index, CCE
window-merge. **Almost none of that is storage I/O — it is content processing.**

Two seams satisfy "no Chroma access outside the service":

- **Seam A (full port):** Java re-implements chunking + extraction + embedding +
  Chroma writes. Re-derives tree-sitter, Docling/MinerU, CCE windowing in the
  JVM. Largest parity surface; highest risk; little of it is storage.
- **Seam B (thin vector boundary) — RECOMMENDED:** chunking/extraction stays in
  the Python indexer client (unchanged); it sends **chunk text + metadata** to
  the service's `upsert-chunks` endpoint. The service owns **embedding (Voyage
  REST / local ONNX), quota enforcement (`chroma_quotas.py`), and the Chroma
  read/write**. Python keeps zero Chroma/Voyage clients. The boundary holds; the
  parity surface shrinks to *embedding-equivalence only* (server-side embed must
  match today's vectors), not chunk-boundary parity.

Seam B keeps the dominant risk to one verifiable property — **embedding
equivalence** — and confines new JVM work to: a Voyage REST client (cloud mode)
and an ONNX-in-JVM embedder (local mode, via onnxruntime-java/DJL) plus the
quota validator. This RDR adopts Seam B and treats the chunk/extract pipeline as
client-side content processing that feeds the service. *Confirm at gate.*

#### Tenancy: schemas are domains, RLS carries tenancy

"Single DB, multiple schemas" — schemas partition **domains** (`t1`, `memory`,
`plans`, `catalog`, `aspects`, `taxonomy`, `telemetry`), not tenants. Tenancy is
a **`tenant_id` column on every tenant-scoped table + a Postgres RLS policy**
keyed on a session GUC the service sets per request:

```text
-- Liquibase changeset (Postgres dialect):
ALTER TABLE memory.entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.entries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON memory.entries
  USING (tenant_id = current_setting('nexus.tenant')::text);
-- Per request, the service runs (in the same txn, before JOOQ):
--   SET LOCAL nexus.tenant = '<principal>';
-- The service's DB role has NO BYPASSRLS.
```

The tenant principal is a **workspace/user** identity (the locked fork), with
`owner_id` (catalog owner, e.g. `nexus-1-1`) as a sub-scope column. delos's
`delphinius` ReBAC oracle (Subject/Object/Relation/**Namespace**) is the
candidate for *fine-grained* authorization above the coarse RLS tenant gate, if
sub-tenant sharing is ever needed — noted, not in v1 scope.

This is the one part with **no delos precedent** (delos is H2, schema-per-tenant,
no RLS) and is therefore the first Phase-0 spike.

**v1 tenancy scope (honest framing).** In practice a single workspace exists, so
the deployed system runs one tenant. What "multi-tenant from the start" delivers
in v1 is **structural, not a multi-user product**: every tenant-scoped table
carries `tenant_id` + an RLS policy from its first changeset, the service stamps
the GUC per request, and **a second-tenant negative test exercises the isolation
boundary on every store** (S0.1 already proved this for memory). Principal
*provisioning* (how a second real workspace/user is created, authenticated, and
assigned a token) is the only deferred piece — it is a Phase-5 concern and does
not change the schema. The boundary is built and tested now; the multi-user
front door is later. This avoids the RDR-113 retrofit trap without smuggling a
multi-user feature in under "substrate".

#### JOOQ session handling under RLS

delos uses `DSL.using(dataSource, dialect)` with pool-managed connections. Under
RLS the GUC must be set on the **same connection/transaction** as the query, so
the service acquires a connection, issues `SET LOCAL nexus.tenant`, and runs JOOQ
inside that transaction — never a bare pooled `DSLContext` that could leak a
connection without the GUC. The connection-acquire wrapper that stamps the GUC is
the load-bearing primitive; an RLS negative test guards it.

#### T1 scratch session contract (replaces RDR-105)

Retiring the chroma-ephemeral T1 lifecycle (Decision 2) means retiring RDR-105's
contract (per-session chroma, `NX_T1_HOST`/`NX_T1_PORT` env passdown for
sub-agent sharing, owned/share_t1/ephemeral modes, process-group cleanup). It
must be replaced, not just deleted, or every `claude -p` sub-process that shares
T1 today breaks. The Postgres replacement:

- **Scoping.** T1 lives in the `t1` schema as `scratch(tenant_id, session_id,
  key, ...)` — **unlogged** tables. It is gated by **both** RLS (`tenant_id`)
  and a `session_id` column. The service derives `session_id` from a
  **session token** the client presents (distinct from the bridge auth token);
  reads/writes are filtered to `(tenant_id, session_id)`.
- **Sub-agent sharing** (the RDR-105 passdown replacement). Sharing T1 across a
  parent and its `claude -p` sub-processes becomes "**pass the session token**"
  instead of "pass the chroma host:port". A sub-process launched with the
  parent's session token sees the parent's T1 rows; one launched with a fresh
  token (the `owned`/`ephemeral` analog) is sealed. The env var changes from
  `NX_T1_HOST`/`NX_T1_PORT` to a single `NX_T1_SESSION`.
- **Cleanup.** Unlogged tables survive Postgres restarts and MCP death does **not**
  auto-clean them, so cleanup is explicit: (a) the MCP lifespan calls a
  `session-close` endpoint on exit that deletes the session's rows; (b) a TTL
  sweep (a service-internal job) reaps rows whose `session_id` has been idle
  past a bound, covering crashed MCPs that never call close. Both are required —
  (a) for promptness, (b) as the crash-safety backstop.
- **In-process Agent-tool sub-agents** still share their parent's T1 trivially —
  they reuse the parent's session token in-process. No separate instance, same
  as RDR-105's in-process case.

This contract is locked in Phase 2 step 3 (T1 migration); the `session_id`
scoping column and the `session-close`/TTL endpoints are the load-bearing
additions.

**Code guidance:** interfaces only at this stage. The artifacts to lock during
planning are: (1) the HTTP request/response JSON shapes per resource family;
(2) the `nexus.tenant` GUC name + the policy template above; (3) the
JOOQ connection-acquire-with-GUC wrapper signature; (4) the `upsert-chunks`
endpoint contract (chunk text + metadata in, server-side embed); (5) the T1
session-token + `session-close`/TTL contract above. No service implementation
code in this RDR.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Postgres substrate | `db/migrations.py` (SQLite) | Replace |
| JOOQ data access | `db/t2/*.py` raw SQL | Replace |
| Liquibase migrations | `db/migrations.py` runner | Replace |
| Java service | `daemon/t2_daemon.py`, `daemon/t3_daemon.py`, `daemon/service_registry.py` | Replace (the entire daemon lifecycle class is deleted) |
| HTTP bridge | `daemon/t2_client.py`, `daemon/t3_client.py` (UDS RPC) | Replace |
| Embed + quota + Chroma write (Seam B) | `db/t3.py`, `db/local_ef.py`, `db/chroma_quotas.py`, Voyage clients | Replace (port to JVM) |
| Chunking / extraction | `code_indexer`, `prose_indexer`, `md_chunker`, `pdf_chunker`, `doc_indexer`, `chunker`, `index_context` | Keep (client-side; feeds `upsert-chunks`) |
| Thin Python clients | MCP tools, `nx` commands | Extend (swap backend, keep surface) |
| RLS tenancy | (none — RDR-113 scrapped) | New |

### Decision Rationale

Postgres removes the root cause (single writer) rather than patching its
symptoms. A single service-owned boundary makes "no DB access outside the
service" structurally true. Liquibase+JOOQ replace the two weakest links in the
current substrate (ad-hoc migrations, raw SQL). RLS-from-start avoids the
RDR-113 trap of bolting tenancy on later. The four forks were chosen toward
maximal consolidation deliberately: a half-migration (two storage owners) keeps
the boundary porous and re-admits the bug class.

**On the RDR-120 moratorium and "substrate-only".** RDR-120's arc deferred the
host-trust/multi-user substance (RDR-113) past a moratorium window. This RDR ships
tenancy (RLS + a tenant principal) and bridge token auth inside a "substrate-only"
framing, which is in tension with that deferral. The justification: tenancy here
is a **schema invariant baked into the first changeset**, not a consumer feature —
retrofitting RLS onto populated single-tenant tables later is far costlier and is
exactly the RDR-113 trap. v1 multi-tenancy is *structural*, not a multi-user
product (see the v1-tenancy scope note in §Technical Design): one default tenant
in practice, with the isolation boundary built and tested by a second-tenant
negative test from day one. This is the honest scope — RLS scaffolding that is
*exercised* by a real second tenant in tests, not a dormant column. The "substrate-only,
no co-shipped consumers" law is still honored: no new end-user feature ships;
the consumer surfaces (MCP tools, `nx` verbs) are unchanged.

## Alternatives Considered

### Alternative 1: Keep SQLite, harden the daemon further (RDR-151 path)

**Description:** continue root-causing the peg/stampede within the single-writer
daemon model.

**Pros:** no new runtime; smallest diff; team already deep in this code.

**Cons:** five-plus releases of patches have not closed the class; the root
cause (one writer) is unfixable within SQLite.

**Reason for rejection:** `feedback_root_cause_after_repeated_patches` — the
repeated-patch trigger fired long ago. Patch N+1 is not the move.

### Alternative 2: Postgres, but keep Python as the service language

**Description:** same substrate, a Python service instead of Java.

**Pros:** reuses the existing chunking/Voyage/quota Python code; no JVM.

**Cons:** Hal's directive is Java + JOOQ; loses JOOQ's compile-time schema
binding and the JVM concurrency story.

**Reason for rejection:** explicit decision to commit to a Java service.

### Briefly Rejected

- **gRPC bridge:** more than v1 needs; HTTP/JSON chosen.
- **Per-tenant database (vs per-tenant RLS in one DB):** the "single DB, multiple
  schemas" directive rules this out.
- **Chroma stays Python-direct:** rejected by the locked Chroma-boundary fork;
  two storage owners re-admit the boundary-porosity bug class.

## Trade-offs

### Consequences

- (+) Entire single-writer peg/stampede/version-skew class dissolved at the
  substrate.
- (+) One enforceable storage boundary; tenancy is a schema invariant.
- (+) Compile-time schema safety (JOOQ) and audited migrations (Liquibase).
- (−) A new self-contained service binary (native-image/jlink) in a Python
  project; packaging/onboarding complexity (`nx init`, plugin install) grows.
- (−) Under Seam B, only the **embedding** path moves to the JVM (Voyage REST +
  ONNX-in-JVM, both verified at cosine 1.0 in S0.2); chunking/extraction stays
  in Python. The JVM correctness surface is embedding-equivalence, not the full
  pipeline.
- (−) Postgres becomes a managed runtime dependency (lifecycle, backup) where
  SQLite was zero-install — mitigated by nx-managed local Postgres (Decision 5).

### Risks and Mitigations

- **Risk:** JVM **embedding** diverges from Python, silently corrupting retrieval
  quality (chunking is unchanged under Seam B, so chunk-boundary divergence is
  off the table). **Mitigation:** an embedding-equivalence parity harness (fixed
  corpus, cosine≈1.0 cloud / exact ONNX) is a Phase-0 gate, not a later check
  (`feedback_exact_assertions_for_fixture_regression`,
  `feedback_no_silent_fallbacks_for_correctness`).
- **Risk:** RLS misconfiguration leaks cross-tenant data. **Mitigation:** RLS
  negative tests (a request in tenant A cannot read/write tenant B) on every
  store from the first changeset; DB role has no `BYPASSRLS`.
- **Risk:** Postgres provisioning friction kills the zero-install UX.
  **Mitigation:** nx-managed local Postgres provisioned at `nx init` (Decision 5,
  RDR-144 heritage); the service binary is self-contained (native-image/jlink) so
  no JRE prerequisite. The remaining footprint cost (Postgres + binary) is the
  accepted price of dissolving the bug class.
- **Risk:** No direct-mode fallback means a service/Postgres outage blocks all
  storage. **Mitigation:** Phase-5 supervision (auto-restart, health checks) on
  the RDR-149 lifecycle substrate; `nx doctor` surfaces a down service loudly.
  This is a deliberate trade — the porous boundary that fallback-mode created is
  what re-admitted the bug class.
- **Risk:** scope creep re-admits the RDR-110–119 failure. **Mitigation:**
  substrate-only law; phase-review-gate cross-walk at every boundary.

### Failure Modes

- **Visible:** service down → every thin client gets a connection error
  (loud, not silent). Migration checksum mismatch → service refuses to start.
- **Silent (the danger):** JVM pipeline producing subtly different chunks/
  embeddings; RLS predicate that filters too little. Both are caught only by the
  parity harness and RLS negative tests — hence both are in-scope gates, not
  deferred.
- **Diagnosis:** service structured logs + a `nx doctor` check that pings the
  service and verifies migration state + RLS policy presence.

## Implementation Plan

### Prerequisites

- [x] All four Critical Assumptions verified — RLS isolation (S0.1), embedding
  equivalence (S0.2), bridge latency (S0.3), Liquibase+JOOQ on Postgres (S0.1).
  All closed 2026-06-06.
- [x] Deployment model **decision** locked (Decision 5: self-contained
  native-image/jlink binary + nx-managed local Postgres).
- [ ] S0.4 build spike (native-image-vs-jlink with the onnxruntime/DJL JNI stack)
  — required **before the Phase-1 skeleton is cut**, not before accept. Both
  outcomes are self-contained binaries, so neither changes the skeleton shape;
  the spike only picks the build path.
- [x] T1 Postgres session contract specified (§Technical Design) — replaces
  RDR-105.

### Minimum Viable Validation

One store (proposed: **T2 memory**) fully migrated end-to-end: `nx memory put`
/ `nx memory search` / `nx memory get` route HTTP → Java → JOOQ → Postgres under
RLS, with a cross-tenant negative test proving isolation, and the Python thin
client passing the existing memory test suite unchanged. This proves the whole
spine (bridge + service + JOOQ + Liquibase + RLS) on a real store before the
rest follow.

### Phase 0: Spikes (close the four assumptions — no production code until green)

Each spike is throwaway, lives under `scripts/spikes/` (Python side) or a
scratch Maven module mirroring delos, and produces a written verdict appended to
the gate. The first two are coupled and ship together (a single delos-style
module is the cheapest way to prove both).

- **Spike S0.1 — Liquibase+JOOQ+RLS on Postgres (closes (a) + (d)).** Stand up a
  one-module Maven project modeled on delos `delphinius`: a `schemas`-style
  changelog that (i) creates a `memory` schema + `entries` table with a
  `tenant_id` column, (ii) `ENABLE`/`FORCE ROW LEVEL SECURITY`, (iii)
  `CREATE POLICY` keyed on `current_setting('nexus.tenant')`. Run
  `liquibase update` → `jooq-codegen` (Postgres dialect, real PG via Testcontainers
  or local). Then via JOOQ: acquire connection → `SET LOCAL nexus.tenant='A'` →
  insert/select; prove a `tenant='B'` session sees **zero** of A's rows.
  **Verdict gate:** RLS denies cross-tenant; codegen-from-live-schema works on PG.
- **Spike S0.2 — Embedding equivalence (closes (b), the dominant risk).** Take a
  fixed corpus of chunk texts; embed via (i) today's Python path (Voyage cloud +
  local ONNX MiniLM) and (ii) a JVM embedder (Voyage REST client; ONNX via
  onnxruntime-java/DJL). **Verdict gate:** server-side vectors match today's
  **exactly — cosine = 1.0**, both modes (no tolerance; the cloud path requires
  mirroring the voyageai SDK request envelope, `truncation=true` included).
- **Spike S0.3 — HTTP bridge latency (closes (c)).** Minimal Java HTTP endpoint
  + Python client; measure per-call round-trip for the hot ops (scratch put/get,
  memory search) over loopback. **Verdict gate:** within the RDR-112 A3 sub-ms-class
  envelope; report the empirical distribution, no estimate.
- **Spike S0.4 — Native-image-vs-jlink build (the deployment model is already
  locked by Decision 5; this only picks the build path).** Build the S0.1
  JOOQ+RLS module as both a GraalVM native-image (adding reachability metadata
  for JOOQ per the delos `delphinius` precedent, and for the onnxruntime/DJL JNI
  stack — the open risk) and a jlink runtime image; confirm both produce a
  self-contained binary that runs the RLS query. **Verdict gate:** at least one
  path works end-to-end; prefer native-image, fall back to jlink if the JNI
  reachability config proves costly. Required **before the Phase-1 skeleton is
  cut**, not before accept; neither outcome changes the skeleton shape.

### Phase 1: Substrate + spine (deliver the MVV on one store)

Builds the reusable spine, then proves it on **memory** (chosen as MVV: the most
standalone store — no catalog/taxonomy FKs).

- **1.1 Service skeleton.** Maven multi-module Java project (location decision
  from S0.4 — likely a `service/` subtree in the nexus repo, separately built):
  HTTP server, connection pool, the JOOQ connection-acquire-with-`SET LOCAL`
  wrapper, structured logging, `/health`, token auth, tenant-principal extraction
  from the request. **Bootstrap auth (Phases 1–4):** a fixed loopback token
  generated at `nx init` into the service config + client config (the same
  pattern as the existing daemon's local trust); the full token lifecycle
  (rotation, per-tenant tokens, session tokens for T1) lands in Phase 5. The
  Phase-1 model is "fixed local token, rotated properly later" — not no-auth, and
  not a credential model that gets torn out.
  **Pooler note:** the GUC is `set_config(...,true)` (transaction-local), which is
  safe under a transaction-mode pooler but **leaks across transactions under a
  session-mode pooler**. If a pooler (e.g. PgBouncer) is introduced it must run
  in transaction mode; v1 connects the service directly to local Postgres (no
  pooler), so this is a forward-looking constraint, documented to avoid a future
  footgun.
- **1.2 Liquibase baseline changelog.** `db.changelog-master` with the first
  domain changeset: `memory` schema, `entries` table (+ `tenant_id`, RLS policy
  from S0.1). FTS: Postgres `tsvector`/GIN replaces SQLite FTS5 — parity per the
  Phase-2 FTS contract (top-K set equality + Spearman ≥ 0.90, not byte-identical
  order).
- **1.3 JOOQ codegen** bound to the build (liquibase update → generate), Postgres
  dialect.
- **1.4 Thin Python memory client.** Replace `db/t2/memory_store.py` call sites
  with an HTTP client; MCP `memory_*` tools and `nx memory` keep their signatures.
- **1.5 One-time data migration.** Idempotent SQLite→PG ETL for memory entries
  (RDR-076 idempotent-upgrade heritage), stamping the current single-user data
  under the default tenant principal.
- **1.6 MVV proof.** `nx memory put/search/get` route HTTP→Java→JOOQ→PG under RLS;
  existing memory test suite passes unchanged against the HTTP backend; a
  cross-tenant negative test proves isolation. **This is the in-scope spine proof.**

### Phase 2: Relational store migration ladder

Each store repeats the Phase-1 unit of work: changeset (schema + `tenant_id` +
RLS) → JOOQ codegen → thin Python client → idempotent SQLite→PG ETL → parity
suite passes unchanged + RLS negative test. Ordered easy-and-standalone first to
bank the pattern, graph-heavy last:

1. **plans** — standalone (`plan_library`). Smallest second proof.
2. **telemetry** — append-mostly (`tier_writes`, `nx_answer_runs`,
   `search_telemetry`, `hook_failures`, `frecency`). No FKs; high write volume —
   good early concurrency soak.
3. **T1 scratch** — now a `t1` schema (unlogged tables), session-scoped,
   tenant-scoped. Retire the chroma-ephemeral T1 lifecycle and its env-passdown
   (RDR-105) once green.
4. **taxonomy** — `topics`, `taxonomy_meta`, `topic_assignments`, `topic_links`.
   `topic_links` references catalog docs → soft FK; migrate before catalog with
   the reference as a plain column, tighten after catalog lands.
5. **aspects / highlights / queue** — `document_aspects`, `document_highlights`,
   `aspect_extraction_queue`, `aspect_promotion_log`. Document-identity FK to
   catalog (soft until step 7). The aspect worker (RDR-138/146 coordination)
   becomes a service-internal consumer — its single-writer contention disappears.
6. **chash** — `chash_index`. Content-addressed; standalone.
7. **catalog** — the hardest, migrated last: `documents` (tumbler tree), links
   graph, spans, the `document_chunks` manifest (RDR-108), git-backing
   (`catalog_git`), auto-linker. Once it lands, tighten the soft FKs from steps
   4–5 into real cross-schema FKs. RDR-146's catalog-behind-daemon work is
   subsumed here. **`catalog_git` open question:** the git-backing reads/writes
   files on disk via git ops; the Postgres catalog holds metadata+links. Two
   options to resolve in catalog-migration planning: (a) the service shells out /
   uses a JVM git library and keeps git-backing; (b) git-backing is reframed as a
   Postgres-native export/audit log and the on-disk git mirror becomes optional.
   This is the single most complex item in the ladder and gets its own design
   note before the catalog phase starts — flagged, not hand-waved.

**FTS note (applies to memory, plans, catalog, taxonomy):** SQLite FTS5 →
Postgres `tsvector`/GIN. Query semantics differ (tokenization, BM25 vs
`ts_rank`), so byte-identical result order is not achievable and "existing suite
passes unchanged" does **not** hold for FTS-ranked queries. Parity is defined
per store as: **(1) top-K set equality** — the same document set appears in the
top-K for a fixed query battery; **(2) a rank-correlation floor** (Spearman ≥ a
threshold set per store from a labeled query set) where order matters. Stores
whose tests assert exact FTS order get those assertions relaxed to this
definition as an explicit, reviewed migration step — not silently. The labeled
query set is the store's **existing FTS test fixtures**; the committed floor is
**Spearman ≥ 0.90** (top-K set equality is exact). A store that cannot meet the
floor escalates to substantive-critic sign-off at its migration gate — the
threshold is not silently lowered by the migration author. This parity contract
is locked before Phase 1's memory migration begins.

**Per-store cutover & write-quiesce:** each store's ETL is SQLite→PG, then the
thin client flips. Writes to the old SQLite backend between ETL-complete and
client-flip would be lost. For low-stakes append stores (telemetry) the window
is documented and accepted. For **memory and catalog**, the cutover quiesces
writes first (the owning MCP/daemon write-path is stopped for the store, ETL
runs, client flips, writes resume) — a short bounded window, not silent loss.
Each store's migration step states its data-at-risk characterization.

### Phase 3: Vector path through the service (Seam B)

Move `search`/`query`/`store_put`/`store_get(_many)`/`store_list`/`store_delete`
behind the service; add the `upsert-chunks` endpoint (chunk text + metadata in,
server-side embed). The Python indexer keeps its chunking/extraction and calls
`upsert-chunks` instead of writing Chroma directly; it drops its Chroma + Voyage
clients. The embedding-equivalence parity harness is the gate.

### Phase 4: Decommission the daemon/SQLite class

**Transition rollback (Phases 1–3).** Until decommission, the deleted-per-store
SQLite code is not yet removed — it is gated behind an `NX_STORAGE_MODE`-style
flag (RDR-120 heritage) per store: `service` (new) vs `sqlite` (old). A store
that regresses in production flips back to `sqlite` while the SQLite data still
exists (the ETL is copy, not move, until the store is confirmed green). Only at
Phase 4 is the flag and the old code removed — at which point rollback is
fix-forward only. This makes the point-of-no-return explicit and late.

Only after every store is green behind the service:

- Delete `src/nexus/daemon/` (t2_daemon, t3_daemon, service_registry, discovery,
  spin_guard, t1_lease, clients, catalog_write_shim) — the entire lifecycle class.
- Delete `src/nexus/db/` SQLite handles + `migrations.py`; remove direct Chroma +
  Voyage clients from the Python tree.
- Remove the `nx daemon` command group and the T1 env-passdown machinery (RDR-105).
- Storage-boundary enforcement flips from the RDR-112 lint to a structural
  guarantee (no storage libs importable in the Python tree) — keep a CI tripwire
  asserting `sqlite3`/`chromadb`/`voyageai` are absent from the client package.
- Supersede/close the RDR-151 remediation beads and epic `nexus-mk73z`; tombstone
  the daemon-lifecycle RDRs (128/129/140/141/146/149/151) as "dissolved by RDR-152"
  via frontmatter pointer (do not delete — RDR files are permanent).

### Phase 5: Operational Activation

Service supervision (per S0.4), Postgres provisioning in `nx init` (RDR-144
onboarding heritage), the bridge token lifecycle, and the `nx doctor` checks that
ping the service + verify migration state + assert RLS policies present. Detailed
under Day 2 Operations below.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| Postgres DB | `nx doctor` | `nx doctor` | N/A (managed) | migration-state check | pg_dump (planning) |
| Java service | `nx doctor` (ping) | service `/health` | stop via service mgr | health endpoint | N/A |
| RLS policies | changelog | changelog | N/A | RLS negative tests | N/A |

### New Dependencies

PostgreSQL 16, Liquibase 4.x, **JOOQ pinned at 3.20.x OSS/Community edition**
(the version S0.1 verified; confirm Community covers RLS DDL + GIN — RLS/policy
DDL is raw `<sql>` so it is edition-independent); a Voyage REST client and an
ONNX-in-JVM embedder (onnxruntime-java 1.20.x + DJL tokenizers 0.30.x, the S0.2
stack) for the Seam-B embed path; the JDK-builtin `HttpServer` (S0.3). The
service ships as a **GraalVM native-image or jlink** binary (Decision 5) — the
native-image path needs reachability metadata for JOOQ (delos precedent:
`delphinius/.../native-image/reachability-metadata.json`) and, the open risk,
for onnxruntime/DJL JNI; the S0.4 build spike decides native-vs-jlink. Licenses
to confirm at planning (JOOQ OSS, ONNX runtime, DJL, the bundled MiniLM model).

## Test Plan

- **Scenario:** `nx memory put`/`search`/`get` over the bridge — **Verify:**
  parity with existing memory tests, unchanged client surface.
- **Scenario:** tenant A request attempts to read tenant B rows — **Verify:**
  RLS denies; zero rows; audited.
- **Scenario:** JVM embed a fixed corpus vs Python baseline (chunk boundaries
  unchanged under Seam B) — **Verify:** exact embedding-equivalence — cosine = 1.0
  for ONNX and for Voyage with the SDK envelope (no tolerance).
- **Scenario:** concurrent writers across stores (the old peg trigger) —
  **Verify:** no peg, no starvation, all writes commit.
- **Scenario:** Liquibase changelog applied twice — **Verify:** idempotent; no
  version-row drift (RDR-142 class closed).
- **Scenario:** service restart under load — **Verify:** clients reconnect; no
  data loss.

## Validation

### Testing Strategy

Integration-over-mocks against a real tmp Postgres and a real ChromaDB
(Ephemeral/server) — mocks hide the boundary bugs this RDR exists to kill.
"Done" for each store = its existing Python test suite passes unchanged against
the HTTP backend, plus RLS negative tests green.

### Performance Expectations

Bridge per-call overhead measured against the RDR-112 A3 sub-ms-class envelope
under loopback; reported empirically in the Phase-0 latency spike, not estimated
here.

## Finalization Gate

> Complete each item with a written response before marking Accepted.

### Contradiction Check

No contradictions found between the forensic evidence (RDR-151/146 daemon
failures), the RDR-120 substrate-only law, the four locked architecture forks,
and the proposed design. The one tension — shipping tenancy/auth under a
"substrate-only" framing while RDR-120 deferred RDR-113's multi-user substance —
is resolved explicitly in §Decision Rationale (tenancy is a schema invariant, not
a consumer feature; v1 is structural-not-multi-user). The Phase-0 spike findings
(S0.1–S0.3) are consistent with every assumption they were run to verify.

### Assumption Verification

All four Critical Assumptions are **VERIFIED** (Phase-0 spikes S0.1–S0.3,
2026-06-06): (a) RLS-via-GUC isolation, (b) embedding equivalence (local ONNX +
cloud Voyage, both cosine 1.0), (c) bridge latency (sub-ms), (d) Liquibase+JOOQ
on the Postgres dialect — all on JDK 25 / PostgreSQL 16. Evidence is in
§Research Findings → Critical Assumptions and T2
(`nexus_rdr/152-spike-S0.1-PASS`, `152-spike-S0.2-PASS`). No unverified **Phase-0
Critical Assumptions** remain. Two verifications are deliberately deferred (not
Phase-0 gates, scoped to their phase): **ChromaDB HTTP from the JVM** (Phase 3,
Seam B) and the **native-image-vs-jlink build** (S0.4 build spike before Phase 1
skeleton). Both are tracked in the API Verification table below as Pending with
their phase.

#### API Verification

| API Call | Library | Verification |
|---|---|---|
| RLS policy + `set_config('nexus.tenant',?,true)` GUC | PostgreSQL 16 | **Spike S0.1 — VERIFIED** |
| changeset apply (schema + `CREATE POLICY`) | Liquibase | **Spike S0.1 — VERIFIED** |
| codegen-from-live-schema + RLS session | JOOQ 3.20.11 | **Spike S0.1 — VERIFIED (JDK 25)** |
| embed (local ONNX, masked-mean-pool) | onnxruntime-java + DJL tokenizer | **Spike S0.2 — VERIFIED (cosine 1.0)** |
| embed (cloud) | Voyage REST (JVM) | **Spike S0.2 — VERIFIED (cosine 1.0 with SDK envelope)** |
| query/upsert/get | ChromaDB HTTP (JVM) | Pending — Phase 3 (Seam B), not a Phase-0 gate |
| native-image / jlink build | GraalVM / JOOQ + onnxruntime/DJL JNI | Pending — S0.4 build spike, before Phase-1 skeleton |

### Scope Verification

The Minimum Viable Validation (T2 memory end-to-end under RLS) is in scope for
Phase 1 and is the spine proof, not deferred.

### Cross-Cutting Concerns

- **Versioning:** Liquibase changelog is the version authority (closes RDR-142).
- **Build tool compatibility:** new JVM build (Gradle/Maven) alongside `uv`;
  packaging decision at planning.
- **Licensing:** JOOQ/Liquibase/Postgres license review at planning.
- **Deployment model:** LOCKED (Decision 5) — self-contained service binary
  (GraalVM native-image preferred, jlink fallback; native-vs-jlink decided by
  S0.4 build spike) + nx-managed local Postgres provisioned via `nx init`
  (RDR-144 heritage), supervised like `chroma run` (RDR-149). No direct-mode
  fallback; supervision is a v1 reliability requirement (Phase 5).
- **IDE compatibility:** N/A.
- **Incremental adoption:** store-by-store migration behind the bridge; thin
  clients keep surfaces stable so MCP/CLI behavior is unchanged per store.
- **Secret/credential lifecycle:** service ↔ DB credentials and the bridge token
  — generation/storage/rotation decided at planning.
- **Memory management:** JVM heap for the embedding pipeline; streaming strategy
  for large-corpus indexing decided in Phase 3.

### Proportionality

Right-sized for a substrate replacement of this magnitude. Trim candidates at
lock: collapse Day-2 if provisioning lands as its own planning artifact.

## References

- Epic `nexus-mk73z` (the decision record).
- RDR-120 (substrate-only law), RDR-112/113 (storage-as-service + trust research),
  RDR-146/149/151 (daemon forensics).
- **delos** (`~/git/delos`) — Liquibase+JOOQ reference: `delphinius/pom.xml`
  (liquibase→jooq codegen build loop), `schemas/src/main/resources/delphinius/*.xml`
  (schema-module DDL), `delphinius/AbstractOracle.java` (JOOQ ReBAC oracle),
  `examples/multi-tenant-demo/` + `examples/simple-kv-store/` (tenant changelogs),
  `witness-service/db/changelog/` (YAML master+include). Versions: delos uses
  JOOQ 3.18.15 / Liquibase 4.8.0 targeting H2; **nexus targets JOOQ 3.20.x /
  Postgres** — see §New Dependencies.
- `docs/postmortem/2026-06-05-daemon-concurrency-forensics.md`.
- `docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md` (scope-entanglement
  postmortem — the failure mode this RDR must not repeat).

## Revision History

- 2026-06-06: Draft created. Four architecture forks locked via AskUserQuestion
  (Java fronts Chroma; T1→Postgres; tenant=workspace/user principal; HTTP/JSON
  bridge). Substrate-only scope per RDR-120.
- 2026-06-06: delos (`~/git/delos`) prior art folded in — Liquibase+JOOQ
  mechanics verified (build loop, schema module, ReBAC oracle); RLS flagged as
  no-delos-precedent (H2). Technical Design expanded: service API surface mapped
  1:1 onto current tools; **Seam B** adopted (chunking/extraction stays
  client-side; service owns embed+quota+Chroma write) to reduce the dominant
  risk to embedding-equivalence; RLS-via-GUC tenancy design + JOOQ
  connection-acquire-with-GUC primitive specified. Seam B flagged for gate
  confirmation.
- 2026-06-06: **Phase-0 spikes S0.1–S0.3 executed — all four Critical Assumptions
  VERIFIED** on JDK 25 / PostgreSQL 16. S0.1: Liquibase changeset creates RLS
  policy + JOOQ codegen-from-live-schema + per-tenant isolation (cross-tenant
  read/write blocked); key findings — `SET LOCAL` needs an explicit txn, use
  `set_config(...,true)` bind-safe, service role must not be superuser/owner-exempt.
  S0.2: local ONNX cosine 1.0 (identical chromadb artifact in onnxruntime-java +
  DJL); cloud Voyage cosine 1.0 with SDK envelope — `truncation=true` default is
  load-bearing (omitting → 0.99995 silent drift), harness must assert exact
  equivalence. S0.3: bridge p50 0.16ms / p99 0.38ms / 99.9% sub-1ms. No unverified
  load-bearing assumptions remain. Ready for `/conexus:rdr-gate`.
- 2026-06-06 (Gate round 1, BLOCKED→addressed): substantive-critic found 2
  Criticals + 7 Significants. Resolved in-place: **C1 deployment model** locked
  (Decision 5: GraalVM native-image/jlink self-contained binary + nx-managed
  local Postgres; native-vs-jlink via S0.4 build spike); **C2 T1 session
  contract** specified (§Technical Design — session-token scoping, session-close
  + TTL cleanup, `NX_T1_SESSION` replacing the RDR-105 env-passdown). Significants:
  Consequences corrected to Seam-B scope; FTS5→tsvector parity defined (top-K set
  equality + Spearman floor); gate declaration reconciled with the API table
  (ChromaDB-HTTP-JVM Pending=Phase-3); per-store write-quiesce/cutover window
  added; bridge bootstrap-token model added (Phase 1) + PgBouncer transaction-mode
  constraint; catalog `git`-backing open question flagged; `NX_STORAGE_MODE`-style
  transition rollback (copy-not-move ETL) added; Test-Plan scenario 3 de-vacuumed;
  RDR-120 moratorium + v1-tenancy honest-scope framing added; JOOQ pinned 3.20.x
  OSS. Ready for re-gate.
- 2026-06-06 (Gate round 2 PASS + consistency pass): re-gate found 0 Criticals;
  the 2 sub-blocking Significants fixed (FTS Spearman ≥ 0.90 floor + existing
  fixtures as labeled set + critic-signoff escalation; S0.4 reframed as an
  unchecked pre-Phase-1 prerequisite). Full-read consistency pass aligned 5
  residual drifts: S0.4 Phase-0 blurb (now native-vs-jlink build, not open
  packaging probe), S0.2 Phase-0 blurb (exact cosine 1.0, no tolerance), Phase 1.2
  FTS note (points at the Phase-2 contract), Contradiction Check (completed — no
  contradictions), API Verification table (native-build row added to match the
  prose). Gate PASSED; ready for `/conexus:rdr-accept`.
