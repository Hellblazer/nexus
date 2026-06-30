---
title: "Survivable Managed-Migration Readiness: Batched ETL, First-Class Downgrade with an Immutable Source, Unified Config-First Auth, Edge Route Coverage, and Migration Observability"
id: RDR-176
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-30
accepted_date: 2026-06-30
related_issues: [nexus-gq5f9, nexus-1qpni, nexus-zvcou, nexus-6bhpm, nexus-tteq8]
related: [RDR-152, RDR-155, RDR-156, RDR-158, RDR-159, RDR-166]
---

# RDR-176: Survivable Managed-Migration Readiness

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The substrate RDRs (152 Postgres service, 155 pgvector, 156 capability leverage,
158 retire-SQLite) designed the **destination architecture**. RDR-159/166 designed
the **happy-path** of `guided-upgrade` and managed onboarding. None of them exercised
the **real upgrade experience at scale, over the real cloud edge, with a real user's
corpus** — and a 2026-06-29 production dogfood of the shipped 6.0.0 release proved that
gap is severe enough that the managed-migration path is **not user-ready**, despite the
"install-friction" RDRs (157/161/166/pebfx) closing green.

This RDR is the **readiness spine**: the set of gaps that must close before any 6.0.x
release advertises a managed-migration path. It governs under the standing directive
`directive-postgres-substrate-no-sqlite.md` (Postgres is the substrate; SQLite is
exception-only), and it coordinates with — does not duplicate — RDR-158 (the SQLite
*retirement* mechanics) and RDR-155/156 (the pgvector destination).

### Enumerated gaps to close

#### Gap 1: The migration is client-mediated row-by-row, when it should be bulk / server-side

The deeper defect (Hal, 2026-06-30): the migration shape is wrong, not merely unbatched.
The Python `nx` client *itself* reads each source store and pushes rows over HTTP — and
the T2 ETLs do it **one round-trip per row** (`HttpMemoryStore.import_entry`,
`HttpTaxonomyStore.import_assignment`; server handlers are single-row, `repo.importRow`).
At cloud RTT that is ~5–16 hours for the 190,112-row `topic_assignments` alone and 644k+
edge-exposed calls; even on localhost it is 190k transactions/RLS-checks for what should
be a few bulk inserts. But batching only fixes the *symptom*. The right shape depends on
who is closest to the source:

- **cloud→cloud** (ChromaCloud → managed pgvector — the prod case): the **service** pulls
  directly from ChromaCloud into pgvector **server-side**; the client triggers + monitors,
  never holds a vector. Today the laptop round-tripped 124k vectors between two clouds.
- **local→managed** (local SQLite → remote service): the remote service cannot read the
  local file, so the client ships a **single bulk dump → server `COPY`/multi-row load**,
  never per-row.
- **local→local** (SQLite → local PG): the service has filesystem access; it reads the
  source directly. The client does not mediate.

The single invariant is that the **service is the only writer** (RLS/schema/validation),
so the source is read **once, in bulk, by whoever is closest to it.** "Client reads source
and dribbles rows over the edge" is the worst shape and is what shipped. Batching is the
floor; server-side-direct (cloud legs) / bulk-dump-load (local→managed) is the target.

**This applies to EVERY leg — T2, catalog, AND T3 vectors — not just the per-row T2
offenders.** The rule is universal: **every transfer is O(N/batch), never O(N rows or
chunks).** T3 vectors already batch (`/v1/vectors/upsert-chunks` at 300; ChromaCloud reads
page at 300), so for T3 the remaining work is making the cloud→cloud leg **server-side**
(the service pages + upserts, no client round-trip) rather than adding batching it already
has. T2 (memory/plans/taxonomy/telemetry) is per-row today and needs both batching and the
right transport. `chash` (200) and catalog `document_chunks` (doc-batched) are partial;
catalog owners/documents/links and the rest are per-row. A single conformance test asserts
the O(N/batch) bound for **every store, no exceptions**: memory, plans, taxonomy (topics +
assignments), telemetry, chash, aspects, aspect_queue, catalog (owners, documents, links,
collections, document_chunks), and T3 vectors. Any leg whose transfer count scales as
O(rows/chunks) fails the test. (nexus-1qpni)

