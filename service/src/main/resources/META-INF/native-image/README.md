# native-image config notes

GraalVM's native-image config JSON (`reflect-config.json`, `resource-config.json`,
`jni-config.json`, `proxy-config.json`, `serialization-config.json`) is parsed
against a closed schema of known attributes. A `_comment` key anywhere in a
descriptor object fails the build with "Unknown attribute(s) [_comment] in
reflection class descriptor object" — nexus-1yqac. Explanatory notes that used
to live inline as `_comment` keys are recorded here instead, keyed by the
descriptor's `name`.

## traced/reachability-metadata.json

- **No platform-specific native-library globs** (nexus-lhr6a, widened by nexus-vwfc0). The file
  is traced on one host, so a glob such as
  `ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib` embeds that host's
  library into every other platform's binary: linux and windows carried the
  mac onnxruntime until 2026-09-29. Each platform's own onnxruntime and DJL
  libraries come from the pom's `native-libs-*` profiles; JNA embeds its own
  per-platform libraries through its own metadata. A re-trace re-adds these
  globs; delete them again. `tests/test_native_image_embedded_libs.py` fails on
  any glob with a native-library suffix (`.so`, `.so.N`, `.dylib`, `.jnilib`,
  `.dll`) or a platform directory segment (darwin, osx, macos, linux, win,
  windows, with `-aarch64` / `-x86-64` / `-x64` variants), plus the per-library
  paths `ai/onnxruntime/native/<dir>/` and `native/lib/<dir>/`. The CI native
  trip-wire checks the resulting binary's actual embedded set
  (`scripts/check_native_embedded_resources.py`).

- **`ai.onnxruntime.OrtException`** (hand-added, nexus-o5xyx.3)
  `libonnxruntime4j_jni` throws it from native code through `FindClass` plus
  the `(int, String)` constructor whenever an ORT call returns an error
  status. The trace came from happy-path runs that never failed, so it was
  absent. The first hot path that makes ORT fail on purpose is a shutdown
  cancelling an in-flight `session.run()` through its terminate flag
  (`GatedRun`); without the entry the native binary could not raise the
  exception the gate maps to a 503.

- **`foreign.downcalls`: `void (jint)`** (hand-added, nexus-y6aj0)
  The downcall `ImmediateExit` makes to libc `_exit(int)` through
  `java.lang.foreign` when a stop's wait bound expires with ONNX Runtime work
  still in flight. A trace never takes that path. Without the entry the native
  binary logs `event=ort_init_immediate_exit_fallback` and leaves through
  `Runtime.halt`. A re-trace drops this section; add it again.

## reflect-config.json

- **`ch.qos.logback.classic.spi.LogbackServiceProvider`**
  Logback SPI: loaded via `ServiceLoader` at startup.

- **`org.postgresql.Driver`**
  PostgreSQL JDBC driver: loaded via `ServiceLoader` (`java.sql.Driver`).

- **`com.zaxxer.hikari.HikariConfig`**
  HikariCP: uses `Unsafe` + proxy generation at startup.

- **`liquibase.precondition.core.PreconditionContainer`** (and the adjacent
  `liquibase.precondition.core.SqlPrecondition`)
  Liquibase `<preConditions>` parsing (nexus-4m6i0.1/.13: the catalog-013-2
  + fk-002/fk-003 guards). `PreconditionContainer` and `SqlPrecondition` are
  instantiated by tag name and their properties (`onFail`, `expectedResult`,
  `sql`) set reflectively via `ObjectUtil.setProperty` during changelog
  parsing. The traced reachability metadata (`traced/reachability-metadata.json`)
  registers these classes TYPE-ONLY because the trace predates any
  `<preConditions>` usage in our changelogs (first-of-kind 2026-07-09) —
  method-level metadata was absent and the native binary crashed at
  changelog-parse time on boot. Caught by the pre-tag `--guided` rehearsal
  (2026-07-10): JVM tests structurally cannot see this failure class, and
  `ci.yml`'s `-Ob` build trip-wire only proves the BUILD succeeds, not
  runtime reflection during boot.

- **`liquibase.change.AbstractSQLChange`, `RawSQLChange`, `SQLFileChange`, `CreateProcedureChange`
  and `liquibase.precondition.core.DBMSPrecondition`** (hand-added, nexus-f3yqd.2.22)
  The vendored `ddl_utils` and `dml_utils` libraries (246 `<sqlFile>`, 180 `<createProcedure>`,
  one `<dbms>` precondition) reach Liquibase by reflection in ways no trace saw, and the native
  engine failed their apply with `MissingReflectionRegistrationError ... SQLFileChange.setPath`
  (engine-service-release run 44269122187). The traced metadata held these classes with
  `<init>` only. Liquibase reaches BOTH sides of every change parameter: the setter at parse
  time (`AbstractChange.load`), and the getter at apply time (`MarkChangeSetRanGenerator` calls
  `getDescription()`, which reads each `*name` and `path` parameter, and
  `CreateProcedureChange.generateCheckSum` calls `super.generateCheckSum()` on every checksum
  version, which serialises every parameter through its getter). The entries use `allDeclaredMethods` plus `allPublicMethods` rather than a
  hand-kept list, so a Liquibase upgrade that adds a parameter stays covered. They live here and
  not in the traced file because a re-trace drops hand edits to that file.
  `service/src/test/java/dev/nexus/service/nativeimage/LiquibaseReflectionRegistrationTest.java`
  loads every changelog the engine applies, asks Liquibase's own `ChangeMetaData` for each
  parameter's read and write method, and fails on any not registered here or in the traced
  file; `tests/test_native_liquibase_changelog_reflection.py` is the cheap XML-text cross-check
  of the setters, and fails on a changelog element it cannot map.

## resource-config.json

- **Every file under `src/main/resources` (outside this directory) needs a pattern here**, or a `-H:IncludeResources`
  buildArg in `service/pom.xml`, or an entry in the not-read-at-run-time list of `ResourceConfigCensusTest`
  (nexus-p2m1x). native-image folds only `Class.getResource` / `getResourceAsStream` calls whose argument is a constant;
  a `ClassLoader.getResourceAsStream(CONSTANT)` call is not registered, and JVM tests load from the classpath, so they
  never see the gap. `db/rdr229/p33a-fk-twins.tsv` was missing from engine-service v0.1.200 to v0.1.202 for that reason:
  `FkTwins` threw "is not on the classpath" from a static holder and RDR-229's boot-time completion work never started.
  `ResourceConfigCensusTest` walks the tree and fails on a file no pattern matches.
