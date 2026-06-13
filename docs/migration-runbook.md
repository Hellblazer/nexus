# Migration-Window Operations Runbook (SQLite/Chroma to Postgres)

Operational narrative for the operator running the next T2 + T3 migration
onto the PG16 + pgvector + nexus-service stack (RDR-152/153/155). The flag
reference lives in [`docs/cli-reference.md` § nx storage](cli-reference.md#nx-storage);
this document is the order of operations, the failure playbook, and how to
read the artifacts. Precedent: the 2026-06-10 production run (115,716
chunks, ~10:46 to 15:05 PT, zero lost, est. $4-6 Voyage; permanent record:
T2 `nexus_rdr/155-production-migration-complete`).

## 0. The guided path: `nx migrate-to-service`

> **Status (RDR-159 P4).** The guided command exists and runs the full flow.
> The two-release deprecation-window cadence is documented in the next section.
> Sections 1-7 below remain the authoritative manual order of operations; the
> guided command sequences exactly those same primitives. The consumer-facing
> entry is the conexus `/upgrade` slash-command — a thin veneer over this same
> `nx migrate-to-service` that owns no migration logic.

`nx migrate-to-service` wraps and sequences everything in sections 2-6 into one
survivable command: detect the Chroma footprint, set the cross-process
migration sentinel (reads degraded-LOUD, never bare-empty), quiesce background
indexing, pre-gate per-collection model support, run T2 `migrate all` then T3
vectors for every detected leg, validate (taxonomy + counts + manifest
orphans), and unlock on a clean verdict. On a validation block it leaves the
migrated copy in place (sentinel `migrated-failed`) and OFFERS — never
auto-invokes — `nx storage migrate vectors --rollback` (§6); the Chroma source
is untouched. Preview first with `nx migrate-to-service --dry-run`. Flag
reference: [`cli-reference.md` § nx migrate-to-service](cli-reference.md#nx-migrate-to-service).

## 0.1 The two-release deprecation window (release cadence)

ChromaDB is retired across **two** releases, never one. The ordering is an
invariant, not a preference: the release that *deletes* the Chroma read path
can only ship *after* a release that gives users a way off Chroma.

| | **Release N** (migration-capable) | **Release N+1** (Chroma deletion) |
|---|---|---|
| Upgrade paths | BOTH ship: cloud/Voyage (1024-dim) and local-only/ONNX (384-dim) | (already migrated) |
| Migration tool | `nx migrate-to-service` + the `/upgrade` veneer ship | **deleted** with the Chroma read path |
| Chroma read path | present (rollback target, immutable) | **deleted** (RDR-155 P4b, bead `nexus-g37fr`) |
| User action | run the guided upgrade any time in the window | none (must already be on the service) |

**Why the order is load-bearing.** RDR-155 P4b deletes
`src/nexus/migration/vector_etl.py` (and the rest of the Chroma read path)
wholesale. The migration tool *reads from Chroma* to copy chunks into pgvector,
so **deleting the Chroma read path deletes the migration tool itself.** A
release that deleted Chroma without a prior migration-capable release would
strand every not-yet-migrated user with no upgrade path and no rollback target.
Hence: ship the tool in N, delete it in N+1, and never collapse the two.

**The window between N and N+1** is the user's migration runway. Throughout it
the Chroma sources (local and ChromaCloud) stay **immutable** (copy-not-move,
RF-5, never modifies the source), so a blocked or regretted migration is fully
recoverable via `nx storage migrate vectors --rollback` (§6). Once N+1 ships and
the Chroma read path is gone, rollback is gone with it; that is the whole point
of giving the window first. The window must be long enough for the user base to
actually migrate before N+1 removes the escape hatch.

**Gating the lift of `nexus-luxe6`** (the standing release blocker: develop is
unreleasable until the service-stack install + user-migration story ships). The
blocker lifts only when ALL of the following hold; do not close it on RDR-159
completion alone.

**RDR-159 phase deliverables** (satisfied when Phase 4 closes; verify the
artifacts exist, not merely that the beads are marked done):

1. The RDR-159 E2E oracle (bead `nexus-ue6g7.28`) is green across all five
   scenarios: cloud/Voyage, local-only/ONNX, the unsupported-model block, the
   forced-failure rollback, AND the two-leg-simultaneous run (local + cloud
   migrated in one invocation, validation counting both legs via the composite
   read client). The fifth scenario closes the only known integration gap in
   the multi-leg path (unit-covered by `test_two_leg_composes_collections_and_dims`,
   no E2E exercise until `.28` adds it).
2. This runbook (bead `nexus-ue6g7.26`) is in place: the operator narrative and
   this cadence. This is the act of authoring the document you are reading; it
   is a phase prerequisite, not an independently re-checkable runtime gate.

**External gates** (NOT engine code; not satisfied by merging RDR-159; must be
true in the world and surfaced explicitly at closure):

3. conexus `xr7.8.9` production-scale recall / hybrid-parity go-live. The served
   backend must be proven at production scale.
4. The deprecation-window cadence is actually running, i.e. a migration-capable
   release N has shipped and the window is open, so N+1 (the Chroma deletion)
   has a release-N predecessor to follow.

> **Do not start RDR-155 P4b (the Chroma deletion) until release N has shipped
> and the window has opened.** P4b is release N+1 by construction. Starting it
> early deletes the migration tool before users can run it.

## 1. Before you start: the quiescent window

The vector ETL's post-write verification compares an exact source count
against an exact target count per collection
(`src/nexus/migration/vector_etl.py`, `migrate_collections`): "concurrent
serving writes into the same collection during the ETL would inflate the
target count and read as a (conservative) failure." A mismatch is a
`failed` migration, never a green one. Rollback runs the same comparison
arithmetic, so the whole window (migrate, validate, and any rollback) must
be quiescent.

Stop everything that writes: other Claude Code sessions (every session
hosts an `nx-mcp` process with an in-process aspect worker writing T2, and
MCP `store_put` writes T3), any `nx index` runs ("Run the migration with
indexing paused", per the docstring), and anything else driving the
service's vector upsert path. On the T2 side, copy-not-move means rows
written to the SQLite source after the ETL's read pass simply are not
migrated: stop the writers so the snapshot is complete.

Then verify the stack is healthy:

```
nx daemon service start        # if not already running
nx daemon service status
```

`status` is the single is-the-stack-healthy surface
([cli-reference § nx daemon service](cli-reference.md#nx-daemon-service-start--stop--status)).
Check, in order:

- The lease (host, port, JAR pid, generation) and a passing live `/health`
  probe (`{"status":"ok","db":"up"}`).
- The PG cluster block: up, pgvector version installed, `pg_credentials`
  path resolvable.
- The `/version` handshake: `app_version`, `schema_latest_id`,
  `schema_changeset_count`, and critically `embedding_mode`: it must say
  `voyage`. In `onnx-local` mode the service refuses every `voyage-*`
  collection with HTTP 422 (nexus-pebfx.2 fail-loud dispatch), so a
  migration started in the wrong mode fails loudly per batch instead of
  silently embedding the wrong model. If the key did not resolve the
  supervisor logs a WARN naming the consequence; fix with
  `nx config set voyage_api_key` (chain: explicit env > `VOYAGE_API_KEY` >
  `config.yml` credentials).
- No stale-JAR warning (running JAR differs from the installed sidecar).
  Install the intended JAR first via `nx daemon service install-jar`; the
  schema-skew gate refuses a JAR older than the database schema at spawn
  (nexus-pebfx.4).

The T2 ladder commands read the service endpoint from the environment
(`NX_SERVICE_PORT` + `NX_SERVICE_TOKEN` are required; the per-store
commands error with "NX_SERVICE_TOKEN is required for storage migrate
memory" when unset, see `src/nexus/db/t2/http_memory_store.py`
`_resolve_config`). The vectors command needs neither: it resolves
`{url, token}` from the supervisor's ServiceRegistry lease, env as
override (nexus-pebfx.1; addr file `~/.config/nexus/storage_service_addr.<uid>`).

## 2. The two migrations

Two independent ETL families land in disjoint Postgres tables. Run the T2
ladder first: the cutover validation in section 5 joins manifest rows
(written by the catalog ETL) against chunk rows (written by the vector
ETL), and the 2026-06-10 production run proved that running it with empty
catalog tables produces a vacuous pass.

### T2 ladder

```
nx storage migrate all [--report PATH]
```

Runs all eight store ETLs in the RDR-152 ladder order
(`LADDER_ORDER` in `src/nexus/migration/etl_registry.py`):
`memory, plans, telemetry, taxonomy, aspects, chash, catalog,
aspects_queue`. Memory is first (smallest, fastest validation); catalog
is second-to-last because it is graph-heavy: every other store's FK
targets must exist before its links land. `aspects_queue` runs AFTER
catalog: `fk_aspect_queue_catalog_doc` requires `catalog_documents`
populated first (first-run FK safety, nexus-iy5se). A store-level crash
is recorded as a `failed` issue and the run continues, so the report
covers every store it attempted.

One shared `IssueCollector` spans the run and emits ONE
`migration-report.json` (default
`~/.config/nexus/migration-reports/migration-<id>.json`; a run always
produces an artifact, even on a mid-run crash). The gate predicate is
`summary.total_failed == 0`. Post-run count verification (pg_count >=
report written, via psql against the local nx-managed cluster) is recorded
in the artifact as `"verification"`: `verified`, `mismatch`, or a loud
`VERIFICATION INDETERMINATE` warning when psql/credentials cannot resolve
(never a silent skip).

Note: `aspects` has no standalone command; it runs only via `migrate all`.

### T3 vectors

```
nx storage migrate vectors --dry-run            # local leg, count only (no service needed)
nx storage migrate vectors                      # local leg (~/.config/nexus/chroma)
nx storage migrate vectors --cloud --dry-run    # cloud leg, count only
nx storage migrate vectors --cloud              # ChromaCloud leg
```

Run BOTH legs, separately: "an ETL with only one leg is a silent
half-migration" (`vector_etl.py` module docstring). Chunk text, chash, and
metadata transfer byte-verbatim; the service re-embeds server-side
(vector-identity decision (a), bead nexus-unp61); collection names are
preserved verbatim so `topic_assignments.source_collection` references
stay valid. Per-collection progress lines are flushed live; a
failures-first summary table closes the run. `--collections A,B` pins a
subset; `--dry-run` only counts source chunks and never touches the
service.

## 3. Mid-run failure: Voyage 429 / outage

A batch-level failure (rate limit, timeout, service outage) shows up as a
per-collection `failed` line and the run continues to the next collection:

```
failed        docs__1-16__voyage-context-3__v1: source=812 written=600 (94.2s) — upsert failed after 600 chunks: ...
```

(structlog event `vector_etl_upsert_failed`). The summary table sorts
failures first, and the command exits non-zero:

```
Error: migration is NOT clean — fix the failed/skipped collections above and re-run (idempotent).
```

Re-running is safe, twice over: the server upserts on
`(tenant, collection, chash)` so already-landed chunks are converged, not
duplicated, and copy-not-move means the Chroma source was never modified,
so the re-read sees exactly the same data. The exact re-run is the same
command, optionally pinned:

```
nx storage migrate vectors --cloud --collections docs__1-16__voyage-context-3__v1
```

Production precedent: 46 of 49 cloud collections were clean on the first
full run; 3 failed deterministically (two on a 120s client timeout against
slow CCE batches, fixed by a 600s per-op upsert timeout; one on 62
NUL-bearing chunks, fixed by service-side sanitization, PR #1152) and were
re-run to a clean 49/49, EXIT=0. NUL delta: the service strips 0x00 bytes
before embed+bind (event `upsert_nul_sanitized`), so for exactly those
rows `sha256(stored_text)[:32] != chash`; the chash is carried source
identity, never recomputed, so manifest joins, rollback, dedup, and
re-migration are unaffected (affected-chash list: T2
`nexus_rdr/155-nul-sanitization-delta`).

The T2 ladder is equally idempotent ("ON CONFLICT DO NOTHING" per the
`migration-report show` failure hint): repair the cause, re-run
`nx storage migrate all`, and a fresh report supersedes the red one.

## 4. Reading the outcome

T2 report issues carry two enums, never mixed
(`src/nexus/migration/migration_report.py`): `class` (what is wrong:
`orphan_parent`, `identity_mismatch`, `format_anomaly`, `soft_dangler`,
`unexpected`) and `action` (what the ETL did). Severity is a function of
action:

| action | severity | meaning |
|---|---|---|
| `failed` | 4 | gate-blocking, data not migrated and the run is red |
| `skipped` | 3 | data not migrated |
| `flagged` | 2 | imported with advisory |
| `handled` | 1 | normalized on the way through |
| `schema_corrected` | 0 | schema fixed; data correct |

`summary.by_action` is the gate-facing rollup; `summary.total_failed == 0`
is the gate predicate (also the RDR-152 Phase-4 SQLite-deletion gate). The
triage surface is:

```
nx storage migration-report show <path>
```

It prints the migration window, the recorded verification verdict,
`max_severity`, the by-action rollup, per-issue lines severity-descending
(class/action/count/sample/reason), and `GATE: PASS` or `GATE: FAIL` with
a non-zero exit (scriptable).

Vector legs use per-collection statuses instead (no JSON artifact; the
summary table plus exit code is the record):

| status | red? | meaning |
|---|---|---|
| `migrated` | no | copied and count-verified exactly |
| `dry-run` | no | counted only |
| `skipped-empty` | no | non-conformant name AND 0 source chunks: nothing can be lost (nexus-pebfx.3) |
| `excluded` | no | `tuples__*` prefix: session-ephemeral, dies with Chroma at P4b; excluded from default enumeration, reported never silent; naming it via `--collections` still acts on it |
| `skipped` | yes | non-conformant name WITH data: partial-migration-never-green |
| `failed` | yes | unreadable source, upsert failure, or post-write count mismatch |

A red exit from either command means the run is not clean and must be
re-run after triage; it never means data was destroyed (both ETLs are
copy-not-move).

## 5. Cutover validation

After BOTH the catalog ETL and both vector legs are clean, run the
manifest checks. These are direct SQL by design (P2.1 constraint, recorded
on nexus-unp61): never `PgVectorRepository.fetchDocumentChunks`, which
fails loud on partially-migrated documents.

The checks are now **stored functions** in the `nexus` schema (catalog-004,
RDR-156 P2, bead nexus-70r3c.9), callable directly via psql. Connection
details come from `~/.config/nexus/pg_credentials`; the port is also shown
by `nx daemon service status`.

Sequence (backfill first: orphan rows with `collection IS NULL` are
pre-backfill state, not orphans):

```
psql -h 127.0.0.1 -p <PG_PORT> -U <admin> -d nexus \
  -c "SELECT nexus.manifest_backfill();"

psql -h 127.0.0.1 -p <PG_PORT> -U <admin> -d nexus \
  -c "SELECT count(*) FROM nexus.manifest_orphans(384);"

psql -h 127.0.0.1 -p <PG_PORT> -U <admin> -d nexus \
  -c "SELECT count(*) FROM nexus.manifest_orphans(768);"

psql -h 127.0.0.1 -p <PG_PORT> -U <admin> -d nexus \
  -c "SELECT count(*) FROM nexus.manifest_orphans(1024);"
```

Or in a single session (use a transaction to avoid schema-state side effects):

```
psql -h 127.0.0.1 -p <PG_PORT> -U <admin> -d nexus <<'SQL'
SELECT nexus.manifest_backfill();
SELECT count(*) AS orphans_384  FROM nexus.manifest_orphans(384);
SELECT count(*) AS orphans_768  FROM nexus.manifest_orphans(768);
SELECT count(*) AS orphans_1024 FROM nexus.manifest_orphans(1024);
SQL
```

Expected: the backfill touches only `collection IS NULL` rows (idempotent
re-run returns 0), and each orphan query returns ZERO rows. An orphan row
is a manifest entry that does not resolve to a migrated chunk. Caveat
repeated on purpose: zero rows against empty catalog tables is a vacuous
pass; run this only after the T2 ladder's catalog leg.

**Legacy note**: the Python-generated equivalents `manifest_backfill_sql()`
and `manifest_orphan_sql(dim)` in `src/nexus/migration/vector_etl.py` are
deprecated and kept only because bead nexus-g37fr (RDR-155 P4b) will
delete that module wholesale. Use the stored functions above.

Per-collection chunk counts (eyeballing a migration or comparing against a
source inventory) come from the `nexus.collection_vector_stats` view
(catalog-005, RDR-156 P3, bead nexus-70r3c.12) — NOT hand-assembled
`count(*)` over the three `chunks_<dim>` tables:

```
psql -h 127.0.0.1 -p <PG_PORT> -U <admin> -d nexus \
  -c "SELECT * FROM nexus.collection_vector_stats ORDER BY tenant_id, collection;"
```

One row per `(tenant_id, collection, dim)` with `chunk_count` and
`last_write` (max `created_at`). Caveat for migration parity: the view is
TOMBSTONE-FILTERED (live chunks only). On a freshly migrated cluster with
no trashed documents it equals the raw count; if documents have been
trashed since, an exact source-vs-target comparison must use the ETL's own
verification (raw counts) — the divergence is the view's purpose, not a
bug. The same data is served to clients at `GET /v1/vectors/stats` and is
what `nx collection list` prints.

Optional check (`verify_taxonomy_consistency`, same module): every
`topic_assignments.source_collection` must resolve to a migrated
collection. Production returned 28 unresolved values, all verified as
pre-existing T2 drift (absent from the Chroma source too, the RDR-108
string-copy-orphan class), not migration loss.

## 6. Rollback

```
nx storage migrate vectors --rollback [--cloud] [--collections A,B]
```

`rollback_collections` deletes from pgvector exactly the chashes present
in the source Chroma collections, nothing else. The source IS the rollback
manifest: copy-not-move keeps it immutable, so the id set at rollback time
equals the id set at migration time, and the source itself is never
modified. Two fail-loud guards refuse to lie:

1. Zero-resolution guard: if the target holds chunks and the source has
   chashes but not a single lookup resolved, the lookup layer may have
   swallowed transport errors; it raises rather than reporting a clean
   "deleted 0".
2. Post-delete count guard: the target count must move by exactly the
   number deleted; if deletes were swallowed by the transport layer, it
   raises.

Both error messages say it explicitly: verify the service and re-run,
rollback is idempotent. The standing fact (T2
`nexus/release-boundary-since-p4a`): the Chroma sources (local and
ChromaCloud) are untouched and remain a free rollback target until RDR-155
P4b deletes the Chroma read path, which ships only in the second release
of the deprecation window.

## 7. If the stack dies mid-run

The stack never dies silently; the evidence lives in the persistent logs
documented in
[cli-reference § nx daemon service, "Observability"](cli-reference.md#nx-daemon-service-start--stop--status)
(under `~/.config/nexus/` unless noted): `logs/storage_service.log`
(supervisor lifecycle: start/exit breadcrumbs, jar exit codes, restart
attempts, PG recoveries), `logs/storage_service_jar.log` (the Java
service's stdout/stderr), `logs/storage_service.crash.log` (pre-startup
failures of the detached supervisor), and `<pg_data>/pg.log` (the
nx-managed Postgres cluster).

The absence convention: a supervisor death WITHOUT a
`storage_service_supervisor_exit` breadcrumb in `storage_service.log`
means it was killed, not that it chose to exit; check the jar log tail and
`pg.log` next. Once `nx daemon service status` is green again, re-run the
interrupted command: both ETL families are idempotent, and a collection
interrupted mid-upsert re-converges on `(tenant, collection, chash)`.