#### Gap 2: The upgrade mutates the legacy stores, so downgrade is blocked — the fix is NON-MUTATION, not backup

Downgrade should be free: reinstall the prior CLI and it reads its own untouched data.
It is not, because the upgrade **mutates the legacy source.** Running 6.0.0 wrote
`_nexus_version=6.0.0` into the local `memory.db` (via `migrations.bootstrap_version` on
open), and 5.10.6's fail-closed exact-match guard (`T2SchemaVersionMismatchError`) then
**refuses to open it**. Recovery on 2026-06-29 required hand-editing the stamp
(`UPDATE _nexus_version SET value='5.10.6'`) — surgery no user could perform.

**The correct fix is to NOT mutate the legacy stores, not to back them up.** If the
upgrade never stamps/migrates the 5.x SQLite and never modifies Chroma (Chroma is already
copy-not-move, and 6.x retired *serving* but must never *delete* it pre-P4b), then the
5.x install's data is pristine and downgrade = reinstall 5.x — no backup, no restore, no
version-guard surgery. Concretely: in service mode the local SQLite is **only ever a
read-only migration source**; the CLI/daemon must not run `bootstrap_version`/migrations
or write `_nexus_version` on it. A pre-upgrade auto-backup may remain as optional
belt-and-suspenders, but it is NOT the rollback mechanism — non-mutation is. (nexus-gq5f9)

#### Gap 3: Service auth resolution is split-brain across storage commands

T2/T3 are one store served by one service, but the CLI resolves the endpoint+token
inconsistently: `nx storage migrate vectors` takes `--service-url` and the managed
chain; the T2 migrate commands read the token from `NX_SERVICE_TOKEN` **env only** and
`migrate all` has no `--service-url` at all. A user who ran `nx config set
service_url/service_token` still must separately export env to migrate T2. (nexus-zvcou)

#### Gap 4: The managed edge has no route-coverage guarantee for the migration path

The cloud go-live gate (xr7.8.9) exercised vector/search routes; the T2 bulk-import
routes (`/v1/memory/import` and siblings) were never opened at the edge, so
`nx storage migrate all` 403s every row at nginx. No gate asserts that *every* endpoint
the migration uses is reachable for a tenant bearer. (nexus-6bhpm)

#### Gap 5: A running migration is unobservable

`migrate all` is silent (no per-store/per-batch progress); there is no destination-side
count/metric, so "is it progressing?" can only be answered by inferring from OS CPU or
paginating the read API. Success events are effectively invisible (only failures log).
(nexus-tteq8)

#### Gap 6: Transient edge failures are not retried in the ETL

The vector + T2 ETLs hard-fail a collection/row on a transient nginx 403/connection
drop (observed intermittently in prod); 403 is not classified retryable and there is no
read-timeout, so a blip strands a leg or hangs forever. Idempotent upsert makes a
bounded retry safe.

#### Gap 7: SQLite is still treated as a co-equal tier rather than an exception

Per the directive, SQLite must be exception-only (read-only migration source; possibly
one offline mode if justified). This RDR ratifies the directive as the acceptance frame
and coordinates the remaining SQLite-as-runtime-backend removal with RDR-158, ensuring
each surviving SQLite use is a documented, justified exception — not a default.

## Context

### Background

Discovered in a real 2026-06-29 production dogfood: upgrading Hal's own install to the
shipped 6.0.0 and migrating his ChromaCloud-origin corpus (≈124k vectors / 49
collections + T2: 3,544 memory, 190,112 topic_assignments, 448,434 chash) to the
managed cloud (`api.conexus-nexus.com`). The vector leg (passthrough, ~$0) succeeded
after two transient-403 retries; the T2 leg was first blocked at the edge (Gap 4), then
— once routes were opened — crawled per-row (Gap 1) and could not be observed (Gap 5);
the attempted rollback to 5.10.6 was blocked by the source-mutation version stamp (Gap
2). 6.0.0 is live on PyPI, so non-dogfood users are exposed now.

