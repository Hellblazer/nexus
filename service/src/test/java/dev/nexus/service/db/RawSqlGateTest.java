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
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * House-rule gate (nexus-xtmtf, widened by nexus-mzuj9): NO raw string-SQL
 * ANYWHERE in {@code service/src/main} — neither statement EXECUTION
 * ({@code ctx.execute(...)}, JDBC {@code executeQuery}/{@code executeUpdate})
 * NOR read-side FETCH ({@code ctx.fetch("...")}, {@code ctx.fetchOne("...")},
 * {@code ctx.fetchAny("...")}, {@code ctx.resultQuery(...)}). jOOQ generates
 * the DSL for these schemas; every call site uses it, full stop — this is a
 * house rule (Hal, 2026-07-03/04), not a style preference.
 *
 * <p>The bootstrap-JDBC file-level whitelist that predated this bead is
 * GONE: {@code HealthHandler}/{@code VersionHandler}/{@code PoolerModeCheck}
 * now route their reads through {@code DSL.using(connection)} like every
 * other call site (nexus-mzuj9 phase (c)).
 *
 * <p>The ONLY remaining escape is the SANCTIONED_METHODS allowlist below —
 * method-scoped, not file-scoped. A handful of read sites genuinely cannot
 * be expressed as typed jOOQ DSL (the pgvector {@code <=>} distance operator
 * ordered directly off a bind-parameter vector literal combined with a
 * dynamic-arity {@code WHERE}; a PgBouncer admin-console meta-command with
 * no fixed column set). Each sanctioned method carries a
 * {@code // SANCTIONED RAW (nexus-mzuj9): <why>} comment at its definition
 * site (auditable, not silent) and is named here explicitly.
 */
class RawSqlGateTest {

    /** String-SQL execution AND fetch shapes, matched across line breaks (review
     * finding: the per-line scan was evadable by a newline after the
     * paren). Covers {@code .execute("...")}, {@code .execute(sql...)},
     * {@code .execute(new StringBuilder...)}, {@code ctx.query("...")}
     * (jOOQ's raw-SQL query builder), JDBC
     * {@code executeQuery("...")/executeUpdate("...")}, and the fetch-side
     * siblings {@code .fetch("...")/.fetch(sql...)},
     * {@code .fetchOne("...")/.fetchOne(sql...)},
     * {@code .fetchAny("...")/.fetchAny(sql...)}, {@code .resultQuery("...")}.
     * A bare {@code .execute()}/{@code .fetch()}/{@code .fetchOne()} (jOOQ DSL
     * terminal, no string/variable argument) does not match.
     *
     * KNOWN RESIDUAL (accepted, documented per critique): a raw SQL
     * string bound to a variable NOT prefixed "sql" and passed to
     * .execute(var)/.fetch(var) evades the name heuristic — jOOQ's legitimate
     * .execute(Query)/.fetch(Field...) overloads make a match-any-identifier
     * rule false-positive on typed DSL usage, so the heuristic stays
     * name-based. */
    private static final Pattern RAW_EXECUTE = Pattern.compile(
        "(\\.execute\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.query\\(\\s*\""
        + "|\\.execute(Query|Update)\\(\\s*\""
        + "|\\.fetch\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.fetchOne\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.fetchAny\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.resultQuery\\(\\s*\")",
        Pattern.DOTALL);

    /** Method-declaration scanner: modifier-led, 4-space-indented top-level
     * method signatures. Deliberately simple (not a full Java parser) —
     * matches this codebase's consistent formatting; the reluctant prefix
     * quantifier stops at the first identifier immediately followed by
     * {@code (}, which is always the method name in valid Java (no
     * modifier/type keyword is itself followed directly by a paren here). */
    private static final Pattern METHOD_DECL = Pattern.compile(
        "(?m)^ {4}(?:public|private|protected)(?:\\s+\\w+)*\\s+[\\w<>\\[\\],.\\s]+?\\b(\\w+)\\s*\\(");

    /**
     * Method-scoped escape hatch (nexus-mzuj9): {@code file.java -> {sanctioned method
     * names}}. Each entry's definition site carries a
     * {@code // SANCTIONED RAW (nexus-mzuj9): <why>} comment explaining why jOOQ's typed
     * DSL cannot express that specific site — see the referenced classes.
     */
    private static final Map<String, java.util.Set<String>> SANCTIONED_METHODS = Map.of(
        "PgVectorRepository.java", java.util.Set.of(
            // pgvector `<=>` ordered off a bind-parameter vector literal, combined with a
            // dynamic-arity metadata WHERE and (hybridSearch) a selectivity-dependent plan
            // choice between structurally different queries — the single execution
            // chokepoint for search()/hybridSearch().
            "rawVectorFetch",
            // nexus.search_<kind>_scoped_<dim>(...) combined-query stored-function calls
            // (metadata-scoped / graph-hop / topic-scoped); per-dim generated table-valued-
            // function wrappers exist but a full dispatch-map conversion is deferred
            // (risk/effort, not a hard DSL wall — see the method javadoc).
            "runCombinedQuery",
            "runCombinedQueryWithChash"),
        "TaxonomyCentroidRepository.java", java.util.Set.of(
            // Same pgvector `<=>` category as PgVectorRepository.rawVectorFetch.
            "annQuery"),
        "PoolerModeCheck.java", java.util.Set.of(
            // `SHOW CONFIG` is a PgBouncer admin-console meta-command, not SQL against any
            // table/schema — no jOOQ DSL form exists (no bind params, no fixed column set).
            "fetchShowConfig")
    );

    @Test
    void noRawExecuteSqlInMainSources() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    String fileName = p.getFileName().toString();
                    java.util.Set<String> sanctioned =
                        SANCTIONED_METHODS.getOrDefault(fileName, java.util.Set.of());

                    // Strip comments FIRST (block + line), then scan the whole
                    // remaining source with a newline-tolerant pattern — a
                    // line break after ".execute(" no longer evades the gate.
                    String src = Files.readString(p)
                        .replaceAll("(?s)/\\*.*?\\*/", "")
                        .replaceAll("(?m)//.*$", "");

                    // Pre-index every top-level method declaration's start offset + name,
                    // in source order, so each violation can be attributed to its nearest
                    // enclosing method (Java methods never nest, so "last declaration
                    // before the violation offset" is always the correct enclosing method).
                    List<int[]> declOffsets = new ArrayList<>();
                    List<String> declNames = new ArrayList<>();
                    Matcher dm = METHOD_DECL.matcher(src);
                    while (dm.find()) {
                        declOffsets.add(new int[] {dm.start()});
                        declNames.add(dm.group(1));
                    }

                    var m = RAW_EXECUTE.matcher(src);
                    while (m.find()) {
                        String enclosing = null;
                        for (int i = declOffsets.size() - 1; i >= 0; i--) {
                            if (declOffsets.get(i)[0] <= m.start()) {
                                enclosing = declNames.get(i);
                                break;
                            }
                        }
                        if (enclosing != null && sanctioned.contains(enclosing)) {
                            continue;
                        }
                        int line = 1 + (int) src.substring(0, m.start()).chars()
                            .filter(c -> c == '\n').count();
                        violations.add(p + ":" + line + "  " + m.group().strip()
                            + (enclosing != null ? "  [in " + enclosing + "]" : ""));
                    }
                } catch (IOException e) {
                    throw new RuntimeException(e);
                }
            });
        }

        assertThat(violations)
            .as("raw string-SQL execute()/fetch() calls in src/main — use the jOOQ DSL "
                + "(PgSession.setLocal for GUCs, DimTables for per-dim tables, "
                + "typed OffsetDateTime binds for timestamptz); if genuinely unavoidable, "
                + "hoist into a named method and add it to RawSqlGateTest's "
                + "SANCTIONED_METHODS with a // SANCTIONED RAW comment")
            .isEmpty();
    }
}
