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


    /**
     * Method-scoped escape hatch (nexus-mzuj9): {@code file.java -> {sanctioned method
     * names}}. Each entry's definition site carries a
     * {@code // SANCTIONED RAW (nexus-mzuj9): <why>} comment explaining why jOOQ's typed
     * DSL cannot express that specific site — see the referenced classes.
     */
    // Map.ofEntries: the allowlist crossed Map.of's 10-pair arity cap when
    // TaxonomyRepository.advanceTopicsIdSequence joined (rdr155-p4b F-C).
    private static final Map<String, java.util.Set<String>> SANCTIONED_METHODS = Map.ofEntries(
        Map.entry("ChashRepository.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-piwya.3): lookup executes PROBE_SQL, the
            // PUBLISHED probe constant that ChashProbePlanShapeTest EXPLAINs
            // verbatim to pin index usage at 255k-row scale — executed SQL
            // and tested SQL must be the same string by construction; a DSL
            // rendering would decouple them. Every other ChashRepository
            // method uses typed DSL (DimTables).
            "lookup")),
        Map.entry("PgVectorRepository.java", java.util.Set.of(
            // pgvector `<=>` ordered off a bind-parameter vector literal, combined with a
            // dynamic-arity metadata WHERE and (hybridSearch) a selectivity-dependent plan
            // choice between structurally different queries — the single execution
            // chokepoint for search()/hybridSearch().
            // (The combined-query stored-function calls — runCombinedQuery /
            // runCombinedQueryWithChash — were converted to the generated
            // table-valued-function DSL and REMOVED from this allowlist,
            // nexus-7ndh3.)
            "rawVectorFetch")),
        Map.entry("TaxonomyCentroidRepository.java", java.util.Set.of(
            // Same pgvector `<=>` category as PgVectorRepository.rawVectorFetch.
            "annQuery")),
        Map.entry("CatalogRepository.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-5xn3k.2): pg_advisory_xact_lock over a
            // hashtext'd (tenant, doc_id) key — a session-scoped lock
            // primitive with no jOOQ DSL form, same category as RekeyOps'
            // advisory lock. Single-homed: every manifest-mutation and
            // verify-then-stamp path calls this one method.
            "acquireIndexRunLock")),
        Map.entry("PoolerModeCheck.java", java.util.Set.of(
            // `SHOW CONFIG` is a PgBouncer admin-console meta-command, not SQL against any
            // table/schema — no jOOQ DSL form exists (no bind params, no fixed column set).
            "fetchShowConfig")),
        Map.entry("RekeyOps.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-jxizy.6 origin; narrowed nexus-4okz4
            // increment 2 — the method-level sanction remains because
            // RawSqlGateTest is method-granular, not statement-granular
            // (that tightening is a later increment), but the raw-SQL
            // surface actually remaining inside rekey() is now narrow: the
            // in-transaction ANALYZE + privilege probe (ChashSqlIdioms.
            // refreshAliasStats) and the two-phase content rekey's phase-A
            // collapse (ChashSqlIdioms.contentCollapseDelete's ctid/
            // array_agg ORDER BY keeper idiom) — neither has a jOOQ DSL
            // form. Every other statement in the method (advisory lock,
            // conflict pre-check, alias INSERT...SELECT...ON CONFLICT,
            // step-3 Item8, phase-B content rekey, every step-5 cascade)
            // converted to typed jOOQ DSL this increment; unionAllContentRows
            // (the raw-string UNION builder) was deleted outright and
            // replaced by the private unionAllContentRowsDsl, so it is
            // REMOVED from this allowlist — a dead sanction entry would
            // otherwise point at a method that no longer exists.
            "rekey")),
        Map.entry("StagingHandler.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-jxizy.10.4): the landing surface is
            // dynamic-by-store (8 staging tables, per-store column lists,
            // multi-row VALUES with ::vector/::jsonb casts) — one-shot
            // migration plumbing over tables jOOQ codegen deliberately
            // does not model (staging is transient landing state).
            "handleLoad", "handleEmbedFill", "handleClear", "handleCounts")),
        Map.entry("ChashCensus.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-jxizy.10.5): the census is dynamic BY
            // CONSTRUCTION — columns enumerated from information_schema at
            // run time; no generated jOOQ table can exist for a column the
            // census exists to DISCOVER. Read-only counts, never serving-path.
            "columns", "scan", "danglingPointers", "assertDiscoversKnownInventory")),
        Map.entry("StagingPromoteOps.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-jxizy.10.3): the land-then-transform
            // promote/finalize — one-shot migration statements composing the
            // ChashSqlIdioms fragments in the INSERT-into-populated-target
            // shape (DISTINCT ON keepers, alias joins, GREATEST-merge,
            // anti-join dedupes). Never serving-path.
            "promoteCollection", "finalizeTenant")),
        Map.entry("ChashSqlIdioms.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-jxizy.10.2, narrowed nexus-4okz4
            // increment 2): this increment converted contentRekeyUpdate,
            // frecencyAliasAggregate, and residualMismatchCount to typed
            // DSL twins (contentRekeyUpdateDsl / frecencyAliasAggregateDsl
            // / residualMismatchCountDsl — no allowlist entry needed, pure
            // DSL, no raw-SQL string executed) and DELETED the now-dead
            // raw-string forms outright, so all three are REMOVED from
            // this allowlist. contentCollapseDelete remains: the
            // ctid/array_agg ORDER BY keeper-selection idiom (an
            // array-subscript of an ordered array_agg) has no jOOQ DSL
            // form. chashOldBytes is untouched (StagingPromoteOps-only, a
            // one-line string helper with no execute/fetch call of its
            // own — out of this increment's scope).
            // SANCTIONED RAW (rdr180-17): refreshAliasStats additionally
            // EXECUTES — ANALYZE is maintenance DDL with no jOOQ DSL form at
            // all, and its privilege probe reads pg_class / has_table_privilege
            // (system catalogs, outside codegen). It must run inside the
            // caller's transaction so the planner sees the alias rows that
            // transaction just wrote (F2: 101min vs 461s), so it cannot be
            // hoisted out to a typed call site. Never serving-path.
            "contentCollapseDelete", "chashOldBytes", "refreshAliasStats")),
        Map.entry("SchemaMigrator.java", java.util.Set.of(
            // nexus-c4143 root fix: pg_constraint is a Postgres SYSTEM CATALOG (jOOQ
            // codegen only covers the nexus/t1 application schemas, no generated table
            // exists for pg_catalog), and ALTER TABLE ... {NO} FORCE ROW LEVEL SECURITY
            // is DDL jOOQ has no typed-DSL form for at all.
            "preflightChashConstraints")),
        Map.entry("TaxonomyRepository.java", java.util.Set.of(
            // SANCTIONED RAW (rdr155-p4b F-C): setval / pg_get_serial_sequence /
            // sequence last_value are sequence-state functions with no generated
            // jOOQ form (codegen models tables, not sequences); one statement on
            // the fidelity-import path only, never serving-path.
            "advanceTopicsIdSequence")),
        Map.entry("TenantScope.java", java.util.Set.of(
            // SANCTIONED RAW (nexus-0ys55): VACUUM is PostgreSQL maintenance syntax
            // with no jOOQ typed-DSL form at all — same category as ChashSqlIdioms'
            // refreshAliasStats ANALYZE call above. Table names are validated
            // against a fixed allowlist (VACUUM_ALLOWED_TABLES) before the string is
            // built, so the concatenation is not an injection surface. Single-homed:
            // CatalogRepository#purgeTrash's post-commit VACUUM step is the only caller.
            "vacuumAnalyze"))
    );

    /** Length-preserving blank-out of comment bodies and string/char literal
     * CONTENTS (delimiters kept): offsets and line numbers stay identical to
     * the original source, brace counting cannot be confused by braces inside
     * strings or comments, and the raw-SQL pattern still fires on the kept
     * opening quote. */
    static String blank(String src) {
        char[] out = src.toCharArray();
        int i = 0;
        while (i < out.length) {
            char c = out[i];
            if (c == '/' && i + 1 < out.length && out[i + 1] == '*') {
                int end = src.indexOf("*/", i + 2);
                end = (end < 0) ? out.length : end + 2;
                for (int j = i; j < end; j++) if (out[j] != '\n') out[j] = ' ';
                i = end;
            } else if (c == '/' && i + 1 < out.length && out[i + 1] == '/') {
                while (i < out.length && out[i] != '\n') out[i++] = ' ';
            } else if (c == '"' || c == '\'') {
                char q = c;
                i++;
                while (i < out.length && out[i] != q) {
                    if (out[i] != '\n') out[i] = ' ';
                    if (src.charAt(i) == '\\' && i + 1 < out.length) {
                        i++;
                        if (out[i] != '\n') out[i] = ' ';
                    }
                    i++;
                }
                i++;  // closing quote kept
            } else {
                i++;
            }
        }
        return new String(out);
    }

    /** [start, end) body regions of each sanctioned method in *blanked*
     * source: find ``name(`` where the preceding char is not ``.``/ident
     * (a receiver call or longer name), paren-match the signature, require
     * a following ``{``, brace-match the body. Brace-depth truth instead of
     * declaration regexes — nexus-8kbzu: one regex heuristic mis-attributed
     * nested-class and package-private shapes, the widened one matched call
     * sites; neither class of error is possible here. */
    static List<int[]> sanctionedRegions(String blanked, java.util.Set<String> names) {
        List<int[]> regions = new ArrayList<>();
        for (String name : names) {
            Matcher m = Pattern.compile("\\b" + Pattern.quote(name) + "\\s*\\(").matcher(blanked);
            while (m.find()) {
                int before = m.start() - 1;
                if (before >= 0 && (blanked.charAt(before) == '.'
                        || Character.isJavaIdentifierPart(blanked.charAt(before)))) {
                    continue;
                }
                int i = blanked.indexOf('(', m.start());
                int depth = 0;
                while (i < blanked.length()) {
                    char c = blanked.charAt(i);
                    if (c == '(') depth++;
                    else if (c == ')' && --depth == 0) break;
                    i++;
                }
                if (i >= blanked.length()) continue;
                int j = i + 1;
                while (j < blanked.length() && (Character.isWhitespace(blanked.charAt(j))
                        || Character.isJavaIdentifierPart(blanked.charAt(j))
                        || blanked.charAt(j) == ',')) {
                    j++;
                }
                if (j >= blanked.length() || blanked.charAt(j) != '{') continue;
                int braces = 0;
                int k = j;
                while (k < blanked.length()) {
                    char c = blanked.charAt(k);
                    if (c == '{') braces++;
                    else if (c == '}' && --braces == 0) break;
                    k++;
                }
                regions.add(new int[] {j, Math.min(k + 1, blanked.length())});
            }
        }
        return regions;
    }

    /** Per-file scan: blank comments/strings -> newline-tolerant raw-SQL
     * pattern -> brace-region sanction filter. Extracted so the nexus-8kbzu
     * adversarial meta-tests exercise the excusal logic against synthetic
     * sources, not just the pattern against the current tree. */
    static List<String> scan(String fileName, String rawSource) {
        String blanked = blank(rawSource);
        List<int[]> regions = sanctionedRegions(
            blanked, SANCTIONED_METHODS.getOrDefault(fileName, java.util.Set.of()));

        List<String> violations = new ArrayList<>();
        var m = RAW_EXECUTE.matcher(blanked);
        while (m.find()) {
            int at = m.start();
            boolean excused = regions.stream()
                .anyMatch(r -> r[0] <= at && at < r[1]);
            if (excused) continue;
            int line = 1 + (int) blanked.substring(0, at).chars()
                .filter(c -> c == '\n').count();
            violations.add(fileName + ":" + line + "  " + m.group().strip());
        }
        return violations;
    }

    @Test
    void noRawExecuteSqlInMainSources() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    violations.addAll(scan(
                        p.getFileName().toString(), Files.readString(p)));
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

    // ── nexus-8kbzu: the gate's own attribution logic under adversarial shapes ──

    /** A violation inside a NESTED class positioned after a sanctioned
     * method must still be flagged — it attributes to the nested method
     * (never sanctioned), not to the preceding sanctioned declaration. */
    @Test
    void attribution_nestedClassAfterSanctionedMethod_isStillFlagged() {
        String synthetic = String.join("\n",
            "public final class PgVectorRepository {",
            "    private void rawVectorFetch() {",
            "        ctx.fetch(\"SELECT sanctioned\");",
            "    }",
            "    static class Sneaky {",
            "        void hide() {",
            "            ctx.execute(\"DROP TABLE evil\");",
            "        }",
            "    }",
            "}");
        // Violation text is blanked (string contents erased by design);
        // assert on the location: line 7 is the nested-class execute call.
        List<String> hits = scan("PgVectorRepository.java", synthetic);
        assertThat(hits)
            .as("nested-class violation must not inherit the sanction")
            .anySatisfy(h -> assertThat(h).startsWith("PgVectorRepository.java:7"));
    }

    /** Package-private (no-modifier) methods are declaration boundaries too. */
    @Test
    void attribution_packagePrivateMethod_resetsSanction() {
        String synthetic = String.join("\n",
            "public final class TaxonomyCentroidRepository {",
            "    private void annQuery() {",
            "        ctx.fetch(\"SELECT sanctioned\");",
            "    }",
            "    void plainMethod() {",
            "        ctx.execute(\"DELETE FROM x\");",
            "    }",
            "}");
        List<String> hits = scan("TaxonomyCentroidRepository.java", synthetic);
        assertThat(hits)
            .anySatisfy(h -> assertThat(h).startsWith("TaxonomyCentroidRepository.java:6"));
    }

    /** Sanctioned methods themselves stay excused. */
    @Test
    void attribution_sanctionedMethodViolation_isExcused() {
        String synthetic = String.join("\n",
            "public final class PoolerModeCheck {",
            "    private void fetchShowConfig() {",
            "        ctx.fetch(\"SHOW CONFIG\");",
            "    }",
            "}");
        assertThat(scan("PoolerModeCheck.java", synthetic)).isEmpty();
    }

    // ── nexus-4okz4 increment 2 critic follow-up (T2 critique-4okz4-
    //    increment1-2026-08-08 [21850], nit 2) ──

    /**
     * {@code ChashSqlIdioms.CHUNK_TABLES} silently hardcodes "three dims" in
     * multiple independently hand-written call sites (RekeyOps' {@code DIMS},
     * StagingPromoteOps' residual-count call site, {@code
     * ChashSqlIdioms.danglingManifestCountDsl}'s three explicit NOT EXISTS
     * clauses). A fourth dim table added without touching every one of
     * those sites would drift silently. This canary fails LOUD instead:
     * whoever adds a fourth dim must touch {@code CHUNK_TABLES}, see this
     * test go red, and follow the checklist below.
     *
     * <p>FOURTH-DIM CHECKLIST (update all of these when this assertion
     * changes):
     * <ul>
     *   <li>{@code ChashSqlIdioms.CHUNK_TABLES} itself</li>
     *   <li>{@code ChashSqlIdioms.danglingManifestCountDsl} (three explicit
     *       {@code NOT EXISTS} clauses)</li>
     *   <li>{@code RekeyOps.DIMS} (three explicit typed entries) and its
     *       {@code orphanCond} (three explicit dim {@code NOT EXISTS}
     *       clauses) and {@code unionAllContentRowsDsl} (three explicit
     *       UNION ALL branches)</li>
     *   <li>{@code StagingPromoteOps.finalizeTenant}'s residual-count call
     *       site (three explicit {@code residualMismatchCountDsl} calls)</li>
     *   <li>{@code dev.nexus.service.vectors.DimTables.CHUNKS} /
     *       {@code CENTROIDS} maps</li>
     * </ul>
     */
    @Test
    void chunkTablesCanary_fourthDimNeedsAllSitesToldChecklistAbove() {
        assertThat(ChashSqlIdioms.CHUNK_TABLES).hasSize(3);
    }
}
