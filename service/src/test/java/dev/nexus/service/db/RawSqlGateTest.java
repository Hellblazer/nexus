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

    /** {@code <receiver>.execute("...")} or {@code .execute(sqlVar, ...)} —
     * the string-SQL overloads. A bare {@code .execute()} (jOOQ DSL terminal)
     * does not match. */
    private static final Pattern RAW_EXECUTE = Pattern.compile(
        "\\.execute\\(\\s*(\"|sql|new StringBuilder)");

    @Test
    void noRawExecuteSqlInMainSources() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    List<String> lines = Files.readAllLines(p);
                    for (int i = 0; i < lines.size(); i++) {
                        String line = lines.get(i);
                        String trimmed = line.trim();
                        if (trimmed.startsWith("//") || trimmed.startsWith("*")
                                || trimmed.startsWith("/*")) {
                            continue;  // comments may cite the forbidden form
                        }
                        if (RAW_EXECUTE.matcher(line).find()) {
                            violations.add(p + ":" + (i + 1) + "  " + trimmed);
                        }
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