### Technical Environment

Python `nx` CLI + `uv tool`; Java engine-service (PG16/17 + pgvector) behind an nginx
edge at `api.conexus-nexus.com`; T2 SQLite (`memory.db`) with `_nexus_version` stamp and
the `T2SchemaVersionMismatchError` exact-match guard (`daemon/t2_client.py`); ETLs in
`src/nexus/db/t2/*_etl.py` and `src/nexus/migration/vector_etl.py`.

## Research Findings

### Investigation (research pass 1, 2026-06-30)

Anchor evidence + verified findings:
- `src/nexus/db/t2/memory_etl.py` / `taxonomy_etl.py`: per-row `import_*` calls.
- `service/.../http/MemoryHandler.java:387,457`: single-row `/v1/memory/import`.
- `nexus/daemon/t2_client.py:346-360`: exact-match version guard.
- `nexus/db/migrations.py:2264` `expected_t2_schema_version()`; `_nexus_version(key,value)`.
- `src/nexus/migration/vector_etl.py`: `_is_same_model_passthrough`, no retry on 403.

### Key Discoveries

- **Verified (Assumption 2 — feasible)**: `MemoryRepository.importRow` runs inside
  `tenantScope.withTenant(tenant, ctx -> doImport(...))`, where RLS is `SELECT
  set_config('nexus.tenant', ?, true)` (txn-local GUC, autoCommit=false) and the upsert is
  `INSERT … ON CONFLICT DO UPDATE`. A **bulk** endpoint reuses this verbatim: one
  `withTenant` txn per batch (GUC set **once per batch**, not per row) wrapping a multi-row
  INSERT with the same ON CONFLICT clause and verbatim timestamp/access_count. Strictly
  better than per-row.
- **Verified (Assumption 3 — already correct; Gap-2 correction)**: the T2 ETLs ALREADY
  open the source read-only — `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` in
  `memory_etl.py:120/169`, `taxonomy_etl.py`, `plan_etl.py`. So the **migration does not
  mutate the source.** The Gap-2 source mutation comes from a *different* path: running the
  6.0.0 CLI/daemon at all calls `migrations.bootstrap_version` on the live `memory.db` and
  stamps `_nexus_version=6.0.0`. ~~The fix is therefore the pre-upgrade auto-backup taken
  before the new CLI first opens the DB~~ — **SUPERSEDED by Research pass 2 Finding A and
  the Decision below: the fix is NON-MUTATION (gate the daemon/upgrade/doctor write paths),
  not backup.** The ETL is already read-only (`?mode=ro`).
