# Containerized gate fan-out (bead nexus-uq3xs)

Shard the pytest suite across N concurrent linux/arm64 containers, each a
fully isolated sandbox: its own hermetic PG (baked bundle), its own engine
JAR boot (`tests/_engine_substrate.py`), its own tmp/config dirs. This is the
isolated-sandbox concurrency the no-parallel-tests rule's refinement blesses
(`feedback_parallelize_isolated_gates`): the serial rule exists for
contention and edit-invalidation, and containers eliminate both — no shared
ports, no shared caches, no shared daemon state, immutable source snapshot
per run.

## Files

- `Dockerfile.shard` — the shard image (leg 1).
- `Dockerfile.shard.dockerignore` — BuildKit per-Dockerfile ignore
  (allowlist; keeps the multi-GB repo trees out of the build context).
- `fanout.sh` — build once, shard, run N capped containers, aggregate.

## Usage

```bash
# Full suite, 6 shards, engine substrate:
NX_TEST_T2_SUBSTRATE=engine tests/containers/fanout.sh

# Explicit roster, 2 shards, custom caps:
NX_TEST_T2_SUBSTRATE=engine tests/containers/fanout.sh -n 2 --cpus 2 --memory 3g \
  tests/test_t2_engine_substrate.py tests/test_memory_consolidation.py

# Rebuild image only (e.g. after a floor bump):
tests/containers/fanout.sh --build-only
```

Prereqs: Docker Desktop; the shaded JAR built on the host
(`mvn -f service/pom.xml package -DskipTests`). The JAR is COPYed into the
image, never built there; `jar_freshness_skip_reason` still applies
in-container because COPY preserves mtimes.

Outputs land in `tests/containers/out/<timestamp>/`: per-shard `shard-N.log`,
`shard-N.xml` (junit), `.rc`, `.time`. Exit code is non-zero if any shard
failed (pytest exit 5, empty-after-deselect, counts as pass).

Host-load discipline: each container is capped (`--cpus=2 --memory=3g
--shm-size=256m` by default), so 6 shards ≈ 12 CPUs / 18 GB worst case on a
16-CPU / 31 GB Docker VM — release gates on the host keep priority. Tune with
`--cpus` / `--memory` / `-n`.

## Cache keys (CI-cost directive: rebuild only on key miss)

| Layer | Key | Invalidation |
|---|---|---|
| base apt + uv | `UV_VERSION` build ARG | uv bump (rare) |
| PG bundle | `PINNED_SERVICE_TAG` build ARG (+ optional `PG_BUNDLE_SHA256` assert; actual sha recorded at `/opt/nexus-pg/bundle.sha256`) | engine floor bump (`REQUIRED_ENGINE_VERSION`) |
| bge-768 ONNX (engine boot requires it) | `BGE_ASSET_TAG` + `BGE_MODEL_SHA256` + `BGE_TOKENIZER_SHA256` ARGs (pins mirror `src/nexus/db/service_bge_model.py`) | model re-export (effectively never) |
| MiniLM ONNX | `MINILM_SHA256` ARG (verified against the chroma-S3 tarball; same constant as `src/nexus/db/minilm_direct.py`) | effectively never |
| CPython | `PY_VERSION` ARG | rare |
| dependency layer | content hash of `pyproject.toml` + `uv.lock` (implicit via COPY) | lock refresh |
| source + project | content of `src/ tests/ scripts/ docs/ ...` | every edit (cheap — wheels already cached) |
| service JAR | content of `service/target/nexus-service-1.0-SNAPSHOT.jar` | host `mvn package` |

`fanout.sh` derives `PINNED_SERVICE_TAG` from
`src/nexus/engine_version.py::REQUIRED_ENGINE_VERSION` (the ONE constant —
never hand-typed), tags the image `nexus-test-shard:<engine tag>`, and passes
it as the build ARG, so a floor bump re-keys exactly the PG-bundle layer and
the image tag.

Why bake the PG bundle instead of letting
`tests/db/_service_fixture._self_provision_pg_bundle` download at runtime:
the runtime path works in-container (it is the same product seam), but every
fresh container would re-download ~100 MB and re-extract; baking moves that
to one keyed image layer. `NEXUS_PG_BIN` is discovery leg 1, so the baked
bundle short-circuits the network leg entirely. Same argument for the MiniLM
tarball (chroma re-downloads on archive-hash miss).

