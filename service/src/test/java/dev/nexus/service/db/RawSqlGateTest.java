// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
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
 * <p><b>STATEMENT-GRANULAR (nexus-4okz4 increment 5, replacing the former
 * method-granular sanction):</b> the escape hatch used to be method-scoped —
 * ANY raw {@code execute()}/{@code fetch()} match found anywhere inside a
 * sanctioned method's brace-matched body was excused, regardless of what it
 * was. That let a sanctioned method silently SHELTER a brand-new, unrelated
 * raw statement added later (exactly how the nexus-11gh6/t76bp arc grew
 * fresh DSL-expressible raw queries inside an already-sanctioned region,
 * fixed same-day under the nexus-4okz4 directive). {@link #SANCTIONED_STATEMENTS}
 * replaces the old method-name allowlist with an explicit per-method
 * MULTISET of the exact canonical statement texts permitted (whitespace-
 * collapsed source from the {@code .execute(}/{@code .fetch(} call through
 * its matching close paren, with an expected occurrence count). A match
 * inside a registered method's region is excused ONLY when its own
 * canonical text is in that method's declared multiset AND has not already
 * been consumed by an earlier match in the same scan — a NEW or DIFFERENT
 * raw statement in the same method, or one extra occurrence of an
 * already-declared statement, is flagged as {@code UNSANCTIONED STATEMENT}
 * even though the method name itself is registered. The reverse direction
 * is checked too: a declared statement that no longer appears (or appears
 * fewer times than declared) is flagged as a {@code STALE SANCTIONED
 * FINGERPRINT} — an allowlist entry cannot silently rot into meaninglessness
 * the way a method-granular one could. See {@link #scan} and
 * {@link #statementGranular_newStatementInSanctionedMethod_isFlagged} /
 * {@link #statementGranular_staleFingerprintNeverMatched_isFlagged} for the
 * falsification proof of both directions.
 *
 * <p>Each sanctioned method's REGISTRATION (a key in {@link
 * #SANCTIONED_STATEMENTS}) still needs a {@code // SANCTIONED RAW
 * (nexus-mzuj9): <why>} comment at its definition site (auditable, not
 * silent). Three methods carry a registration with a genuinely EMPTY
 * statement multiset — {@code ChashRepository.lookup}, {@code
 * ChashSqlIdioms.contentCollapseDelete} — see their entries below for why:
 * each is a real, deliberate raw-SQL primitive, but this gate's
 * execute/fetch-call-shape detector structurally never observes a matching
 * call site inside the method's OWN body (one builds and returns a raw SQL
 * string without ever calling {@code execute}/{@code fetch} itself; the
 * other's argument is a named constant, not a literal, which evades the
 * name-based heuristic below — a pre-existing, documented KNOWN RESIDUAL).
 * Keeping the registration with zero declared statements is strictly safer
 * than removing it: any FUTURE literal raw call accidentally added to
 * either method is caught immediately (owner-attributed, zero fingerprints
 * match by construction) instead of needing a fresh registration to notice.
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
     * Statement-granular escape hatch (nexus-mzuj9 origin, nexus-4okz4
     * increment 5 tightening): {@code file.java -> {method name ->
     * {canonical statement text -> expected occurrence count}}}. The
     * canonical text is the whitespace-collapsed ORIGINAL source from the
     * matched {@code .execute(}/{@code .fetch(}-family call through its
     * matching close paren (see {@link #statementRegion} /
     * {@link #canonicalStatementText}) — copy it verbatim from a failing
     * run's {@code UNSANCTIONED STATEMENT}/{@code STALE SANCTIONED
     * FINGERPRINT} message rather than hand-transcribing it from the
     * source file, so a reformatting-only change (which the whitespace
     * collapse already tolerates) never produces a spurious diff here.
     */
    private static final Map<String, Map<String, Map<String, Integer>>> SANCTIONED_STATEMENTS =
        Map.ofEntries(
        Map.entry("ChashRepository.java", Map.of(
            // SANCTIONED RAW (nexus-piwya.3): lookup executes PROBE_SQL, the
            // PUBLISHED probe constant that ChashProbePlanShapeTest EXPLAINs
            // verbatim to pin index usage at 255k-row scale — executed SQL
            // and tested SQL must be the same string by construction; a DSL
            // rendering would decouple them. Every other ChashRepository
            // method uses typed DSL (DimTables). EMPTY statement multiset
            // (nexus-4okz4 increment 5): PROBE_SQL is passed BY NAME
            // (`ctx.resultQuery(PROBE_SQL, ...)`), not as a literal, so the
            // RAW_EXECUTE regex's name-based heuristic never matches inside
            // this method at all — a documented, pre-existing KNOWN
            // RESIDUAL (see this class's own javadoc above), not something
            // increment 5 introduced or is asked to close. Registered here
            // anyway so a future LITERAL raw call accidentally added to
            // lookup() is caught immediately rather than silently inheriting
            // a blanket excuse.
            "lookup", Map.of())),
        Map.entry("PgVectorRepository.java", Map.of(
            // pgvector `<=>` ordered off a bind-parameter vector literal, combined with a
            // dynamic-arity metadata WHERE and (hybridSearch) a selectivity-dependent plan
            // choice between structurally different queries — the single execution
            // chokepoint for search()/hybridSearch().
            "rawVectorFetch", Map.of(
                ".fetch(sql, binds)", 1))),
        Map.entry("TaxonomyCentroidRepository.java", Map.of(
            // Same pgvector `<=>` category as PgVectorRepository.rawVectorFetch.
            "annQuery", Map.of(
                ".fetch( \"SELECT topic_id, (embedding <=> ?::vector) AS distance FROM \" "
                + "+ centroidTable(dim) + \" WHERE collection \" + op + \" ?\" "
                + "+ \" ORDER BY distance ASC, topic_id ASC LIMIT ?\", "
                + "vectorLiteral(embedding), collection, nResults)", 1))),
        Map.entry("CatalogRepository.java", Map.of(
            // SANCTIONED RAW (nexus-5xn3k.2): pg_advisory_xact_lock over a
            // hashtext'd (tenant, doc_id) key — a session-scoped lock
            // primitive with no jOOQ DSL form, same category as RekeyOps'
            // advisory lock (see ChashSqlIdioms.java's entry below — the
            // lock primitive's OWN DSL rendering there is what RekeyOps
            // actually uses; this method is a second, independent
            // hashtext'd-key variant local to CatalogRepository, not a
            // duplicate of that one). Single-homed: every manifest-mutation
            // and verify-then-stamp path calls this one method.
            "acquireIndexRunLock", Map.of(
                ".execute(\"SELECT pg_advisory_xact_lock(hashtext('indexrun:' || ? || ':' || ?))\", "
                + "tenant, docId)", 1))),
        Map.entry("PoolerModeCheck.java", Map.of(
            // `SHOW CONFIG` is a PgBouncer admin-console meta-command, not SQL against any
            // table/schema — no jOOQ DSL form exists (no bind params, no fixed column set).
            "fetchShowConfig", Map.of(
                ".fetch(\"SHOW CONFIG\")", 1))),
        // RekeyOps.java: REMOVED (nexus-4okz4 increment 5) — rekey() itself
        // never calls ctx.execute/fetch with a raw literal or "sql"-prefixed
        // variable; its two raw-SQL-touching delegates (ChashSqlIdioms.
        // refreshAliasStats' ANALYZE + privilege probe, ChashSqlIdioms.
        // contentCollapseDelete's ctid/array_agg keeper DELETE) are METHOD
        // CALLS into ChashSqlIdioms — their OWN single true home, already
        // separately registered below — not literal statements inside
        // rekey() itself. Under the OLD method-granular gate this entry
        // sanctioned a region containing ZERO matchable statements (verified
        // empirically: the RAW_EXECUTE pattern finds nothing anywhere in
        // RekeyOps.java at this commit); under the new statement-granular
        // gate a zero-statement registration is structurally identical to no
        // registration at all, so per the same dead-entry-avoidance
        // discipline that removed StagingHandler.java/ChashCensus.java/
        // StagingPromoteOps.java's entries in increments 3-4, this one is
        // removed too rather than kept as a no-op. The bead's own increment-
        // 2 comment anticipated exactly this ("the method-level sanction
        // remains because RawSqlGateTest is method-granular ... that
        // tightening is a later increment") — this IS that later increment.
        Map.entry("ChashSqlIdioms.java", Map.of(
            // SANCTIONED RAW (rdr180-17): refreshAliasStats EXECUTES twice —
            // a privilege-probe fetchOne (system catalogs: pg_class,
            // has_table_privilege — outside codegen) and the ANALYZE itself
            // (maintenance DDL, no jOOQ DSL form at all). Must run inside
            // the caller's transaction so the planner sees that
            // transaction's own uncommitted alias rows (F2: 101min vs 461s
            // — see the method's own javadoc). Never serving-path.
            "refreshAliasStats", Map.of(
                ".fetchOne( \"SELECT current_setting('server_version_num')::int >= 170000 \" "
                + "+ \" AND (pg_catalog.has_table_privilege('nexus.chash_alias', \" "
                + "+ \" CASE WHEN current_setting('server_version_num')::int >= 170000 \" "
                + "+ \" THEN 'MAINTAIN' ELSE 'SELECT' END) \" "
                + "+ \" OR pg_catalog.pg_get_userbyid(\" "
                + "+ \" (SELECT relowner FROM pg_class \" "
                + "+ \" WHERE oid = 'nexus.chash_alias'::regclass)) = current_user)\" )", 1,
                ".execute(\"ANALYZE nexus.chash_alias\")", 1),
            // contentCollapseDelete: the ctid/array_agg ORDER BY
            // keeper-selection idiom (an array-subscript of an ordered
            // array_agg) has no jOOQ DSL form — genuinely raw SQL TEXT, but
            // this method only BUILDS and RETURNS that string (a plain
            // string-concatenation return statement); it never itself calls
            // execute()/fetch(). The actual execution happens at RekeyOps'
            // one call site (`ctx.execute(ChashSqlIdioms.
            // contentCollapseDelete(d.name()))`), whose argument is a
            // METHOD CALL, not a literal/"sql"-prefixed variable — outside
            // this regex's detection shape at BOTH ends (definer and
            // caller), the same structural blind spot as ChashRepository.
            // lookup's PROBE_SQL above. EMPTY statement multiset for the
            // same reason: registered so a future literal execute()/fetch()
            // accidentally added to this method is caught immediately.
            "contentCollapseDelete", Map.of())),
        Map.entry("SchemaMigrator.java", Map.of(
            // nexus-c4143 root fix: pg_constraint is a Postgres SYSTEM CATALOG (jOOQ
            // codegen only covers the nexus/t1 application schemas, no generated table
            // exists for pg_catalog), and ALTER TABLE ... {NO} FORCE ROW LEVEL SECURITY
            // is DDL jOOQ has no typed-DSL form for at all. Two DISTINCT statements
            // (NO FORCE / FORCE), each executed once per loop iteration over
            // CHASH_LEN_CONSTRAINTS at runtime but appearing exactly once each in the
            // SOURCE — the fingerprint is a source-level construct, not a runtime count.
            "preflightChashConstraints", Map.of(
                ".execute(\"ALTER TABLE nexus.\" + table + \" NO FORCE ROW LEVEL SECURITY\")", 1,
                ".execute(\"ALTER TABLE nexus.\" + table + \" FORCE ROW LEVEL SECURITY\")", 1))),
        Map.entry("TaxonomyRepository.java", Map.of(
            // SANCTIONED RAW (rdr155-p4b F-C): setval / pg_get_serial_sequence /
            // sequence last_value are sequence-state functions with no generated
            // jOOQ form (codegen models tables, not sequences); one statement on
            // the fidelity-import path only, never serving-path.
            "advanceTopicsIdSequence", Map.of(
                ".execute( \"SELECT setval(pg_get_serial_sequence('nexus.topics', 'id'), \" "
                + "+ \"GREATEST((SELECT last_value FROM nexus.topics_id_seq), ?))\", "
                + "maxImportedId)", 1))),
        Map.entry("TenantScope.java", Map.of(
            // SANCTIONED RAW (nexus-0ys55): VACUUM is PostgreSQL maintenance syntax
            // with no jOOQ typed-DSL form at all — same category as ChashSqlIdioms'
            // refreshAliasStats ANALYZE call above. Table names are validated
            // against a fixed allowlist (VACUUM_ALLOWED_TABLES) before the string is
            // built, so the concatenation is not an injection surface. Single-homed:
            // CatalogRepository#purgeTrash's post-commit VACUUM step is the only caller.
            "vacuumAnalyze", Map.of(
                ".execute(\"VACUUM (ANALYZE) \" + table)", 1)))
    );

    /**
     * DSL.inline(...) call sites where the argument is genuinely NOT a
     * compile-time literal but is provably injection-safe by construction
     * anyway (nexus-4okz4 increment 5, item (c) mechanical enforcement):
     * {@code file.java -> {method name -> {exact allowed argument text}}}.
     * Statement-granular by the SAME discipline as {@link
     * #SANCTIONED_STATEMENTS} — a DIFFERENT non-literal expression inside
     * the same allowlisted method still fails; only the EXACT declared
     * expression text is excused.
     */
    private static final Map<String, Map<String, java.util.Set<String>>> INLINE_NONLITERAL_SANCTIONED =
        Map.of(
        "VectorBinding.java", Map.of(
            // SANCTIONED NON-LITERAL INLINE (nexus-4okz4 increment 5): this
            // is jOOQ's own Binding SPI contract — sql() must render an
            // INLINED literal for ParamType.INLINED (debug/EXPLAIN
            // rendering; ordinary query execution takes the bind-variable
            // branch below, never this one). The argument is runtime DATA
            // (an embedding vector), not a fixed protocol constant, so it is
            // a structurally different safety category from THE
            // INLINE-VS-BIND RULE's "UTF8"/"hex" constants — but it is
            // PROVABLY injection-safe: Vector#toString() renders exclusively
            // via StringBuilder.append(float), which for a Java float
            // primitive can only ever produce digits/'.'/'-'/'E'/"Infinity"/
            // "NaN" — no quote, semicolon, or any other SQL metacharacter is
            // reachable through this path by construction, verified against
            // Vector.java's toString() implementation (no user string ever
            // passes through it).
            "sql", java.util.Set.of(
                "v == null ? null : v.toString()")));

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

    /** Comments-only blank (nexus-4okz4 increment 5, item (c)): same
     * traversal as {@link #blank}, but string/char literal CONTENTS are
     * left untouched — the inline-literal classifier below needs to see
     * the actual argument text, not blanked-out spaces. Still
     * length-preserving, still safe against a {@code //}/{@code /*} inside
     * a string literal being misread as a comment start (the traversal
     * skips over string/char bodies verbatim, the same way {@link #blank}
     * does, it just does not overwrite them). */
    static String blankComments(String src) {
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
                    if (src.charAt(i) == '\\' && i + 1 < out.length) i++;
                    i++;
                }
                i++;  // closing quote
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

    /** [start, end) span of ONE matched raw-SQL call statement (nexus-4okz4
     * increment 5): from the RAW_EXECUTE match start through the matching
     * close paren of that same call's argument list, found via paren-depth
     * counting on the (fully) BLANKED text — safe against stray parens
     * inside a string literal, exactly like {@link #sanctionedRegions}. */
    static int[] statementRegion(String blanked, int matchStart) {
        int open = blanked.indexOf('(', matchStart);
        int depth = 0;
        int i = open;
        while (i < blanked.length()) {
            char c = blanked.charAt(i);
            if (c == '(') {
                depth++;
            } else if (c == ')') {
                depth--;
                if (depth == 0) {
                    i++;
                    break;
                }
            }
            i++;
        }
        return new int[] {matchStart, Math.min(i, blanked.length())};
    }

    /** Canonical (whitespace-collapsed) ORIGINAL-source text of a matched
     * statement region (nexus-4okz4 increment 5) — read from the
     * UNBLANKED source (over the SAME offsets {@link #statementRegion}
     * computed on the blanked text; {@link #blank}/{@link #blankComments}
     * are length-preserving so the offsets line up 1:1) so the fingerprint
     * carries the real embedded SQL text, not blanked-out spaces.
     * Whitespace-collapse tolerates pure reformatting without changing the
     * fingerprint. */
    static String canonicalStatementText(String original, int[] region) {
        return original.substring(region[0], Math.min(region[1], original.length()))
            .replaceAll("\\s+", " ")
            .trim();
    }

    /** Per-file scan: blank comments/strings -> newline-tolerant raw-SQL
     * pattern -> brace-region method attribution -> per-statement
     * fingerprint validation (nexus-4okz4 increment 5) -> stale-fingerprint
     * sweep. Extracted so the nexus-8kbzu adversarial meta-tests exercise
     * the excusal logic against synthetic sources, not just the pattern
     * against the current tree. */
    static List<String> scan(String fileName, String rawSource) {
        String blanked = blank(rawSource);
        Map<String, Map<String, Integer>> methodStatements =
            SANCTIONED_STATEMENTS.getOrDefault(fileName, Map.of());

        Map<String, List<int[]>> regionsByMethod = new LinkedHashMap<>();
        for (String name : methodStatements.keySet()) {
            regionsByMethod.put(name, sanctionedRegions(blanked, java.util.Set.of(name)));
        }

        Map<String, Map<String, Integer>> consumedByMethod = new LinkedHashMap<>();
        List<String> violations = new ArrayList<>();

        var m = RAW_EXECUTE.matcher(blanked);
        while (m.find()) {
            int at = m.start();
            String owner = null;
            findOwner:
            for (var e : regionsByMethod.entrySet()) {
                for (int[] r : e.getValue()) {
                    if (r[0] <= at && at < r[1]) {
                        owner = e.getKey();
                        break findOwner;
                    }
                }
            }
            int line = 1 + (int) blanked.substring(0, at).chars()
                .filter(c -> c == '\n').count();
            if (owner == null) {
                violations.add(fileName + ":" + line + "  " + m.group().strip());
                continue;
            }
            int[] region = statementRegion(blanked, at);
            String text = canonicalStatementText(rawSource, region);
            Map<String, Integer> expected = methodStatements.get(owner);
            Map<String, Integer> consumed = consumedByMethod.computeIfAbsent(
                owner, k -> new LinkedHashMap<>());
            int already = consumed.getOrDefault(text, 0);
            int allowed = expected.getOrDefault(text, 0);
            if (already >= allowed) {
                violations.add(fileName + ":" + line + "  UNSANCTIONED STATEMENT in "
                    + owner + " (not declared in SANCTIONED_STATEMENTS, or exceeds its "
                    + "declared count): " + text);
            } else {
                consumed.put(text, already + 1);
            }
        }

        // Stale-fingerprint sweep (nexus-4okz4 increment 5): a declared
        // statement that the source no longer contains (renamed, reworded,
        // removed) rots the allowlist silently unless this checks the
        // REVERSE direction too.
        for (var methodEntry : methodStatements.entrySet()) {
            String method = methodEntry.getKey();
            Map<String, Integer> consumed = consumedByMethod.getOrDefault(method, Map.of());
            for (var stmtEntry : methodEntry.getValue().entrySet()) {
                int got = consumed.getOrDefault(stmtEntry.getKey(), 0);
                if (got < stmtEntry.getValue()) {
                    violations.add(fileName + "  STALE SANCTIONED FINGERPRINT in " + method
                        + ": declared " + stmtEntry.getValue() + "x, found " + got
                        + "x -- update or remove this SANCTIONED_STATEMENTS entry: "
                        + stmtEntry.getKey());
                }
            }
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
                + "hoist into a named method and add its EXACT canonical statement text to "
                + "RawSqlGateTest's SANCTIONED_STATEMENTS with a // SANCTIONED RAW comment "
                + "(a STALE SANCTIONED FINGERPRINT message means an existing entry's text "
                + "no longer matches — update or remove it)")
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

    /** A sanctioned method's OWN declared statement, reproduced verbatim,
     * stays excused. */
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

    // ── nexus-4okz4 increment 5: statement-granular falsification proof ──

    /**
     * THE core non-vacuity proof for item (a): a sanctioned method
     * sheltering a SECOND, DIFFERENT raw statement beyond the one
     * SANCTIONED_STATEMENTS actually declares must fail loud. Under the
     * OLD method-granular gate this would have been silently excused
     * (method name present -> whole region excused); under the new
     * statement-granular gate only the DECLARED text is excused, so the
     * new statement is flagged even though it sits inside a registered
     * method.
     */
    @Test
    void statementGranular_newStatementInSanctionedMethod_isFlagged() {
        String synthetic = String.join("\n",
            "public final class PoolerModeCheck {",
            "    private void fetchShowConfig() {",
            "        ctx.fetch(\"SHOW CONFIG\");",
            "        ctx.execute(\"DROP TABLE nexus.chash_alias\");",
            "    }",
            "}");
        List<String> hits = scan("PoolerModeCheck.java", synthetic);
        assertThat(hits)
            .as("the declared statement stays excused")
            .noneMatch(h -> h.contains("SHOW CONFIG"));
        assertThat(hits)
            .as("a NEW raw statement inside an already-sanctioned method must fail loud, "
                + "even though fetchShowConfig itself is registered")
            .anySatisfy(h -> assertThat(h)
                .contains("UNSANCTIONED STATEMENT")
                .contains("fetchShowConfig")
                .contains("DROP TABLE"));
    }

    /**
     * The reverse-direction non-vacuity proof: a declared statement that no
     * longer appears in the source (renamed, reworded, or removed) must
     * fail loud too — an allowlist entry cannot silently outlive the code
     * it once excused.
     */
    @Test
    void statementGranular_staleFingerprintNeverMatched_isFlagged() {
        String synthetic = String.join("\n",
            "public final class PoolerModeCheck {",
            "    private void fetchShowConfig() {",
            "        ctx.fetch(\"SHOW ADMIN\");",  // reworded -- no longer "SHOW CONFIG"
            "    }",
            "}");
        List<String> hits = scan("PoolerModeCheck.java", synthetic);
        assertThat(hits)
            .as("the declared SHOW CONFIG statement is no longer found -- stale entry")
            .anySatisfy(h -> assertThat(h).contains("STALE SANCTIONED FINGERPRINT")
                .contains("fetchShowConfig"));
        assertThat(hits)
            .as("the reworded statement is itself unsanctioned")
            .anySatisfy(h -> assertThat(h).contains("UNSANCTIONED STATEMENT")
                .contains("SHOW ADMIN"));
    }

    /**
     * Duplicate occurrences of the SAME declared statement beyond the
     * declared count are flagged too (the "counts" half of "per-statement
     * fingerprints/counts") — a copy-pasted second call is not free just
     * because its text matches an already-excused one.
     */
    @Test
    void statementGranular_duplicateOfDeclaredStatement_exceedsCount_isFlagged() {
        String synthetic = String.join("\n",
            "public final class PoolerModeCheck {",
            "    private void fetchShowConfig() {",
            "        ctx.fetch(\"SHOW CONFIG\");",
            "        ctx.fetch(\"SHOW CONFIG\");",
            "    }",
            "}");
        List<String> hits = scan("PoolerModeCheck.java", synthetic);
        assertThat(hits)
            .as("the declared count is 1; a second identical occurrence exceeds it")
            .anySatisfy(h -> assertThat(h).contains("UNSANCTIONED STATEMENT")
                .contains("fetchShowConfig"));
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
     *   <li>{@code StagingPromoteOps.chunkDim} (nexus-4okz4 increment 3:
     *       three explicit switch branches resolving {@code promoteCollection}'s
     *       per-dim content-table accessor)</li>
     *   <li>{@code dev.nexus.service.vectors.DimTables.CHUNKS} /
     *       {@code CENTROIDS} maps</li>
     *   <li>{@code ChashSqlIdioms.existsInAnyDim} (nexus-4okz4 increment 4:
     *       three explicit {@code EXISTS} disjuncts — the SINGLE-HOMED home
     *       for this idiom since increment 5 converged {@code
     *       StagingPromoteOps.canonExistsDsl}'s three call sites onto it and
     *       deleted the private copy; {@code ChashCensus}' dangling-pointer
     *       scan is a second caller)</li>
     * </ul>
     */
    @Test
    void chunkTablesCanary_fourthDimNeedsAllSitesToldChecklistAbove() {
        assertThat(ChashSqlIdioms.CHUNK_TABLES).hasSize(3);
    }

    // ── nexus-4okz4 increment 5, item (c): mechanical THE INLINE-VS-BIND
    //    RULE enforcement (critic Significant #2, T2 critique-4okz4-
    //    increment3-2026-08-09 [21952]) ──

    /** Only a compile-time literal (string/char/numeric/boolean/null) is a
     * safe {@code DSL.inline(...)} argument per THE INLINE-VS-BIND RULE
     * (ChashSqlIdioms.java class javadoc): fixed protocol constants
     * hardcoded in-file, never caller/user-derived values. */
    private static final Pattern COMPILE_TIME_LITERAL = Pattern.compile(
        "^\"([^\"\\\\]|\\\\.)*\"$"
        + "|^'([^'\\\\]|\\\\.)*'$"
        + "|^-?[0-9][0-9_]*(\\.[0-9_]+)?[fFdDlL]?$"
        + "|^(true|false|null)$");

    static boolean isCompileTimeLiteral(String arg) {
        return COMPILE_TIME_LITERAL.matcher(arg.trim()).matches();
    }

    /** Extract the FIRST top-level argument of a call whose {@code (} is at
     * {@code openParenIdx} — string/char literal interiors are copied
     * through without their contents being mistaken for structural parens
     * or a top-level comma (mirrors {@link #blank}'s quote-skipping, but
     * this scan needs the literal TEXT, not blanked-out spaces). */
    static String firstArg(String src, int openParenIdx) {
        int i = openParenIdx + 1;
        int depth = 1;
        StringBuilder sb = new StringBuilder();
        while (i < src.length() && depth > 0) {
            char c = src.charAt(i);
            if (c == '"' || c == '\'') {
                char q = c;
                sb.append(c);
                i++;
                while (i < src.length() && src.charAt(i) != q) {
                    if (src.charAt(i) == '\\' && i + 1 < src.length()) {
                        sb.append(src.charAt(i));
                        i++;
                    }
                    sb.append(src.charAt(i));
                    i++;
                }
                if (i < src.length()) {
                    sb.append(src.charAt(i));
                    i++;
                }
                continue;
            }
            if (c == '(') {
                depth++;
                sb.append(c);
                i++;
                continue;
            }
            if (c == ')') {
                depth--;
                if (depth == 0) break;
                sb.append(c);
                i++;
                continue;
            }
            if (c == ',' && depth == 1) break;
            sb.append(c);
            i++;
        }
        return sb.toString();
    }

    /** Per-file scan for {@code DSL.inline(...)} call sites whose first
     * argument is not a compile-time literal (nexus-4okz4 increment 5, item
     * (c)) — statement-granular allowlist via {@link
     * #INLINE_NONLITERAL_SANCTIONED}, same discipline as {@link #scan}. */
    static List<String> scanInlineNonLiteralArgs(String fileName, String rawSource) {
        String commentsBlanked = blankComments(rawSource);
        String fullBlanked = blank(rawSource);
        Map<String, java.util.Set<String>> methodAllowed =
            INLINE_NONLITERAL_SANCTIONED.getOrDefault(fileName, Map.of());
        Map<String, List<int[]>> regionsByMethod = new LinkedHashMap<>();
        for (String name : methodAllowed.keySet()) {
            regionsByMethod.put(name, sanctionedRegions(fullBlanked, java.util.Set.of(name)));
        }

        List<String> violations = new ArrayList<>();
        Matcher m = Pattern.compile("\\bDSL\\.inline\\(").matcher(commentsBlanked);
        while (m.find()) {
            int openParen = m.end() - 1;
            String arg = firstArg(commentsBlanked, openParen).trim();
            if (isCompileTimeLiteral(arg)) {
                continue;
            }
            int at = m.start();
            int line = 1 + (int) commentsBlanked.substring(0, at).chars()
                .filter(c -> c == '\n').count();
            String owner = null;
            findOwner:
            for (var e : regionsByMethod.entrySet()) {
                for (int[] r : e.getValue()) {
                    if (r[0] <= at && at < r[1]) {
                        owner = e.getKey();
                        break findOwner;
                    }
                }
            }
            boolean allowed = owner != null && methodAllowed.get(owner).contains(arg);
            if (!allowed) {
                violations.add(fileName + ":" + line + "  DSL.inline(" + arg + ") -- argument "
                    + "is not a compile-time literal (THE INLINE-VS-BIND RULE requires only "
                    + "fixed protocol constants ever be inlined -- ChashSqlIdioms.java class "
                    + "javadoc); bind via DSL.val(...) instead, or add a scoped, justified "
                    + "entry to INLINE_NONLITERAL_SANCTIONED if the value is PROVABLY "
                    + "injection-safe by construction despite not being a literal"
                    + (owner != null ? " [inside " + owner + ", but this exact text is not "
                        + "the allowlisted expression]" : ""));
            }
        }
        return violations;
    }

    @Test
    void noNonLiteralDslInlineArgsInMainSources() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    violations.addAll(scanInlineNonLiteralArgs(
                        p.getFileName().toString(), Files.readString(p)));
                } catch (IOException e) {
                    throw new RuntimeException(e);
                }
            });
        }

        assertThat(violations)
            .as("DSL.inline(...) call sites whose argument is not a compile-time literal — "
                + "see this class's mechanical enforcement of THE INLINE-VS-BIND RULE "
                + "(ChashSqlIdioms.java class javadoc): only fixed protocol constants "
                + "hardcoded in-file may ever be inlined")
            .isEmpty();
    }

    @Test
    void inlineRule_nonLiteralArgOutsideAllowlist_isFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void danger(String userInput) {",
            "        ctx.select(DSL.inline(userInput)).fetch();",
            "    }",
            "}");
        assertThat(scanInlineNonLiteralArgs("Whatever.java", synthetic))
            .as("a caller-derived DSL.inline argument outside the allowlist must fail loud")
            .isNotEmpty();
    }

    @Test
    void inlineRule_literalArgs_areExcusedWithoutAnyAllowlistEntry() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void safe() {",
            "        ctx.select(DSL.inline(\"UTF8\"), DSL.inline(0), DSL.inline(true)).fetch();",
            "    }",
            "}");
        assertThat(scanInlineNonLiteralArgs("Whatever.java", synthetic)).isEmpty();
    }

    @Test
    void inlineRule_allowlistedExpression_isExcusedOnlyForItsExactText() {
        String synthetic = String.join("\n",
            "public final class VectorBinding {",
            "    void sql() {",
            "        ctx.render().visit(DSL.inline(v == null ? null : v.toString()));",
            "    }",
            "}");
        assertThat(scanInlineNonLiteralArgs("VectorBinding.java", synthetic)).isEmpty();

        String drifted = String.join("\n",
            "public final class VectorBinding {",
            "    void sql() {",
            "        ctx.render().visit(DSL.inline(someOtherExpression()));",
            "    }",
            "}");
        assertThat(scanInlineNonLiteralArgs("VectorBinding.java", drifted))
            .as("the allowlist is text-scoped, not method-scoped -- a DIFFERENT non-literal "
                + "expression inside the same allowlisted method still fails")
            .isNotEmpty();
    }
}