- **Verified (backup sizing)**: full `~/.config/nexus` is ~42 GB, but the **restore-
  sufficient set ≈ 300 MB**: `memory.db` (241 MB, T2), `pipeline.db` (18 MB),
  `catalog/catalog.db` (6 MB) + `catalog/.catalog.db` (34 MB) + the small catalog jsonl
  (documents/links/owners ≈ 12 MB), `config.yml` (308 B). EXCLUDE: `catalog/events.jsonl`
  (1.6 GB — replayable event log), per-repo `*.cache` Chroma indexes (regenerable + 6.0.0
  doesn't touch them, so they survive in place), `postgres/` (3.1 GB local PG cluster),
  `*.bak`. A per-upgrade auto-backup of the ~300 MB stamped-DB set is feasible in seconds.
- **Architectural (Hal, 2026-06-30)**: cloud→cloud should be server-side direct (no client
  round-trip); local→managed a single bulk dump→load. See Gap 1 / Pillar 1.

### Research pass 2 (2026-06-30): deep audit (three parallel analyzers)

**A. Non-mutation IS achievable — the root cause is found and bounded (load-bearing).**
The mutation that blocked downgrade is **not** the migration ETL (which opens `?mode=ro`).
It is the **T2 daemon, launched unconditionally on every `nx-mcp` boot** regardless of
backend:
- `mcp/_first_run.py:176-181` shells `nx daemon t2 ensure-running` on every MCP start, with
  **no `NX_STORAGE_BACKEND` check**.
- The daemon opens `T2Database(db_path, run_migrations=True)` (`t2_daemon.py:943`) →
  `bootstrap_schema` → `apply_pending` → `UPDATE _nexus_version SET value='6.0.0'`
  (`migrations.py:2976`). **This is what stamped `memory.db=6.0.0`.**
- The same daemon builds a hosted Catalog and opens `.catalog.db` **read-write + schema DDL**
  (`t2_daemon.py:1471`, `catalog.py:347-370`) — also no service-mode check.
- Additional writers: `nx upgrade` (`upgrade.py:283-412`, no gate) and `nx doctor`
  (`doctor.py:67,535,2060` — `PRAGMA journal_mode=WAL` is a header **write** mislabeled
  read-only). The full-service write path (`mcp_infra.py:350`, `run_migrations=False`, all
  `Http*` stores) is already safe — it is bypassed by the daemon.

**Concrete change list (5; #1 + #3 sufficient for the guarantee):**
1. `_first_run.py`: skip the T2-daemon launch when `storage_backend_for("memory")==SERVICE`
   (root fix — the daemon is irrelevant in service mode).
2. `t2_daemon._build_hosted_catalog`: return `HttpCatalogClient` in service mode (no
   `.catalog.db` open).
3. `t2/__init__.bootstrap_schema`: skip the migration/stamp block in service mode (lowest-
   level gate).
4. `upgrade.py`: service-mode guard — "no local SQLite to migrate" and return.
5. `doctor.py`: open `?mode=ro`, drop the WAL pragma.
Enforced by the **byte-identical-before/after** test (Pillar 2).

**B. Server-side cloud→cloud is mostly REUSE, not new.** `ChromaRestClient.cloud(tenant,
db, apiKey)` with paginated `.get()`/`.upsert()` already exists in Java (attached to the
*retired* `VectorRepository` = dead code); `PgVectorRepository.upsertChunksWithVectors()`
already stores pre-computed vectors. New work ≈ a `POST /v1/migration/ingest-cloud` handler
(~200 LOC) + EgressProxy wiring + service-side Chroma cred plumbing. **Bulk T2:** `chash`
already batches (`upsert_many`, 200/call) — the reuse template; catalog endpoints already
accept arrays but loop per-element server-side (convert to jOOQ batch); new `import_batch`
endpoints for memory/plans/telemetry/taxonomy reuse `withTenant` (GUC once per batch).
Postgres `COPY` is an optional stretch (T2 is metadata, not vectors).

**C. Catalog is fully copied, but completeness is NOT fully asserted.** `catalog_etl.py`
copies owners/documents/links/collections/document_chunks via `/v1/catalog/import/*` (ON
CONFLICT idempotent) from the authoritative **`.catalog.db`** (35 MB; the legacy
`catalog.db`/6 MB has 0 chunks — must NOT be used). `events.jsonl`/`_meta` excluded by
design (PG needs no replay log). **Gap:** `_VERIFY_TABLES` (`orchestrator.py:43-54`) count-
verifies only documents + links — **owners, collections, document_chunks are NOT count-
verified**, so a partial copy of those passes green. Fix: add the three to the verify map.

**D. Edge route allowlist (Gap 4)** — the set a tenant bearer must reach:
`/v1/memory/import`, `/v1/plans/import`, `/v1/telemetry/import`, `/v1/taxonomy/*`,
`/v1/aspects/import|highlights/import|promotion/import|queue/import`,
`/v1/chash/import|upsert_many`, `/v1/catalog/import/{owner,document,link,chunk,collection}`,
`/v1/catalog/verify/relation-counts`, `/v1/vectors/upsert-chunks`, `/v1/vectors/collections`,
and the taxonomy import routes (now enumerated): `/v1/taxonomy/import/topic`,
`/v1/taxonomy/import/assignment`, `/v1/taxonomy/import/link`, `/v1/taxonomy/import/meta`.
The route-coverage gate asserts every one is reachable for a tenant bearer.

### Critical Assumptions

- [x] Bulk multi-row INSERT preserves ON-CONFLICT idempotency + verbatim fields + RLS
  principal — **Status**: Verified — `withTenant`/`doImport`; `chash upsert_many` is the
  proven template.
- [x] Migration opens the source read-only — **Status**: Verified (`?mode=ro`).
- [x] **Non-mutation is achievable** — **Status**: Verified — 5 bounded changes; the daemon
  launched by `_first_run.py` (service-mode-blind) is the sole stamp source; #1+#3 close it.
- [x] Server-side cloud→cloud feasible at low cost — **Status**: Verified — `ChromaRestClient.cloud`
  + `upsertChunksWithVectors` already exist; ~200 LOC + egress/cred wiring.
- [~] Additive-migrations / older-reads-newer — **Status**: MOOT — non-mutation means the
  5.x DB keeps its 5.x stamp; downgrade needs no schema compat.

## Proposed Approach (pillars — refine in planning)

1. **Right-shape the migration transport — bulk / server-side, not client-mediated**
   (Gap 1, 6): cloud→cloud legs (ChromaCloud → pgvector) move **server-side** (the
   service pulls from ChromaCloud directly; the client triggers + monitors); local→managed
   ships a **single bulk dump → server `COPY`/multi-row load**; local→local has the service
   read the source file directly. Bulk multi-row INSERT endpoints (array → one INSERT, GUC
   set once per batch — verified feasible via the existing `tenantScope.withTenant` + ON
   CONFLICT pattern) are the floor for the client-ships path; bounded transient-retry on
   the transfer. Tests assert transfer/call count is O(1)–O(N/batch), never O(N rows).

   **1a. Completeness gate must cover every table.** Extend `_VERIFY_TABLES`
   (`orchestrator.py:43-54`) + the service `/v1/catalog/verify/relation-counts` mapping to
   count-verify **owners, collections, document_chunks** (today only documents + links),
   and assert per-store count parity for every T2 store + T3 collections. A leg that lands
   a subset must FAIL. (Filed bead: catalog count-verify gap.)

   **1b. Cloud→cloud credential flow + egress (DECIDED).** The server-side ChromaCloud read
   reuses `ChromaRestClient.cloud(tenant, db, apiKey)`:
   - **Per-tenant creds — DECIDED (Hal, 2026-06-30): client-supplied, ephemeral.** The
     client supplies the tenant's ChromaCloud tenant/db/apiKey **in the migration trigger
     request body**; the service holds them **in memory for the request lifetime only —
     never persisted to the DB, never logged** (redact in any request logging). The
     operator service momentarily handling a tenant's third-party credential is accepted as
     the cost of an explicit, tenant-initiated migration. Enforced by a test asserting the
     creds never reach persistent storage or logs.
   - **Egress proxy**: `ChromaRestClient` does NOT route through the egress proxy
     (`ChromaRestClient.java:103-106`); behind squid, `cloud()` to `api.trychroma.com`
     direct-connects and times out. DELIVERABLE: wire `EgressProxy.selector()` onto the
     `ChromaRestClient.cloud()` HttpClient builder (code) — the same pattern `VoyageEmbedder`
     already uses. (Or, fallback: allowlist `api.trychroma.com` in squid — pick the code
     path; infra allowlists rot.)
2. **Non-mutation of the legacy stores → downgrade is free** (Gap 2): **DECIDED — the
   upgrade must not mutate the 5.x SQLite or Chroma.** The CLI/daemon never runs
   `bootstrap_version`/migrations or writes `_nexus_version` on a legacy DB used purely as
   a migration source; Chroma stays copy-not-move and is never deleted pre-P4b. Then
   downgrade = reinstall the prior CLI, which reads its own pristine data — no backup, no
   restore, no version-guard surgery, no reverse-migration. Optional pre-upgrade
   auto-backup of the ~300 MB stamped-DB set is belt-and-suspenders only, NOT the rollback
   mechanism. **Invariant (precise):** the main `.db` files (`memory.db`, `.catalog.db`,
   `pipeline.db`) are **content-unchanged** before/after an upgrade + full migration run,
   and the prior CLI opens them with **no `_nexus_version` mismatch / schema error**. NOT
   literal whole-file byte-equality — a read-only WAL open can touch the `-shm`/`-wal`
   sidecars; the test hashes the **main `.db` page content** (e.g. `.dump` or a checksum
   excluding sidecars), not the raw file. (Confirmed: the migration ETLs already open the
   source `?mode=ro` — `catalog_etl.py:193`, `memory_etl.py:120/169` — so the source-open
   side is clean; the stamps come from the daemon/upgrade/doctor paths, gated by Changes
   1–5.) Tested end-to-end: upgrade → migrate → downgrade-to-prior-CLI → old CLI opens
   clean.
3. **Unified config-first auth** (Gap 3): one resolution chain (env > config) for every
   storage command incl. `migrate all`; `--service-url` everywhere.
4. **Edge route-coverage gate** (Gap 4): a go-live check that every migration endpoint
   is reachable for a tenant bearer.
5. **Migration observability** (Gap 5): per-store/per-batch progress + a destination
   count/metric; INFO-level progress events.
6. **SQLite-as-exception ratification** (Gap 7): coordinate with RDR-158; document each
   surviving SQLite use as a justified exception.

## Migration completeness + copy semantics (the objective)

The migration **copies the full local persistent state to the cloud Postgres** — every T2
store (`memory`, `plans`, `taxonomy`, `telemetry`, `chash`, **`catalog`**, aspects,
aspect_queue) **and** the T3 vectors — so the cloud holds a complete copy and is the
canonical substrate (per `directive-postgres-substrate-no-sqlite.md`). **Copy-not-move:**
the local source is read-only and survives verbatim; the cloud is an additive copy, not a
relocation. Completeness is a closing assertion: every source store's row/chunk count
equals the destination's after the run (no silent partial — the `summary.total_failed==0`
gate plus per-store count parity). "All the stuff, copied to the cloud" is the acceptance
bar; a leg that lands a subset is a failure, not a green.

## Non-Goals / Scope Boundaries

- The pgvector/Postgres destination design (RDR-155/156) and the SQLite *retirement
  mechanics* (RDR-158) are not re-litigated here; this RDR is the migration *survivability
  + release-readiness* layer over them.
- The 6.0.0 release-recall decision (yank / un-pin) is operational, tracked separately.

## Decisions

- **Downgrade = NON-MUTATION of legacy stores** (Hal, 2026-06-30; supersedes the earlier
  backup/restore framing). The upgrade must not stamp/migrate the 5.x SQLite or modify
  Chroma; then reinstalling the prior CLI reads pristine data. No backup, no restore, no
  reverse-migration. The invariant is enforced by a **byte-identical-before/after** test
  on the legacy DBs across an upgrade + full migration run. Optional auto-backup is
  insurance only. (Also moots the additive-migrations assumption.)
- **Cloud→cloud creds = client-supplied, ephemeral** (Hal, 2026-06-30): the client passes
  ChromaCloud tenant/db/apiKey in the migration request body; the service holds them in
  memory for the request lifetime only, never persisted, never logged. Test-enforced.
- **The migration copies the FULL local state to the cloud** (Hal, 2026-06-30): every T2
  store + catalog + T3 vectors, copy-not-move, completeness asserted by per-store count
  parity **across ALL tables**: memory, plans, taxonomy (topics + assignments + links +
  meta), telemetry, chash, aspects, aspect_queue, catalog (**owners, documents, links,
  collections, document_chunks**), and T3 collections. Today `_VERIFY_TABLES`
  (`orchestrator.py:43-54`) covers only `catalog.documents` + `catalog.links` — owners,
  collections, and document_chunks are unverified, so a partial copy passes green. That is
  a defect, not the contract: the acceptance bar is parity on **every** table (see Gap 1,
  bead for the `_VERIFY_TABLES` fix).
- **SQLite is exception-only; the managed/service runtime uses none of it as a co-equal
  tier** (Hal, 2026-06-29 directive; Gap 7 ratification). The default and only sanctioned
  managed-runtime T2/T3 backend is the Postgres (pgvector) service. SQLite is a legacy
  substrate being retired, not a co-equal backend, and there are NO new SQLite write paths.
  Every surviving SQLite use is one of these documented, justified exception classes:
  - **Local-mode single-user backend.** The SQLite-backed T2 stores (`db/t2/memory_store`,
    `plan_library`, `telemetry`, `chash_index`, `document_aspects`, `document_highlights`,
    `aspect_extraction_queue`, `catalog*`) and `db/migrations.py`, plus the local-mode
    plumbing that reads them (`t2_daemon`, `hooks`, `health`, `pipeline_buffer`,
    `search_engine`, and similar). In **service mode these are bypassed**: the `Http*` stores
    route every read/write to PG under RLS (the storage-boundary lint enforces no direct
    raw-handle use off the sanctioned path). This is the genuinely-offline / single-binary
    exception. Its eventual consolidation/removal as a *runtime backend mode* is **RDR-158's
    scope** (cf. `nexus-7bomn` / RDR-158 P3, "no sqlite opt-out backend as a supported
    runtime mode"), which this RDR coordinates with rather than duplicates.
  - **Read-only migration source.** The `*_etl.py` readers open the legacy 5.x SQLite
    **read-only** (`mode=ro`) as the immutable upgrade source (Gap 2 non-mutation). One-time,
    never written.
  - **Test isolation.** Unit tests use a tmp-path SQLite + `chromadb.EphemeralClient` so the
    suite needs no network or API keys (project testing convention).
  `retry.py`'s `sqlite3.OperationalError`-"locked" classification is not a SQLite *backend*
  use; it is transient-error handling for the local-mode path above. This decision ratifies
  the directive as RDR-176's acceptance frame. It does not itself remove any backend (that is
  RDR-158). A complete per-file enumeration of the ~44 `import sqlite3` sites and their
  exception-class justification is deferred to RDR-158, where they are the direct subject of
  removal or per-file ratification (e.g. an `import sqlite3` allowlist lint). This register
  defines the exception vocabulary and acceptance frame; RDR-158 applies it per file. The
  category boundaries above are the contract, not the file list, which is illustrative.

## Sequencing (implementation order)

1. **Gap 2 — non-mutation** (pure Python, ~5 files / ~50 LOC + byte-content test).
   Prerequisite for any release advertisement; unblocks safe downgrade. Ship first.
2. **Gap 3 — auth unification** + **Gap 4 — edge route-coverage gate** (incl. the
   completeness `_VERIFY_TABLES` fix, Gap 1a). Blocks any end-to-end managed test.
3. **Gap 1 — T2/catalog bulk endpoints** (Java; `import_batch` via `withTenant`, jOOQ
   batch on the catalog array loop). Blocked on Gap 4 (routes reachable).
4. **Gap 1 — cloud→cloud server-side** (Java + infra; blocked on the 1b credential/egress
   design being locked). Highest-effort; reuses `ChromaRestClient.cloud`.
5. **Gap 5 — observability**, **Gap 6 — retry**, **Gap 7 — SQLite-exception docs**
   (coordinate RDR-158).

## Open Questions

- **Do we keep any offline single-binary SQLite mode, or is the service mandatory?** Drives
  how hard "no SQLite" (Gap 7) is. The one genuinely-open question. (RESOLVED items removed:
  non-mutation feasibility → Research A; backup sizing → moot under non-mutation.)
- Batch size + partial-batch error semantics (whole-batch txn vs per-row-within-batch) —
  settle at planning, not gate-blocking.
- **O1 (verify, not block):** RDR-166 Gap 3 (`resolve_service_config` built `http://` and
  broke TLS for `https://api.conexus-nexus.com`) — empirically the 2026-06-29 dogfood
  reached the `https` endpoint and migrated 124k vectors, so it is fixed in practice;
  confirm the fix landed and add a `§Related` note rather than re-deriving.