## In-container contract notes

- Containers run as uid 1010 `nexus` — PostgreSQL `initdb` refuses root, and
  `tests/_engine_substrate.py` reads `os.environ["USER"]` (set via ENV).
- The linux PG bundle is `$ORIGIN`-relocatable (`scripts/build_pg_bundle.sh`),
  so a plain tar extract to `/opt/nexus-pg` works; the archive's top level is
  `bundle/`, bin at `bundle/bin`.
- `--shm-size=256m`: PG's dynamic shared memory uses `/dev/shm`; Docker's
  64 MB default is tight once a shard's engine substrate is up.
- Engine JAR needs Java 25 (`service/pom.xml` `maven.compiler.release=25`);
  base image is `eclipse-temurin:25-jre-noble`.
- `NX_TEST_T2_SUBSTRATE=engine` boots ONE PG + JAR per container (session-
  scoped, memoized) and mints a tenant per test — per-shard isolation is the
  container, per-test isolation is the tenant token. Budget ~15-25 s of
  substrate boot per container before the first test runs.
- uv is fetched from its GitHub release tarball, not `ghcr.io/astral-sh/uv`
  (ghcr pulls are denied from some networks; the GitHub asset is identical).
- The engine JAR's `Bge768Embedder` exits at boot without the bge-768 fp32
  model at `~/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/` — baked (layer
  2b). Symptom: `IllegalStateException: bge ONNX model not found`.
- `dt/scripts` must be in the image: it is a hatchling force-include of the
  conexus wheel; `uv sync --frozen` (project install) fails without it.
- FIRST container run after a fresh image unpack pays a one-time page-in cost
  (~2-3 min observed for a 4.5 GB image on Docker Desktop/macOS). Warm runs
  of the same image start in ~5 s. Budget for it or pre-warm with a trivial
  `docker run --rm <image> true`.

## Measured (2026-07-24, Docker Desktop 29.4.0, M-series 16 CPU / 31 GB VM)

- Image: 4.5 GB; cold build ~18 min (dep download dominates); no-change
  rebuild 24 s; source-edit rebuild = source COPY + project install + JAR
  copy + export (~2 min, wheels stay cached via the uv cache mount).
- 6-file / 149-test engine-substrate demo: serial one-container 51 s wall;
  2-shard fan-out 61 s wall (both shards green). At this roster size the
  fixed per-container cost (~15 s engine boot + ~10 s collection/imports)
  eats the win — sharding pays at full-suite scale where per-shard test time
  dominates the boot, and for host-load isolation of long sweeps.

## Leg 2 — rehearsal image (design sketch; extends nexus-imkxs)

Same layer discipline, different payload: a `Dockerfile.rehearsal` whose
cache keys are the REHEARSAL inputs rather than the working tree:

| Layer | Key |
|---|---|
| base + uv + JRE | as leg 1 (shared base layers if built from the same Dockerfile prefix) |
| prev-release wheel | `PREV_VERSION` ARG → `pip install conexus==<prev>` from PyPI (immutable) |
| prev release's engine | prev release's `REQUIRED_ENGINE_VERSION` tag → PG bundle + `nexus-service.jar` assets for THAT tag |
| seeded corpus (optional) | content hash of a fixture tarball |

A rehearsal container then runs the migration under test:
`pip install <candidate wheel>` (mounted from `dist/`) + `nx guided-upgrade`
against the baked prev-state, turning the 20-30 min
`tests/e2e/migration-rehearsal/run.sh` cold-start into minutes, and
`--from-version` legs become concurrent `docker run`s of different rehearsal
images (`nexus-rehearsal:<prev>-<prev-engine>`). Rotation policy stays what
`nexus-dlhub` decides; the image tag encodes the pair so stale images are
inert, not wrong.

## Leg 3 — sweep fan-out (design sketch)

Already covered by `fanout.sh`'s explicit file-list mode: a chunked sweep
(e.g. the RDR-155 flip dry-run's `NX_TEST_T2_SUBSTRATE=engine` sweeps) is
exactly `fanout.sh -n K <chunk files...>` with the env passthrough. The one
addition worth making when leg 3 goes live: a `--chunk-file` option that
accepts the sweep planner's chunk manifest (one file per line) verbatim, and
per-chunk junit aggregation into the sweep ledger format. No new image is
needed — sweeps run the same shard image.
