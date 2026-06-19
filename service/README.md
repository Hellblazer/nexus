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

### CI

`.github/workflows/service-ci.yml` builds and smoke-tests the native image on
Linux amd64 every time `service/**` changes — the authoritative gate. The
`native-build-tools` reachability metadata plus the committed
`META-INF/native-image/traced/reachability-metadata.json` (captured by the
native-image tracing agent via `trace-native.sh`) supply the reflection/resource
config; re-capture with `trace-native.sh` if reflection-using deps change.
