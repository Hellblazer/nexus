// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * House-rule gate (nexus-xtmtf): no raw string-SQL through
 * {@code ctx.execute(...)} anywhere in {@code service/src/main}. jOOQ
 * generates the DSL for these schemas; write paths use it, full stop.
 * Transaction-local GUCs route through {@link PgSession#setLocal} (itself
 * pure DSL via {@code set_config}); the pgvector/dynamic-dim tables route
 * through {@code DimTables}; timestamptz binds are typed OffsetDateTime.
 *
 * <p>Scope: statement EXECUTION only. Read-side {@code ctx.fetch("...")}
 * raw SQL (vector search, stats views, UNION-ALL reads) is inventoried on
 * the nexus-h8rf6 audit and converted separately — widening this gate to
 * fetch requires that conversion first.
 */
class RawSqlGateTest {

    /** String-SQL execution shapes, matched across line breaks (review
     * finding: the per-line scan was evadable by a newline after the
     * paren). Covers {@code .execute("...")}, {@code .execute(sql...)},
     * {@code .execute(new StringBuilder...)}, {@code ctx.query("...")}
     * (jOOQ's raw-SQL query builder), and JDBC
     * {@code executeQuery("...")/executeUpdate("...")}. A bare
     * {@code .execute()} (jOOQ DSL terminal) does not match.
     *
     * KNOWN RESIDUAL (accepted, documented per critique): a raw SQL
     * string bound to a variable NOT prefixed "sql" and passed to
     * .execute(var) evades the name heuristic — jOOQ's legitimate
     * .execute(Query) overload makes a match-any-identifier rule
     * false-positive on typed DSL usage, so the heuristic stays
     * name-based. The three pre-existing bootstrap JDBC sites
     * (HealthHandler, VersionHandler, PoolerModeCheck) run before/
     * outside the jOOQ context and are whitelisted below. */
    private static final Pattern RAW_EXECUTE = Pattern.compile(
        "(\\.execute\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.query\\(\\s*\""
        + "|\\.execute(Query|Update)\\(\\s*\")",
        Pattern.DOTALL);

    /** Bootstrap/health JDBC sites that predate jOOQ context availability. */
    private static final java.util.Set<String> JDBC_BOOTSTRAP_WHITELIST =
        java.util.Set.of(
            "HealthHandler.java", "VersionHandler.java", "PoolerModeCheck.java");

    @Test
    void noRawExecuteSqlInMainSources() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    if (JDBC_BOOTSTRAP_WHITELIST.contains(p.getFileName().toString())) {
                        return;
                    }
                    // Strip comments FIRST (block + line), then scan the whole
                    // remaining source with a newline-tolerant pattern — a
                    // line break after ".execute(" no longer evades the gate.
                    String src = Files.readString(p)
                        .replaceAll("(?s)/\\*.*?\\*/", "")
                        .replaceAll("(?m)//.*$", "");
                    var m = RAW_EXECUTE.matcher(src);
                    while (m.find()) {
                        int line = 1 + (int) src.substring(0, m.start()).chars()
                            .filter(c -> c == '\n').count();
                        violations.add(p + ":" + line + "  " + m.group().strip());
                    }
                } catch (IOException e) {
                    throw new RuntimeException(e);
                }
            });
        }

        assertThat(violations)
            .as("raw string-SQL execute() calls in src/main — use the jOOQ DSL "
                + "(PgSession.setLocal for GUCs, DimTables for per-dim tables, "
                + "typed OffsetDateTime binds for timestamptz)")
            .isEmpty();
    }
}
