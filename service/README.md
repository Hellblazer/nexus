# nexus-service

Postgres-backed T2/T3 storage service for nexus (RDR-152). Java 25, jOOQ, Liquibase,
HikariCP, local ONNX embedding (onnxruntime + DJL HuggingFace tokenizers).

## Build

```bash
./mvnw test                       # JVM unit suite (needs Docker for testcontainers)
./mvnw -DskipTests package        # fat jar -> target/nexus-service-*.jar
./mvnw -DskipTests package -Pnative   # GraalVM native binary -> target/nexus-service
```

Every build runs jOOQ codegen against a throwaway `pgvector/pgvector:pg17`
testcontainer, so **Docker must be running**.

## Native image (`-Pnative`)

Produces a self-contained binary — no JVM needed at runtime. Opt-in; the default
build and the jlink image are unaffected.

### Prerequisites

- **GraalVM ≥ 25.0.3** (Oracle GraalVM, `distribution: graalvm`). 25.0.1 is rejected:
  the bundled jOOQ/liquibase reachability metadata use the unified
  `reachability-metadata` schema only newer 25.x understands. Point both
  `JAVA_HOME` and `GRAALVM_HOME` at it (the plugin detects via `GRAALVM_HOME`).
- **Docker** — for the codegen testcontainer.
- **A C toolchain for native-image:**
  - **Linux:** `gcc`, `glibc-devel`, `zlib-devel` (the Oracle `native-image` container has them).
  - **macOS:** Xcode Command Line Tools (`xcode-select --install`).
  - **Windows:** **MSVC Build Tools** (Visual Studio "Desktop development with C++"
    workload). Run the build from a *Developer Command Prompt for VS* (or any shell
    where `cl.exe` is on `PATH`).

### native-image does NOT cross-compile

The binary targets the **build host's OS + arch**. To get a Windows `.exe`, build on
Windows; for a macOS binary, build on a Mac; for Linux, build on Linux (or in the
Oracle GraalVM container). There is no Docker cross-build to a Windows/macOS target —
Docker only helps for Linux, and Windows containers need a Windows host.

### Per-platform embedding libs (automatic)

onnxruntime and DJL tokenizers ship a native lib per `<os-arch>` inside their jars.
The build embeds **only the host platform's** libs (not all ~120MB of them), selected
by the `native-libs-{mac,windows,linux-aarch64}` profiles in `pom.xml` (Maven `<os>`
activation; linux-x64 is the default). So you just run `./mvnw -Pnative package` on
each machine and it bundles the right `.so`/`.dylib`/`.dll`.

Supported build hosts: linux-x64, linux-aarch64, osx-aarch64, win-x64. (Intel macOS
is not supported — DJL 0.30.0 ships no `osx-x86_64` tokenizers lib.)

### Smoke test

```bash
./native-smoke.sh        # boots the native binary on a throwaway pgvector,
                         # asserts migration + jOOQ INSERT/SELECT/FTS = 200
```

### Runtime env

