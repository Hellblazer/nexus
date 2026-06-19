# RDR-157 to conexus RDR-001 handoff: distribution & install primitives

**Status:** delivered 2026-06-16 (RDR-157 P5, bead `nexus-vwvv5.21`)
**Producer:** nexus engine, RDR-157 (End-User Distribution & Installation)
**Consumer:** conexus RDR-001 (upgrade-orchestration / multitenant cloud service), tracked engine-side by epic `nexus-w5v8j`
**Release gate:** this epic is one of the four conditions on release-blocker `nexus-luxe6` (see the Release-blocker readiness section)

RDR-157 builds the per-OS/arch distribution for the RDR-152/155 storage stack:
a GraalVM native-image `nexus-service` binary plus, for the local distribution,
a ship-alongside relocatable PostgreSQL 17 + pgvector bundle, brought to a
serving state by a single `nx init --service`. This document enumerates the
primitives that conexus RDR-001's upgrade-orchestration consumes, each with its
entry point and contract. **Nothing here is implemented for RDR-001; RDR-001 is
the consumer.**

## Primitives

### 1. Native-image service binaries + sha256 manifest (P2)

- **Entry point:** GitHub Releases assets, one native binary per target
  {`linux-amd64`, `linux-aarch64`, `mac-arm64`}, plus a `sha256` manifest.
  Built by the per-platform matrix in `.github/workflows/engine-service-release.yml`.
- **Contract:** download the target's binary, verify against the sha256 manifest,
  mark executable. The binary is self-contained (no JVM/JRE). Windows is a
  separate release-N+1 follow-on; `mac-x64` is out of scope (owner call).
- **Provenance (cosign/Sigstore, `nexus-1odsm`):** each asset additionally
  carries a keyless cosign signature bundle `<asset>.cosign.bundle`. The
  publisher (`engine-service-release.yml`) signs and self-verifies before
  upload. **Consumers MUST verify before use:**
  ```
  cosign verify-blob <asset> --bundle <asset>.cosign.bundle \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp \
      'https://github.com/Hellblazer/nexus/.github/workflows/engine-service-release.yml@refs/tags/engine-service-v.*'
  ```
  conexus deploy/engine must run this check before `docker run` (track in
  conexus `ECR_PUSH.md`, which already verifies the pushed image's signature).
- **Consumer use:** upgrade-orchestration fetches the binary for the host
  platform during an install/upgrade, verifies the cosign bundle + sha256, and
  positions it (see primitive 4).

### 2. Relocatable PG17 + pgvector bundle (P3.1, ship-alongside)

- **Entry point:** `nexus-pg-<platform>.txz` (Strategy B: PostgreSQL 17 built
  from source with pgvector, lean configure flags). Built by
  `scripts/build_pg_bundle.sh`; packaged in CI (`.github/workflows/ci.yml`,
  `ca3-pgvector-bundle` matrix).
- **Code:** `src/nexus/db/pg_bundle.py` provides `locate_bundle_archive`,
  `extract_bundle` (path-traversal-safe, atomic completion marker),
  `ensure_pg_bundle`, and `extracted_bin_dir`. `NEXUS_PG_BUNDLE` overrides the
  archive location (set-but-missing fails loud).
- **Contract:** the bundle extracts once (idempotent) to
  `<config_dir>/pg-bundle`. `pg_provision` is relocation-aware: it resolves
  `sharedir`/`pkglibdir` relative to the binary, not via `pg_config`'s
  build-time absolute paths (bead `nexus-1e205`). The CA-2 verdict is
  ship-alongside ({binary, `pg-<plat>.txz`}), not in-binary embed
  (`nexus-vwvv5.11`); the bundle is ~5.74 MB compressed, with a CI size
  tripwire guarding the assumption.
- **Consumer use:** the local distribution archive ships the binary alongside
  the matching `pg-<plat>.txz`; the cloud distribution ships the binary alone.

### 3. Local first-run provisioning path (P3.4)

- **Entry point:** `nx init --service` (local mode) drives
  extract, `initdb`, provision, serve. Bundle binaries are discovered by
  `pg_provision.discover_pg_binaries` (env override, then extracted bundle,
  then fixed candidates, then PATH), so every caller (init, the daemon's
  PG-restart path) finds the bundle on a bundle-only machine.
- **Contract:** the local Java service connects ONLY to a local Postgres
  (never a remote PG). The pgvector >= 0.8 (`iterative_scan`) floor is validated
  locally by `pg_provision.check_pgvector_available`.

### 4. `nx init --service` one-command collapse + native-binary launch (P4.1)

- **Entry point:** `nx init --service` (LOCAL mode). Lifecycle commands:
  `nx daemon service {start,stop,status}` (`stop --with-pg` also stops the
  cluster). Programmatic: `start_storage_service()` / `stop_storage_service()`
  in `src/nexus/daemon/storage_service_daemon.py`.
- **Contract:** idempotent and individually re-runnable. The collapse is
  preflight, provision PG (local), provision the bge-768 ONNX, start the
  service, `/health` 200, publish the discovery lease. A live lease
  short-circuits a re-run (same endpoint). Every step fails loud with a remedy,
  never a traceback.
- **Native-binary launch:** the supervisor execs the native binary when present
  (via `NEXUS_SERVICE_BIN`, where set-but-missing / non-executable fails loud,
  or the well-known `<config_dir>/service/nexus-service`) and falls back to
  `java -jar` for dev. Configuration reaches the service entirely via the
  environment (`NX_DB_*`, `NX_SERVICE_PORT`, `NX_SERVICE_TOKEN`,
  `NX_VOYAGE_API_KEY`), so native and JAR launches are argv-only variants.
  Schema-skew gating is JAR-only (a native binary bakes its changelog at build
  time).
- **Consumer use (binary positioning):** the distribution launcher / upgrade
  orchestration positions the native binary at the well-known path or sets
  `NEXUS_SERVICE_BIN`, then runs `nx init --service`. This positioning is the
  consumer's responsibility (see `_find_service_binary` in
  `storage_service_daemon.py`).

