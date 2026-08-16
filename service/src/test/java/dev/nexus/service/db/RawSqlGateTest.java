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
 * <p><b>SCOPE OF THE "cannot shelter new raw statements" GUARANTEE</b>
 * (nexus-8emxy, critic Critical on increment 5's first review pass,
 * 2026-08-09): the guarantee above is real but bounded to what {@link
 * #RAW_EXECUTE} can SEE — a LITERAL {@code .execute(}/{@code .fetch(}
 * -family call. It does NOT extend to raw SQL assembled DYNAMICALLY (a
 * {@code StringBuilder} chain across many lines, runtime-varying
 * WHERE-predicate loops) and funneled through a NAMED PRIVATE WRAPPER
 * method rather than a literal call — {@code PgVectorRepository.
 * searchWithTokens}/{@code hybridSearch} call their private {@code
 * rawVectorFetch(ctx, sql, binds)} BY NAME, which {@code RAW_EXECUTE}
 * cannot match at all (not "excused" — no anchor exists there for
 * statement-granularity to attach to). Confirmed empirically: appending an
 * entirely new raw-SQL predicate to {@code searchWithTokens}'s {@code
 * StringBuilder} produced ZERO gate signal before the fix below. {@link
 * #RAW_SQL_ASSEMBLY_SENTINELS} closes this SPECIFIC instance with a
 * coarser, whole-method-body tripwire (see its own javadoc) — it is a
 * targeted patch for the bead's first NAMED motivating file, not a general
 * solution to "a raw-SQL execution wrapper hidden behind an arbitrary
 * method name" recurring elsewhere in the codebase. That general problem is
 * tracked as nexus-8emxy (P2), not closed by this file.
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
 * NOT kept for detection speed — a registered zero-fingerprint entry and no
 * entry at all behave IDENTICALLY at scan time (both fail any future
 * literal match immediately; verified directly by falsifying {@code
 * RekeyOps.rekey}'s OWN removal below, which detects exactly as fast via
 * the plain unowned-violation path — only the failure MESSAGE differs).
 * Kept instead because these two methods genuinely CONTAIN/PRODUCE raw SQL
 * (unlike {@code RekeyOps.rekey}, whose "raw SQL" was entirely delegation
 * to elsewhere-registered primitives, hence removed outright below): the
 * registration is this class's own documented AUDIT-TRAIL invariant — "a
 * handful of read sites genuinely cannot be expressed as typed jOOQ DSL...
 * named here explicitly" (this class's own top-level javadoc). Dropping
 * these two entries would make them invisible to anyone reading {@link
 * #SANCTIONED_STATEMENTS} as the inventory of "where does raw SQL exist in
 * this codebase, and why" — a misleading omission given they demonstrably
 * DO belong in that inventory, gate-visibility of their call shape aside.
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
     * {@code .fetchAny("...")/.fetchAny(sql...)},
     * {@code .resultQuery("...")/.resultQuery(sql...)} (nexus-rfx2j: the
     * sql/SQL-prefix branch used to be missing here, an asymmetry with its
     * four siblings above -- closed so a future sql-prefixed resultQuery
     * call is caught the same way a sql-prefixed execute/fetch call is).
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
        + "|\\.resultQuery\\(\\s*(\"|sql|SQL|new StringBuilder))",
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
            // RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5):
            // retargeted from a bare "embedding" column + a hand-rolled
            // "nexus.taxonomy_centroids_<dim>" table name (the latter a table
            // dropped by the unify changeset -- would have been a SILENT
            // RUNTIME failure, invisible to this compile-time gate) to
            // DimTables.embeddingColumn(dim)/CENTROIDS_TABLE_NAME (via
            // centroidTable(dim)) plus an explicit "embedding_<dim> IS NOT
            // NULL" guard -- see the method's own javadoc for why the guard
            // is load-bearing, not cosmetic.
            "annQuery", Map.of(
                ".fetch( \"SELECT topic_id, (\" + embeddingCol + \" <=> ?::vector) AS distance FROM \" "
                + "+ centroidTable(dim) + \" WHERE \" + embeddingCol + \" IS NOT NULL AND collection \" "
                + "+ op + \" ?\" + \" ORDER BY distance ASC, topic_id ASC LIMIT ?\", "
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
                + "tenant, docId)", 1),
            // SANCTIONED RAW (RDR-191 Phase 5, nexus-o8dil.29): SET CONSTRAINTS is
            // PostgreSQL transaction-control syntax with no jOOQ typed-DSL form —
            // same category as SchemaMigrator's NO FORCE/FORCE ROW LEVEL SECURITY
            // entry below. Deferred-constraint fix for deleteCollectionTxn's
            // chunk-before-manifest ordering under fk_catalog_chunks_chunk (class-B
            // site 2 — see the method's own javadoc for the full derivation).
            "deferManifestChunkFk", Map.of(
                ".execute(\"SET CONSTRAINTS fk_catalog_chunks_chunk DEFERRED\")", 1))),
        Map.entry("PoolerModeCheck.java", Map.of(
            // `SHOW CONFIG` is a PgBouncer admin-console meta-command, not SQL against any
            // table/schema — no jOOQ DSL form exists (no bind params, no fixed column set).
            "fetchShowConfig", Map.of(
                ".fetch(\"SHOW CONFIG\")", 1))),
        // RekeyOps.java + ChashSqlIdioms.java's refreshAliasStats/
        // contentCollapseDelete entries: REMOVED at nexus-lgdel.l1. RekeyOps
        // is deleted outright (its only caller, the client chash_rekey
        // upgrade rung, is deleted in the same commit family). refreshAliasStats
        // and contentCollapseDelete are deleted from ChashSqlIdioms.java —
        // both existed solely to serve RekeyOps.rekey() and
        // StagingPromoteOps.promoteCollection's now-deleted alias-stats
        // refresh call; nothing in the surviving codebase calls either.
        // Per the dead-entry-avoidance discipline already established here
        // (increments 3-4 removed StagingHandler.java/ChashCensus.java/
        // StagingPromoteOps.java's entries the same way), a registration
        // for a deleted method is removed outright rather than kept as a
        // no-op.
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
        List<String> violations = new ArrayList<>();
        for (String name : methodStatements.keySet()) {
            List<int[]> regions = sanctionedRegions(blanked, java.util.Set.of(name));
            // nexus-rfx2j: sanctionedRegions attributes by BARE method name only
            // (no enclosing-class scoping) -- if a sanctioned file ever grows a
            // second, same-named method in a different (e.g. nested) class, BOTH
            // declarations collapse into one region list under this one key, and
            // a match inside the UNSANCTIONED twin could inherit the sanctioned
            // twin's excusal/count budget. Rather than adding class-scoped
            // attribution (a bigger rewrite for a collision that has never
            // occurred), this asserts the invariant the simpler bare-name
            // attribution depends on: exactly one physical declaration per
            // sanctioned name per file. See
            // #sanctionedRegions_bareNameCollisionAcrossClasses_isFlaggedAmbiguous
            // for the falsification proof.
            if (regions.size() > 1) {
                violations.add(fileName + "  AMBIGUOUS SANCTIONED METHOD NAME: \"" + name
                    + "\" matches " + regions.size() + " method declarations in this file -- "
                    + "sanctionedRegions() attributes by bare name only, so a same-named "
                    + "method in a different class/nested class would silently share this "
                    + "name's sanction. Rename one of the methods, or extend "
                    + "sanctionedRegions() to scope by enclosing class before adding a "
                    + "second same-named declaration to a sanctioned file.");
            }
            regionsByMethod.put(name, regions);
        }

        Map<String, Map<String, Integer>> consumedByMethod = new LinkedHashMap<>();

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

    /**
     * nexus-rfx2j falsification proof: {@link #sanctionedRegions} matches by
     * bare method name only, with no enclosing-class scoping. A same-named
     * method declared a second time in a different (here: nested) class
     * must be flagged as an ambiguous sanction target, not silently allowed
     * to share the outer method's excusal. Before the fix in {@link #scan},
     * this synthetic source produced ZERO violations for the collision
     * itself (only whatever the decoy's own raw-SQL text happened to
     * trigger) -- verified manually by reverting the {@code regions.size()
     * > 1} check and re-running this test, which then failed for lack of
     * an AMBIGUOUS finding.
     */
    @Test
    void sanctionedRegions_bareNameCollisionAcrossClasses_isFlaggedAmbiguous() {
        String synthetic = String.join("\n",
            "public final class PoolerModeCheck {",
            "    private void fetchShowConfig() {",
            "        ctx.fetch(\"SHOW CONFIG\");",
            "    }",
            "    static class Decoy {",
            "        void fetchShowConfig() {",
            "        }",
            "    }",
            "}");
        List<String> hits = scan("PoolerModeCheck.java", synthetic);
        assertThat(hits)
            .as("a same-named method in a different (nested) class must be flagged as an "
                + "ambiguous sanction target -- sanctionedRegions() cannot tell them apart "
                + "by bare name alone")
            .anySatisfy(h -> assertThat(h).contains("AMBIGUOUS SANCTIONED METHOD NAME")
                .contains("fetchShowConfig"));
    }

    /**
     * nexus-rfx2j falsification proof: {@link #RAW_EXECUTE}'s
     * {@code .resultQuery(} alternation previously matched only an
     * immediate opening quote, unlike its {@code .execute}/{@code .fetch}/
     * {@code .fetchOne}/{@code .fetchAny} siblings, which also match a
     * "sql"/"SQL"-prefixed variable. Reverting the added
     * {@code (\"|sql|SQL|new StringBuilder)} branch back to the old
     * {@code \"} -only form makes this test fail (zero violations) --
     * verified manually before applying the fix below and reverted
     * immediately after confirming it.
     */
    @Test
    void rawExecute_resultQuerySqlPrefixedVariable_isFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void probe() {",
            "        ctx.resultQuery(sqlProbe, bytes);",
            "    }",
            "}");
        List<String> hits = scan("SomeRepo.java", synthetic);
        assertThat(hits)
            .as(".resultQuery(sql-prefixed-variable) must match RAW_EXECUTE, consistent "
                + "with its .execute/.fetch/.fetchOne/.fetchAny siblings")
            .anySatisfy(h -> assertThat(h).contains("resultQuery"));
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
     * RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.41 items 3+4)
     * RETARGET of this canary: {@code nexus.chunks_384/768/1024} and {@code
     * nexus.taxonomy_centroids_384/768/1024} collapsed into ONE unified
     * table each ({@code nexus.chunks} / {@code nexus.taxonomy_centroids}),
     * three nullable typed embedding columns apiece. Dim is now a COLUMN
     * choice, not a TABLE identity — so "how many dim TABLES exist" (the
     * old canary's question, {@code ChashSqlIdioms.CHUNK_TABLES.hasSize(3)})
     * is no longer the drift signal a future dimension change needs. The
     * live signal is {@code DimTables.CHUNKS}/{@code CENTROIDS} map size
     * (pinned below); {@code CHUNK_TABLES} itself is now a single-element
     * list (see its own javadoc) kept only as a checklist artifact, not a
     * per-dim table-name enumeration.
     *
     * <p>nexus-o8dil.41 comment 7 (2026-08-13, [22416] Part-9-derived
     * finding): the OLD checklist covered ~16 sites against ~110 real
     * Phase-4 decision sites — about 15%, and it grew reactively, one
     * bullet per site that happened to bite someone. Rewritten here as a
     * list of CHANNELS (a category of place a dim/table fact can live) so a
     * future maintainer checks a CATEGORY, not a stale enumeration that
     * will always be one channel short of the next drift class. Each
     * channel names ONE representative live site as of this lane, not an
     * exhaustive inventory — grep the category, don't trust the example.
     *
     * <ul>
     *   <li><b>Typed jOOQ (single-table-instance-per-dim-key maps).</b>
     *       {@code dev.nexus.service.vectors.DimTables.CHUNKS} / {@code
     *       .CENTROIDS} — the SINGLE authority every typed call site reads
     *       through. A dimension-count change starts and ends here for the
     *       typed channel.</li>
     *   <li><b>Raw-string table names.</b> Sites that build a
     *       schema-qualified table name for string-concatenated SQL MUST
     *       consult {@code DimTables.CHUNKS_TABLE_NAME} / {@code
     *       .CENTROIDS_TABLE_NAME}, never re-derive {@code "nexus.chunks_"
     *       + dim}. Representative: {@code
     *       PgVectorRepository.chunksTable(int)}, {@code
     *       TaxonomyCentroidRepository}'s ANN query, {@code
     *       ChashRepository.PROBE_SQL}.</li>
     *   <li><b>Raw-string column names.</b> Sites selecting/ordering by the
     *       per-dim embedding column in raw SQL MUST consult {@code
     *       DimTables.embeddingColumn(int)}, never hand-roll {@code
     *       "embedding_" + dim} or bare {@code "embedding"}. Representative:
     *       {@code PgVectorRepository.searchWithTokens}/{@code
     *       hybridSearch}'s raw-SQL distance projections, {@code
     *       TaxonomyCentroidRepository.annQuery}.</li>
     *   <li><b>Stored-function bodies (Liquibase, {@code LANGUAGE sql} /
     *       {@code plpgsql}).</b> Nine typed combined-query facades kept
     *       per-dim DELIBERATELY (Decision 2, T2 [22445] — collapsing them
     *       would reintroduce F13's silent-seq-scan regression), plus
     *       {@code gc_quarantine_orphans}/{@code gc_restore_rereferenced}/
     *       {@code gc_expire_quarantine}, {@code assign_from_chashes_<dim>}
     *       (now retargeting BOTH unified tables per the o8dil.47 ruling),
     *       {@code purge_trash}, {@code chash_conformance_report}. A
     *       dimension-count change touches the changelog, not Java, for
     *       this channel.</li>
     *   <li><b>Views.</b> {@code nexus.live_chunks} (dim now DERIVED from
     *       which embedding column is non-null, not a UNION leg), {@code
     *       nexus.collection_vector_stats}.</li>
     *   <li><b>Name-lists (Java constants enumerating dim-bearing table
     *       names or dim ints).</b> {@code
     *       dev.nexus.service.db.TenantScope.VACUUM_ALLOWED_TABLES} / {@code
     *       CatalogRepository.PURGE_VACUUM_TABLES} (three-way lockstep with
     *       the grants channel below, {@code
     *       TenantScopeVacuumMaintainGrantParityTest}); {@code
     *       ChashCensus.KNOWN_INVENTORY} / {@code TEXT_EXCLUSIONS}; {@code
     *       CatalogRepository.MANIFEST_DIMS} / {@code
     *       COLLECTION_SCOPED_TABLES}; every {@code DIMS}/{@code
     *       VALID_DIMS} array (ChashRepository, TaxonomyCentroidRepository,
     *       RekeyOps, StagingPromoteOps) — each of these is a loop-over-dim
     *       site that, post-unification, MUST filter on {@code
     *       embedding_<dim> IS NOT NULL} rather than relying on table
     *       membership to scope a dim (the D1 hazard: looping the SAME
     *       unified table N times without that filter silently
     *       triples/N-tuples counts or duplicates rows).</li>
     *   <li><b>Grants.</b> {@code grants-nexus-svc.xml}'s {@code
     *       grants-005-chunks-unify-maintain} changeset (superseding {@code
     *       grants-003}'s frozen chunks_384/768/1024 MAINTAIN list, guarded
     *       by a checksum-neutral {@code preConditions}) — no centroid
     *       analog exists (verified: zero grants ever named a centroid
     *       table).</li>
     *   <li><b>Python taxonomy.</b> {@code chash_tables.py}'s collapsed
     *       inventory (RDR-191 E1, T2 [22463]) and its eight importers.</li>
     *   <li><b>Shell rehearsals.</b> The upgrade-ladder / migration-
     *       rehearsal loops that seed or assert per-dim table names
     *       directly (E3).</li>
     * </ul>
     */
    @Test
    void chunksSchemaCanary_dimensionCountChangeNeedsAllChannelsToldChecklistAbove() {
        assertThat(dev.nexus.service.vectors.DimTables.CHUNKS)
            .as("the typed-jOOQ channel's per-dim key set — the live authority for "
                + "\"how many embedding dimensions are supported\" now that dim is a "
                + "column choice, not a table identity")
            .hasSize(3);
        assertThat(dev.nexus.service.vectors.DimTables.CENTROIDS).hasSize(3);
        assertThat(ChashSqlIdioms.CHUNK_TABLES)
            .as("the unified chunks table is ONE physical table regardless of how "
                + "many dims it carries — this list stays single-element; a change "
                + "here means the unification itself was reverted, not that a dim "
                + "was added")
            .containsExactly(dev.nexus.service.vectors.DimTables.CHUNKS_TABLE_NAME);
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

    // ── nexus-4okz4 increment 5 post-review fix (critic Critical, nexus-8emxy,
    //    T2 critique-4okz4-increment5-2026-08-09): whole-method-body sentinels
    //    for raw-SQL ASSEMBLY funneled through a named non-execute/fetch-shaped
    //    wrapper ──

    /**
     * Whole-method-body sentinels: {@code file.java -> {method name ->
     * {expected canonical whole-body texts}}}. A STRUCTURALLY DIFFERENT
     * mechanism from {@link #SANCTIONED_STATEMENTS} (which fingerprints ONE
     * {@code .execute(}/{@code .fetch(}-shaped LITERAL call) — this exists
     * because {@code PgVectorRepository.searchWithTokens}/{@code
     * hybridSearch} build SQL DYNAMICALLY across many lines (a {@code
     * StringBuilder} chain, runtime-varying WHERE-predicate loops, a
     * selectivity-dependent branch between structurally different queries)
     * and funnel the result through the PRIVATE WRAPPER {@code
     * rawVectorFetch(ctx, sql, binds)} — called BY NAME, never via a
     * literal {@code .execute(}/{@code .fetch(} at the CALL site. {@link
     * #RAW_EXECUTE} anchors exclusively on those two call SHAPES, so it has
     * NO anchor inside {@code searchWithTokens}/{@code hybridSearch} at
     * all — not "excused," structurally INVISIBLE, statement-granular or
     * not. Confirmed empirically (nexus-8emxy falsification, 2026-08-09):
     * appending an entirely new raw-SQL predicate to {@code
     * searchWithTokens}'s {@code StringBuilder} produced ZERO gate signal;
     * {@code RawSqlGateTest} stayed 12/12 green with the probe in place.
     *
     * <p>This is a COARSER, whole-body tripwire, not a finer one: the
     * method's ENTIRE whitespace-collapsed body text must match one of the
     * registered snapshots EXACTLY. ANY edit — a new predicate, a changed
     * literal, a restructured branch — changes the canonical text and
     * fails the gate, forcing a maintainer to consciously update the
     * registration (and thus review the diff) before it can land. This is
     * STRONGER than statement-granular protection for these two methods
     * (it also catches an EDITED existing statement, not just an ADDED
     * one), traded against being unable to say WHICH statement inside the
     * body changed — appropriate for a method whose danger is the shape of
     * a dynamically-composed query, not a single fixed literal.
     *
     * <p>Registered under the bare method name — like {@link
     * #SANCTIONED_STATEMENTS}, this matches EVERY same-named overload
     * ({@link #sanctionedRegions} is name-only, not overload-aware).
     * {@code searchWithTokens}/{@code hybridSearch} each have several thin
     * one-line delegating overloads sharing the bare name with the one
     * real SQL-assembling implementation; all bodies found are registered
     * (harmless conservatism — a delegator's body is a natural, low-churn
     * additional tripwire, not overreach).
     */
    private static final Map<String, Map<String, java.util.Set<String>>> RAW_SQL_ASSEMBLY_SENTINELS =
        Map.of(
        "PgVectorRepository.java", Map.of(
            "searchWithTokens", java.util.Set.of(
                "{ return searchWithTokens(tenant, queryText, collectionNames, nResults, where, false); }",
                "{ if (collectionNames == null || collectionNames.isEmpty()) { return new Tokened<>(List.of(), 0"
                + "L); } int dim = dimForCollection(collectionNames.get(0)); for (String col : collectionNames) { "
                + "int colDim = dimForCollection(col); if (colDim != dim) { throw new IllegalArgumentException( \""
                + "mixed dimensions in one search call: '\" + collectionNames.get(0) + \"' is \" + dim + \"-dim bu"
                + "t '\" + col + \"' is \" + colDim + \"-dim - one query vector cannot serve both spaces\"); } } /"
                + "/ Route by the first collection - the same-dim check above guarantees the set is // homogeneous"
                + ", and the Python client never mixes embedder families in one call // (same convention as the Ch"
                + "roma path). EmbedResult embedResult = embedQuery(collectionNames.get(0), queryText, dim); float"
                + "[] queryVec = embedResult.embeddings().get(0); StringBuilder sql = new StringBuilder() // RDR-1"
                + "80: bytea storage — hex at the SQL seam (raw-SQL twin of // the ChashHex converted type the jOO"
                + "Q paths use). .append(\"SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata:"
                + ":text AS metadata_json,\") .append(\" (\").append(DimTables.embeddingColumn(dim)).append(\" <=>"
                + " ?::vector) AS distance\") .append(\" FROM \").append(chunksTable(dim)).append(\" c\") .append("
                + "\" WHERE c.collection IN (\").append(placeholders(collectionNames.size())).append(\")\") // RDR"
                + "-156 Decision 6 (nexus-3ck2g): live_chunks predicate, inlined so the // HNSW index scan on c st"
                + "ays engaged (see liveChunksPredicate's javadoc). .append(\" AND \").append(liveChunksPredicate("
                + "\"c\")) // nexus-74zvm DECISION (see method javadoc): exclude foreign-dim rows so a // NULL emb"
                + "edding_<dim> never produces a NULL-distance result under LIMIT. .append(\" AND \").append(DimTa"
                + "bles.embeddingColumn(dim)).append(\" IS NOT NULL\"); List<Object> binds = new ArrayList<>(); bi"
                + "nds.add(vectorLiteral(queryVec)); binds.addAll(collectionNames); if (where != null) { for (Map."
                + "Entry<String, Object> e : where.entrySet()) { appendWherePredicate(sql, binds, e.getKey(), e.ge"
                + "tValue()); } } sql.append(\" ORDER BY distance ASC, chash ASC LIMIT ?\"); binds.add(nResults); "
                + "Result<Record> result = tenantScope.withTenant(tenant, ctx -> { // Filtered-ANN recall: keep HN"
                + "SW scanning past ef_search when the RLS + // collection + metadata predicates narrow the candid"
                + "ate set. SET LOCAL is // txn-scoped (same pool discipline as the TenantScope GUC stamp). PgSess"
                + "ion.setLocal(ctx, \"hnsw.iterative_scan\", \"relaxed_order\"); return rawVectorFetch(ctx, sql.t"
                + "oString(), binds.toArray()); }); List<Map<String, Object>> rows = new ArrayList<>(result.size()"
                + "); for (Record rec : result) { Map<String, Object> row = new LinkedHashMap<>(); row.put(\"id\","
                + " rec.get(\"chash\", String.class)); row.put(\"content\", rec.get(\"chunk_text\", String.class))"
                + "; row.put(\"distance\", rec.get(\"distance\", Double.class)); row.put(\"collection\", rec.get("
                + "\"collection\", String.class)); row.putAll(fromJson(rec.get(\"metadata_json\", String.class)));"
                + " rows.add(row); } // RDR-169 G5: surface address triple additively (chash + span always; source"
                + "_uri opt-in) enrichSearchRows(tenant, rows, includeSourceUri); return new Tokened<>(rows, embed"
                + "Result.tokens()); }"),
            "hybridSearch", java.util.Set.of(
                "{ return hybridSearchWithTokens(tenant, queryText, collectionNames, nResults, where, false).value();"
                + " }",
                "{ return hybridSearch(tenant, queryText, collectionNames, nResults, where, selectiveGateMax, null); "
                + "}",
                "{ if (collectionNames == null || collectionNames.isEmpty()) { return List.of(); } // queryText "
                + "is bound twice below as a raw text parameter (plainto_tsquery + // trgm <%); a NUL-bearing quer"
                + "y would hit the same UTF8-0x00 rejection the // upsert path sanitizes (nexus-rvfwj sibling hole"
                + ", dual-review H1). queryText = stripNul(queryText); if (nResults < 1) { // LIMIT -1 is \"no lim"
                + "it\" in Postgres - a non-positive value would silently // unbound the query instead of capping "
                + "it. throw new IllegalArgumentException(\"nResults must be >= 1, got \" + nResults); } if (selec"
                + "tiveGateMax < 1) { // A non-positive threshold routes EVERY gate to the HNSW-first branch // (m"
                + "atchCount >= 0 is always > a non-positive cutoff), silently re-enabling // the lcogi selective-"
                + "gate collapse. Reject rather than mis-dispatch. throw new IllegalArgumentException( \"selective"
                + "GateMax must be >= 1, got \" + selectiveGateMax); } int dim = dimForCollection(collectionNames."
                + "get(0)); for (String col : collectionNames) { int colDim = dimForCollection(col); if (colDim !="
                + " dim) { throw new IllegalArgumentException( \"mixed dimensions in one hybrid-search call: '\" +"
                + " collectionNames.get(0) + \"' is \" + dim + \"-dim but '\" + col + \"' is \" + colDim + \"-dim "
                + "- one query vector cannot serve both spaces\"); } } EmbedResult hybridEmbed = embedQuery(collec"
                + "tionNames.get(0), queryText, dim); if (tokensOut != null) tokensOut[0] = hybridEmbed.tokens(); "
                + "float[] queryVec = hybridEmbed.embeddings().get(0); // Non-text scope (collection IN + metadata"
                + " where). Shared by the selective // rank-by-chash query (nexus-x7z7l): that query re-applies th"
                + "ese cheap predicates // but NOT the text gate - the gate's matching chashes already satisfy it,"
                + " so the // expensive <% trigram heap-recheck runs ONCE (in the bounded fetch below), not // aga"
                + "in at rank time. (The metadata->>? predicate is kept on the rank query, not // dropped: two sam"
                + "e-text rows in different collections share a chash, so chash // alone would not re-impose a per"
                + "-row metadata filter.) StringBuilder scope = new StringBuilder() .append(\" WHERE collection IN"
                + " (\").append(placeholders(collectionNames.size())).append(\")\"); List<Object> scopeBinds = new"
                + " ArrayList<>(collectionNames); // RDR-156 Decision 6 (nexus-3ck2g): live_chunks predicate folde"
                + "d into `scope` // BEFORE `gate` is derived from it below, so every downstream query built off /"
                + "/ either scope or gate — the bounded gate-selectivity probe, the selective // chash-ranked rank"
                + ", and the HNSW-first fallback, all three read sites — inherits // it by construction; a single "
                + "fix point instead of three. Inlined (never a JOIN // to nexus.live_chunks) so the HNSW/GIN scan"
                + "s on `c` (the alias assigned to // `table` below) stay engaged — see liveChunksPredicate's java"
                + "doc. scope.append(\" AND \").append(liveChunksPredicate(\"c\")); // Full gate = scope AND a tex"
                + "t signal. FTS lexeme match OR word-trigram similarity: // the <% operator form (word_similarity"
                + " >= pg_trgm.word_similarity_threshold) is // gin_trgm_ops-indexable (vectors-002) where the fun"
                + "ction-call form is not; // word_similarity (vs plain similarity) does not dilute with chunk_tex"
                + "t length. // The threshold GUC is pinned per-transaction below. StringBuilder gate = new String"
                + "Builder(scope) .append(\" AND (chunk_tsv @@ plainto_tsquery('english', ?) OR ? <% chunk_text)\""
                + "); List<Object> gateBinds = new ArrayList<>(scopeBinds); gateBinds.add(queryText); gateBinds.ad"
                + "d(queryText); if (where != null) { for (Map.Entry<String, Object> e : where.entrySet()) { appen"
                + "dWherePredicate(gate, gateBinds, e.getKey(), e.getValue()); appendWherePredicate(scope, scopeBi"
                + "nds, e.getKey(), e.getValue()); } } final String table = chunksTable(dim) + \" c\"; final Strin"
                + "g gateSql = gate.toString(); final String scopeSql = scope.toString(); final String vecLit = ve"
                + "ctorLiteral(queryVec); // SELECTIVITY-AWARE DISPATCH (nexus-lcogi; single-gate-eval, nexus-x7z7"
                + "l). ONE // bounded fetch of the gate's chashes (LIMIT SELECTIVE_GATE_MAX+1) both picks the // p"
                + "lan AND, for the selective case, IS the gate evaluation - the ranked query then // filters by c"
                + "hash (PK lookup), so the expensive <% trigram heap-recheck runs once, // not twice. The prior d"
                + "esign ran a standalone COUNT(*) probe AND re-ran the gate in // the ranked query: on a large co"
                + "de corpus that was two ~650ms <% heap-rechecks per // call (conexus-qsa EXPLAIN: count probe 70"
                + "0ms + materialized-CTE rank 654ms, both // dominated by the lossy gin_trgm_ops recheck over ~19"
                + "00 candidate rows). // // * SELECTIVE gate (matches <= SELECTIVE_GATE_MAX): the bounded fetch r"
                + "eturns the // COMPLETE gate (all matches, since it did not hit the LIMIT). Rank those exact // "
                + "chashes by cosine distance via a chash IN (...) filter + the cheap non-text // scope (collectio"
                + "n/metadata). No HNSW, no re-gate: ranks the small gated set // exactly, with NO dependence on h"
                + "nsw.max_scan_tuples (the lcogi collapse fix is // preserved - the prior HNSW-first single-query"
                + " plan starved selective gates). // // * NON-SELECTIVE gate (matches > SELECTIVE_GATE_MAX): the "
                + "bounded fetch hit the // LIMIT (returned SELECTIVE_GATE_MAX+1 chashes) and is discarded - keep "
                + "the // HNSW-first plan (gate as scan filter, iterative_scan). A dense gate is usually // found "
                + "within the scan budget; materializing a huge gated set (~4 KB/row // embeddings) would spill wo"
                + "rk_mem. The bounded fetch caps this probe's cost // (the prior unbounded COUNT scanned the full"
                + " dense gate). Same SEMI-selective // caveat as before applies; P5.2's RRF fusion closes that wi"
                + "ndow. // // matches == 0 -> empty gate -> selective branch returns an empty result (no silent /"
                + "/ vector fallback), handled explicitly (chash IN () is not valid SQL). List<Object> probeBinds "
                + "= new ArrayList<>(gateBinds); probeBinds.add(selectiveGateMax + 1); Result<Record> result = ten"
                + "antScope.withTenant(tenant, ctx -> { // Trigram gate calibration (contract anchor): word_simila"
                + "rity >= 0.6, pg_trgm's // default - typo-probe candidates sit at ~0.9 and pass, no-signal rows "
                + "at ~0.1 // do not. Pinned per-transaction so the gate is independent of cluster config. PgSessi"
                + "on.setLocal(ctx, \"pg_trgm.word_similarity_threshold\", \"0.6\"); List<String> gateChashes = ra"
                + "wVectorFetch( ctx, \"SELECT encode(chash, 'hex') AS chash FROM \" + table + gateSql + \" LIMIT "
                + "?\", probeBinds.toArray()) .map(r -> r.get(\"chash\", String.class)); if (gateChashes.size() <="
                + " selectiveGateMax) { // Selective: the bounded fetch returned the COMPLETE gate (the LIMIT did "
                + "NOT // fire - fewer than selectiveGateMax+1 matches exist, so it scanned the full // GIN candid"
                + "ate set, same work the old COUNT(*) did). The win is not a // cheaper probe: this single gate s"
                + "can REPLACES both the old COUNT(*) probe // AND the MATERIALIZED-CTE gate re-evaluation - the r"
                + "ank below filters by // chash with NO text gate, so the <% heap-recheck happens once, not twice"
                + ". if (gateChashes.isEmpty()) { // Empty gate: typed-empty result (chash IN () is invalid SQL). "
                + "return rawVectorFetch(ctx, \"SELECT NULL::text AS chash, NULL::text AS chunk_text,\" + \" NULL:"
                + ":text AS collection, NULL::text AS metadata_json,\" + \" NULL::float8 AS distance WHERE false\""
                + "); } // chash is NOT unique across collections (the table key is // (tenant_id, collection, cha"
                + "sh)): a multi-collection gate can return the // same chash from N collections. Dedup the IN lis"
                + "t - the collection scope in // scopeSql still yields one ranked row per (collection, chash). De"
                + "dup runs // AFTER the size-based dispatch so the selective/non-selective boundary stays // iden"
                + "tical to the old per-row COUNT(*). List<String> inChashes = gateChashes.stream().distinct().toL"
                + "ist(); String sql = \"SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::t"
                + "ext AS metadata_json,\" + \" (\" + DimTables.embeddingColumn(dim) + \" <=> ?::vector) AS distan"
                + "ce FROM \" + table + scopeSql + \" AND chash IN (\" + decodePlaceholders(inChashes.size()) + \""
                + ")\" // nexus-74zvm DECISION (see method javadoc): the text gate has no dim // awareness, so a f"
                + "oreign-dim row can match it — exclude here rather // than rank it with a NULL embedding_<dim> d"
                + "istance. + \" AND \" + DimTables.embeddingColumn(dim) + \" IS NOT NULL\" + \" ORDER BY distance"
                + " ASC, chash ASC LIMIT ?\"; List<Object> b = new ArrayList<>(); b.add(vecLit); b.addAll(scopeBin"
                + "ds); b.addAll(inChashes); b.add(nResults); return rawVectorFetch(ctx, sql, b.toArray()); } // H"
                + "NSW-first for a dense gate: keep HNSW scanning past ef_search. PgSession.setLocal(ctx, \"hnsw.i"
                + "terative_scan\", \"relaxed_order\"); String sql = \"SELECT encode(chash, 'hex') AS chash, chunk"
                + "_text, collection, metadata::text AS metadata_json,\" + \" (\" + DimTables.embeddingColumn(dim)"
                + " + \" <=> ?::vector) AS distance FROM \" + table + gateSql // nexus-74zvm DECISION (see method "
                + "javadoc): same NULL-distance guard as // the selective-gate branch above. + \" AND \" + DimTabl"
                + "es.embeddingColumn(dim) + \" IS NOT NULL\" + \" ORDER BY distance ASC, chash ASC LIMIT ?\"; Lis"
                + "t<Object> b = new ArrayList<>(); b.add(vecLit); b.addAll(gateBinds); b.add(nResults); return ra"
                + "wVectorFetch(ctx, sql, b.toArray()); }); List<Map<String, Object>> rows = new ArrayList<>(resul"
                + "t.size()); for (Record rec : result) { Map<String, Object> row = new LinkedHashMap<>(); row.put"
                + "(\"id\", rec.get(\"chash\", String.class)); row.put(\"content\", rec.get(\"chunk_text\", String"
                + ".class)); row.put(\"distance\", rec.get(\"distance\", Double.class)); row.put(\"collection\", r"
                + "ec.get(\"collection\", String.class)); row.putAll(fromJson(rec.get(\"metadata_json\", String.cl"
                + "ass))); rows.add(row); } return rows; }")));

    /** Per-file scan for {@link #RAW_SQL_ASSEMBLY_SENTINELS} violations:
     * every registered method's CURRENT whole-body canonical text must
     * match one of its declared snapshots exactly, and every declared
     * snapshot must be matched by some current body (symmetric, same
     * discipline as {@link #scan}'s stale-fingerprint sweep). */
    static List<String> scanAssemblySentinels(String fileName, String rawSource) {
        String blanked = blank(rawSource);
        Map<String, java.util.Set<String>> methods =
            RAW_SQL_ASSEMBLY_SENTINELS.getOrDefault(fileName, Map.of());
        List<String> violations = new ArrayList<>();
        for (var entry : methods.entrySet()) {
            String name = entry.getKey();
            java.util.Set<String> expected = entry.getValue();
            List<int[]> regions = sanctionedRegions(blanked, java.util.Set.of(name));
            if (regions.isEmpty()) {
                violations.add(fileName + "  SENTINEL METHOD NOT FOUND: " + name
                    + " -- registered in RAW_SQL_ASSEMBLY_SENTINELS but no method of "
                    + "that name exists in this file any more; remove or update the "
                    + "registration");
                continue;
            }
            java.util.Set<String> actual = new java.util.LinkedHashSet<>();
            for (int[] r : regions) {
                actual.add(canonicalStatementText(rawSource, r));
            }
            for (String a : actual) {
                if (!expected.contains(a)) {
                    violations.add(fileName + "  SENTINEL BODY CHANGED in " + name
                        + " -- this raw-SQL-assembling method's body no longer matches "
                        + "any registered snapshot; review the diff for a new/edited raw "
                        + "SQL fragment, then copy the canonical text below into "
                        + "RAW_SQL_ASSEMBLY_SENTINELS verbatim: " + a);
                }
            }
            for (String e : expected) {
                if (!actual.contains(e)) {
                    violations.add(fileName + "  STALE SENTINEL in " + name
                        + " -- a registered snapshot no longer matches any method body "
                        + "found; remove it: " + e);
                }
            }
        }
        return violations;
    }

    @Test
    void noUnreviewedRawSqlAssemblyChanges() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    violations.addAll(scanAssemblySentinels(
                        p.getFileName().toString(), Files.readString(p)));
                } catch (IOException e) {
                    throw new RuntimeException(e);
                }
            });
        }

        assertThat(violations)
            .as("a raw-SQL-ASSEMBLING method (StringBuilder-built SQL funneled through a "
                + "named non-execute/fetch wrapper) changed body since its "
                + "RAW_SQL_ASSEMBLY_SENTINELS snapshot was registered — review the diff for "
                + "a new or edited raw SQL fragment, then update the registration to the new "
                + "canonical text shown in the failure message")
            .isEmpty();
    }

    @Test
    void assemblySentinel_bodyChange_isFlagged() {
        String synthetic = String.join("\n",
            "public final class PgVectorRepository {",
            "    private void hybridSearch() {",
            "        rawVectorFetch(ctx, \"SELECT 1 WHERE definitely-not-the-real-body\");",
            "    }",
            "}");
        assertThat(scanAssemblySentinels("PgVectorRepository.java", synthetic))
            .as("a body that matches none of the registered snapshots must fail loud, "
                + "even though a method of that name IS registered")
            .anySatisfy(h -> assertThat(h).contains("SENTINEL BODY CHANGED")
                .contains("hybridSearch"));
    }

    @Test
    void assemblySentinel_unregisteredFileOrMethod_isUnaffected() {
        String synthetic = String.join("\n",
            "public final class SomeOtherClass {",
            "    void whatever() {",
            "        rawVectorFetch(ctx, \"anything\");",
            "    }",
            "}");
        assertThat(scanAssemblySentinels("SomeOtherClass.java", synthetic)).isEmpty();
    }
}
