# native-image config notes

GraalVM's native-image config JSON (`reflect-config.json`, `resource-config.json`,
`jni-config.json`, `proxy-config.json`, `serialization-config.json`) is parsed
against a closed schema of known attributes. A `_comment` key anywhere in a
descriptor object fails the build with "Unknown attribute(s) [_comment] in
reflection class descriptor object" — nexus-1yqac. Explanatory notes that used
to live inline as `_comment` keys are recorded here instead, keyed by the
descriptor's `name`.

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