### 5. Local-mode embedder: bge-768 (RDR-160)

- **Entry point:** `src/nexus/db/service_bge_model.py` provides
  `fetch_service_bge_onnx()` (fetches the STANDARD fp32 ONNX the Java service
  reads, NOT fastembed's fused export), `service_bge_model_present()`, and
  `service_bge_model_dir()`. Provisioned by `nx init --service` (fail-loud: the
  Java service cannot boot without the model).
- **Contract:** a `--service` install routes every collection through the Java
  service's bge-768 (768-dim) embedder; `minilm-384` is non-operative on the
  service T3 path and gets an advisory. (`nexus-jrrve` is closed as subsumed by
  RDR-160.)

### 6. Cloud remote-validation: owned by conexus RDR-001

- The cloud distribution has no local stack: the client (`nx` CLI + local
  MCP server) points at the managed service (`api.conexus-nexus.com`) over HTTP
  and validates reachability + capability/version compatibility, failing loud
  on mismatch. There is no client-side remote-pgvector SQL check; the pgvector
  floor is the managed service's server-side concern.
- **Entry point (client probe):** `nx service probe` and the managed-endpoint
  config + capability probe shipped under RDR-001 (`nexus-vwvv5.12`, moved out
  of RDR-157 by the topology correction PR #1199).
- This is RDR-001 territory and listed here only to mark the boundary: RDR-157
  delivers the LOCAL native-binary + embedded-PG distribution; the cloud client
  path is RDR-001's.

### 7. Cross-model migrate: legacy minilm-384 → service bge-768 (RDR-162)

The upgrade case RDR-001 orchestrates for an existing user: a pre-RDR-160
install holds `minilm-384` (384-dim) collections the bge-768 service cannot
serve. RDR-162 makes migrating them a rehearsal-proven primitive.

- **Entry point:** `nx migrate-to-service` (`src/nexus/commands/migrate_cmd.py`
  → `nexus.migration.driver.run_guided_upgrade`). `--dry-run` previews the
  footprint without moving data.
- **Mode (the contract):** a collection whose embedding model the service does
  not wire (e.g. `minilm-384`), with a conformant four-segment name and present
  chunk text, is **cross-model migrated**: the ETL reads the SOURCE collection's
  **stored chunk text** (model-agnostic) and upserts it into a TARGET collection
  whose model segment is the service model (`bge-768`); the service re-embeds the
  text with bge-768. The target name is the source name with only the model
  segment swapped (`vector_etl.cross_model_target_name`). This is a **single
  stage** — re-embed-on-migrate, NOT a two-stage chain. Voyage collections with
  no key, and non-conformant names, are NOT remapped — they BLOCK with a
  credential / re-index diagnostic.
- **Reference remap:** after the target verifies populated, the catalog/topic
  `source_collection` / `physical_collection` references are re-pointed
  source → target (`collection_rename.remap_collection_references`: the T2
  reference cascade raises on failure; the catalog cascade is fail-open).
- **Idempotency:** the service upsert keys on `(tenant_id, target_collection,
  chash)`; `chash = sha256(chunk_text)[:32]` is identical across the re-embed, so
  a re-run resumes a partial target rather than duplicating.
- **Partial-failure disposition (copy-not-move, RDR-155 RF-5):** the source
  Chroma collection is **never mutated**, so any failed migrate is cleanly
  re-runnable from the untouched source. The reference remap is the one mutation
  and is ordered STRICTLY AFTER the target verifies populated (mirror RDR-144
  reindex-first / delete-after-verify), so a mid-migrate failure never leaves
  dangling references. A per-collection remap failure demotes that leg → the run
  refuses partial success and leaves the `migrated-failed` sentinel (reads
  degrade-loud); re-run to converge.
- **BOUNDARY — not to be confused with `embed_migrate`:** `nexus.db.embed_migrate`
  (under `db/`) is the LOCAL-only 384 → 768 re-embed for a user **staying on
  Chroma** (no service). It re-reads SOURCE FILES and therefore CANNOT upgrade
  `sourceless` collections (MCP `store_put` notes with no backing file). The
  RDR-162 service-migration path re-embeds STORED TEXT and so DOES cover
  sourceless notes. RDR-001 orchestration uses the service path
  (`nx migrate-to-service`), never `embed_migrate`.
- **Consumer use:** RDR-001 upgrade-orchestration invokes `nx migrate-to-service`
  (after the service is provisioned + serving per primitives 3–5). A clean run
  reports `Migration VERIFIED and unlocked`; a block leaves `migrated-failed`
  with rollback offered (`nx storage migrate vectors --rollback`, source intact).
- **Reference flow:** `tests/e2e/migration-rehearsal/run.sh` proves the whole
  chain soup-to-nuts in a container (install → provision → serve → seed legacy
  minilm-384 incl. a sourceless note → cross-model migrate → validate → rollback-
  safe).
- **Known residual (fail-open):** the cross-model catalog reference remap
  (`/v1/catalog/collections/rename`) currently 500s server-side (the upsert
  pre-registers the bge target, so the catalog rename's `collection_registry`
  UPDATE collides under the RDR-156 `ON UPDATE CASCADE` FKs). It is fail-open —
  the migration still verifies + unlocks; catalog `physical_collection` refs may
  be stale until `nx catalog rebuild`. Tracked engine-side (handoff
  `engine_service/HANDOFF-catalog-rename-500-cross-model-collision`); RDR-001
  orchestration should run `nx catalog rebuild` after a cross-model migrate until
  the engine fix lands.

## Fresh-machine E2E (P4.2)

`tests/e2e/release-sandbox.sh service` proves a fresh machine reaches serving
with zero manual steps for LOCAL mode: position the artifact, `nx init
--service`, assert `/health == ok`, assert an idempotent re-run returns the same
endpoint, then `stop --with-pg`. Consumers can use it as the reference flow for
what "installed and serving" means.

## Release-blocker readiness (`nexus-luxe6`)

RDR-157 epic close satisfies condition (a) of the release gate: the engine-side
distribution & install story. `nexus-luxe6` additionally requires:

- (b) conexus RDR-001 upgrade-orchestration ships (this handoff is its input);
- (c) conexus xr7.8.9 recall/hybrid-parity go-live;
- (d) the two-release deprecation window, where release N ships both paths plus
  the bundled migration tool, and the RDR-155 P4b Chroma deletion ships only in
  release N+1 (it deletes the migration tool itself).

RDR-157 close is necessary but not sufficient for `nexus-luxe6`; it removes the
engine-side blocker and hands the install/upgrade primitives above to RDR-001.

## Deferred (tracked, not RDR-157 acceptance)

- A dedicated CI `service-e2e` job (build native + PG-from-source + bge + run
  the harness). The harness exists and is live-validated; automated per-PR
  execution is a follow-on.
- Native-binary live E2E through the Python init path (the native spawn is
  unit-covered with a mocked `Popen`; the Java `/health` is covered by the
  engine-service-release CI smoke).
- `cosign`/Sigstore signing of the published binary: the **publisher half**
  (sign + self-verify + publish the bundle) is delivered (`nexus-1odsm`,
  primitive 1 above). The **consumer half** (conexus deploy verifies the bundle
  before `docker run`, documented in conexus `ECR_PUSH.md`) is the remaining
  RDR-001 consumer task, tracked by a child bead under `nexus-w5v8j`.