`NX_DB_URL` `NX_DB_USER` `NX_DB_PASS` (Postgres), `NX_SERVICE_PORT`,
`NX_SERVICE_TOKEN`, `NX_EMBED_MODE=onnx`. See `Main.java` for the full set.
`NX_HNSW_EF_SEARCH` (default 200, range 1..1000) overrides the serving
`hnsw.ef_search` floor on every vector-ranked query — the cross-tenant
crowd-out headroom (nexus-4ktfm; see `PgSession.DEFAULT_EF_SEARCH_FLOOR`).
A malformed/out-of-range value fails the service AT BOOT with the parse
error (validated from `Main`, never deferred to the first query).
`NX_SEARCH_STATEMENT_TIMEOUT_MS` (default 30000, range 1..600000) bounds
every vector-ranked statement with a transaction-local `statement_timeout`
(nexus-g17tf) so an orphaned or pathological scan cancels (SQLSTATE 57014)
instead of pinning xmin for hours. Sized to the edge's 30s budget; `0`
would disable the bound and is refused at boot. The shutdown hook also
terminates this process's own backends (`BackendReaper`, keyed on a
per-boot `application_name`) before closing the pool, since a CPU-bound
backend never notices a closed socket.
`NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS` (default 30; `0` disables) is the read bound on a Postgres
server that has gone silent (nexus-u9zkn; the 2026-10-05 failover hung one search read 59 s with no
RST). It is per path, not pool-wide, and it covers the statements a path bounds, not every read of a
request: every `statement_timeout` the Java side sets also gives its connection a socket read timeout of
that bound plus the margin (`PgSession.setLocal` sets both, so they cannot drift; it runs first among a
borrow's `setLocal` round trips so the others are covered), restored when the connection returns to the
pool. At the defaults: search 30 + 30 = 60 s, taxonomy assign 60 s, reaper statements and the gc batches
25 + 30 = 55 s, a bounded reaper census 60 + 30 = 90 s, the scheduled sweeps 60 s, the sweep gate
5 + 30 = 35 s, tuple subspace list 10 + 30 = 40 s. That ends a silent bounded statement at about the 59 s
the failover took, deterministically; lower the margin to end it sooner. Reads that stay unbounded: the
token store's reads on an auth-cache miss and session resolution (`TokenStore`, no stamp, no statement
bound), `CollectionRegistry.lookup` on a cache miss, and any read with no statement bound. A request can
still wait on TCP in any of them. Paths with no statement bound get no read bound, deliberately (collection
re-home, quarantine, purge-trash, delete and rename collection, the taxonomy link joins, `VACUUM`, writes
in general, the Liquibase migration): they legitimately run for minutes with the server silent, and a
socket timeout closes the socket without cancelling the backend, so the write would roll back and its
retry would stack a second backend behind the first. The tenant stamp that opens every borrow runs before
the bound is set, so it gets a read bound of the margin, never under 5 s, reset to none before the path
runs. The `gc_*` and `reaper_*` plpgsql functions set `statement_timeout` themselves (5 s or 25 s), which
no Java bound covers; every path that calls a 25 s function sets a Java bound of 25 s, so its read bound
(25 s plus the margin) outlasts it, and a new such path must do the same. `tcpKeepAlive` is on for both
pools (`PoolKeepAlive`, which does not load `PgSession`). A bad margin fails the service AT BOOT.
`NX_SEARCH_EXACT_MAX_ROWS` (default 10000, provisional; range 0..1000000, `0` disables) is the
cardinality router's threshold (nexus-tu8wp.6): a plain-search statement whose selected
collections hold at most that many physical rows in the tenant runs exact instead of
walking the shared HNSW index. A malformed value fails the service AT BOOT.
`NX_OWNERLESS_WRITE_MODE` (RDR-223 Phase 3 Step 2) is `enforce` or `log-only`;
**unset or blank means `log-only`**, so only an explicit `enforce` refuses a
`/v1/vectors/upsert-chunks` or `/store-put` write whose chashes have no live
manifest row (422, `reason: ownerless_chunk_write`). In `log-only` the write
proceeds, and the engine logs `ownerless_chunk_write_would_refuse` (once per
route, tenant and collection per minute, with the request's `User-Agent` and
`X-Nexus-Client-Version`, `absent` for a client older than the cut that sends
it) and counts it. Any other value fails the service AT BOOT. `GET /v1/status`
carries `ownerless_write_mode`, `ownerless_writes_refused_total` and
`ownerless_writes_would_refuse_total`. The local engine launch sets `enforce`, and **a blank value at the
local launcher counts as unset (enforce), while the raw engine parses blank as `log-only`**. The would-refuse
log line is a SAMPLE of writers, not a complete list: one line per route, tenant and collection per
minute for the first unowned chunk only; past 10,000 live keys new keys share ONE overflow bucket (one
line a minute, `suppressed_since_last` pooled across keys), and under key churn a minute can carry up to
10,001 lines. The counter delta, not the log, is the criterion for the flip to `enforce`
(nexus-z0o2p.40). In `log-only`, a request the pre-embed check already reported counts once and its
in-transaction recheck is skipped, so a chash that loses its owner during the embed is not counted again.
The line holds tenant content: the first unowned chunk's `source_path`, `title` and `source_agent` (a URL
value loses its userinfo, query and fragment), the collection name, `User-Agent` and
`X-Nexus-Client-Version`; never `source_uri` or chunk text. Its retention is the deployment's log retention.
`NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS` (default 30000) and
`NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS` (default 5000), both range 1..600000,
bound the taxonomy assign transaction (`assign_from_chashes_<dim>`) the
same way (nexus-r0vkh): a runaway call cancels (57014) and a call queued on
the `nexus.topics` row locks behind it fails (55P03) instead of holding a
pool connection for the head's lifetime. 2026-09-16: one 782s assign call
plus eight queued on it took nine of the pool's ten connections and every
PG-backed route on the box for 11 minutes. `0` is refused at boot.

### CI

`.github/workflows/service-ci.yml` builds and smoke-tests the native image on
Linux amd64 every time `service/**` changes — the authoritative gate. The
`native-build-tools` reachability metadata plus the committed
`META-INF/native-image/traced/reachability-metadata.json` (captured by the
native-image tracing agent via `trace-native.sh`) supply the reflection/resource
config; re-capture with `trace-native.sh` if reflection-using deps change.
