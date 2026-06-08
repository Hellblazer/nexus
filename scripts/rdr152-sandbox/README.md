# RDR-152 Acceptance Sandbox Harness

Fully isolated Postgres + Chroma + Java service stack for de-risking
Phase-4 destructive deletion.  Prod is never written.

## Prerequisites

- PostgreSQL 16 binaries (Homebrew: `brew install postgresql@16`)
- Java >= 17 on PATH or `JAVA_HOME` set
- Maven (`mvn`) for the service build
- `uv` (Python package manager)
- The repo root must have `service/target/nexus-service-1.0-SNAPSHOT.jar`
  (built automatically by `up.sh` if missing)

## Workflow

### 1. Bring the sandbox up

```bash
cd scripts/rdr152-sandbox
./up.sh
```

Optional: override the sandbox location:

```bash
SANDBOX_HOME=/tmp/my-sandbox ./up.sh
```

`up.sh` will:

1. Enforce the **hard prod-touch guard** (abort if sandbox would overlap prod).
2. Provision an isolated Postgres cluster via `pg_provision.provision()` —
   creates a fresh cluster under `$SANDBOX_HOME/.config/nexus/postgres` at a
   free ephemeral port, creates the `nexus` database, and creates the
   `nexus_admin` (DDL) and `nexus_svc` (DML / FORCE RLS) roles with random
   passwords written to `$SANDBOX_HOME/.config/nexus/pg_credentials`.
3. Build the Java service jar (skipped if already built).
4. Start the service with `NX_CHROMA_MODE=local`, pointing at an isolated
   Chroma data directory.  **The service self-applies Liquibase at startup**
   (the full net63 two-role topology: `nexus_admin` runs DDL, `nexus_svc`
   does DML under FORCE RLS, all 52 changesets).  This is the end-to-end
   proof of the `.31` public-schema grant fix.
5. Wait for `/health 200`.
6. Write `$SANDBOX_HOME/sandbox.env` with all `NX_*` variables needed for
   clients and `cc-sandbox` to point at the sandbox instead of prod.

### 2. Seed from prod (optional, read-only)

```bash
./prod-copy.sh
```

`prod-copy.sh` will:

1. Record prod file mtimes before touching anything.
2. Copy `~/.config/nexus/chroma` into the sandbox Chroma directory via
   `cp -R` (no write to source).
3. Run all five `nx storage migrate` ETLs (memory, plans, telemetry, taxonomy,
   chash) pointing at the prod SQLite file as source and the sandbox service
   as destination.  Each ETL opens the source in `mode=ro` (OS-level
   read-only; the fidelity-preserving `/import` endpoints are idempotent).
4. Assert prod file mtimes UNCHANGED after the copy.
5. Assert prod file mtimes UNCHANGED after the copy — including `.db`, `-shm`,
   and `-wal` WAL-mode sidecar files.
6. Verify sandbox counts match prod exactly (strict `==`) for stores that copy
   completely.  Tables with known service-side gaps (nexus-0a7xc, nexus-5gaj7)
   are verified against their known-gap count and annotated with the tracking
   bead so the assertion tightens automatically when the gaps are fixed.

### 3. Check status

```bash
./status.sh
```

Reports:

- `/health` ping
- `DATABASECHANGELOG` row count (proves Liquibase ran)
- `nexus_admin` and `nexus_svc` role existence
- Per-table row counts from Postgres
- Chroma collection / embedding counts

### 4. Tear down

```bash
./down.sh           # graceful stop (service SIGTERM, pg_ctl stop -m fast)
./down.sh --purge   # stop + delete $SANDBOX_HOME entirely
```

## Isolation guarantees

- **Prod-touch guard (hard)**: `up.sh` computes `realpath` of both the sandbox
  config dir and `~/.config/nexus`; if they are equal or the sandbox is under
  prod, the script aborts before doing any work.
- **Config path isolation**: `NEXUS_CONFIG_DIR`, `XDG_CONFIG_HOME`, and
  `NX_CONFIG_HOME` are all redirected into `$SANDBOX_HOME/.config`; no nx/
  config path can resolve outside the sandbox.
- **Separate Postgres cluster**: `pg_provision.provision()` creates a cluster
  under `$SANDBOX_HOME/.config/nexus/postgres` at a fresh ephemeral port.
  All credentials are sandbox-specific.
- **Read-only ETL source**: all T2 SQLite ETLs open the prod file with
  SQLite URI `mode=ro`; the Chroma copy uses `cp -R` with no `--archive`
  timestamp-update flags.  All `sqlite3` CLI invocations on prod paths use
  `--readonly` so the WAL-mode `-shm` sidecar is never updated.
- **Chroma copy is a live-prod snapshot** (best-effort): `cp -R` takes the
  Chroma directory while the prod MCP may be writing new embeddings.  If the
  sandbox produces odd query results, stop the prod MCP (`nx daemon stop`),
  re-run `prod-copy.sh`, then restart the prod MCP.  For Phase-4 smoke testing
  the snapshot is sufficient; the exact embedding counts are asserted post-copy.
- **Down --purge safety guard**: `down.sh --purge` refuses to `rm -rf` unless
  the target contains a harness marker file (`sandbox.env` or `service.pid`),
  has >= 2 path components, is not `$HOME`, and is not under prod
  `~/.config/nexus`.  This prevents `SANDBOX_HOME=~ ./down.sh --purge` from
  deleting the home directory.
- **Separate Chroma**: `NX_CHROMA_PATH` points at
  `$SANDBOX_HOME/.config/nexus/chroma`, not the prod path.
- **Separate service token**: `NX_SERVICE_TOKEN` is a freshly generated
  random hex string.

## Gap notes

- **catalog.db / catalog/ directory**: The catalog (SQLite in
  `~/.config/nexus/catalog/`) is not included in the `nx storage migrate`
  ETL set.  `prod-copy.sh` copies the Chroma data (which includes T3
  collections) but does not ETL the catalog graph.  A catalog ETL bead
  is a known future item.  For Phase-4 smoke testing (vector ops, DML) the
  current ETL coverage is sufficient.
- **aspect_extraction_queue / document_aspects / document_highlights**:
  These tables exist in `memory.db` but do not yet have a dedicated ETL
  in `nx storage migrate`.  They are not required for Phase-4 decommission
  smoke validation.
- **CHANGELOGLOCK crash recovery**: if the JVM is kill-9'd mid-migration,
  `DATABASECHANGELOGLOCK` may remain held (Liquibase waits up to 5 minutes
  then fails).  Recovery: connect as `nexus_admin` and run
  `TRUNCATE public."DATABASECHANGELOGLOCK";`.

## Environment reference

After `source $SANDBOX_HOME/sandbox.env`, the following variables point at
the sandbox:

| Variable | Purpose |
|---|---|
| `NEXUS_CONFIG_DIR` | All nx config paths resolve here |
| `NX_SERVICE_URL` | Sandbox service base URL |
| `NX_SERVICE_TOKEN` | Sandbox bearer token |
| `NX_DB_URL` | nexus_svc JDBC URL |
| `NX_DB_ADMIN_URL` | nexus_admin JDBC URL |
| `PG_PORT` | Sandbox Postgres port |
| `NX_CHROMA_PATH` | Sandbox Chroma data dir |
| `NX_STORAGE_BACKEND` | Set to `service` |
