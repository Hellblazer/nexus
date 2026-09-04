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
 * method rather than a literal call.
 *
 * <p><b>HISTORY (nexus-8emxy, 2026-08-09 through 2026-08-21; RETIRED nexus-zrcj7
 * step 4, 2026-09-03):</b> exactly one such wrapper ever existed in this codebase
 * — {@code PgVectorRepository.rawVectorFetch(ctx, sql, binds)}, called BY NAME
 * from {@code searchWithTokens}/{@code hybridSearch}, which {@code RAW_EXECUTE}
 * could not match at all (confirmed empirically: appending an entirely new
 * raw-SQL predicate to {@code searchWithTokens}'s {@code StringBuilder} produced
 * ZERO gate signal). Two mechanisms closed it: a whole-method-body sentinel map
 * (the exact canonical body text of every raw-SQL-assembling method, so ANY
 * edit — not just a new statement — forced a reviewed registration update) and
 * a companion wrapper-call-site scan (found every invocation of the wrapper's
 * own name and required its enclosing method to already be sentineled, so a
 * brand-new caller failed loud instead of producing zero signal). Both {@code
 * searchWithTokens} and {@code hybridSearch} are retired onto generated jOOQ
 * function tables (nexus-zrcj7 step 1: {@code plain_search_<dim>}/{@code
 * text_gated_search_<dim>}, vectors-009/011) — {@code rawVectorFetch} itself is
 * deleted, {@link #wrapperCallSites_realPgVectorRepository_zeroRawVectorFetchCallSitesRemain}
 * pins that zero call sites remain in the real tree, and both maps (having had
 * exactly one entry between them, now gone) were deleted outright at step 4
 * rather than kept as permanently-empty machinery — recoverable from git
 * history at the commit immediately preceding the deletion if a future raw-SQL-
 * assembling wrapper ever needs this same shape of proof again. {@link
 * #wrapperCallSites} survives as the one piece of that mechanism with an
 * independent, standalone use.
 *
 * <p><b>DSL-TEMPLATE SCAN (nexus-zrcj7 step 4, 2026-09-03; closes a residual
 * documented by nexus-8emxy's 2026-08-21 critique):</b> jOOQ's plain-SQL-template
 * overloads — {@code DSL.field("...", Class, binds...)}, {@code
 * DSL.condition("...", binds...)}, {@code DSL.query("...")}, {@code
 * DSL.table("...", binds...)} — parse a Java string as raw SQL text and were
 * invisible to every check above (none of them anchor on this call shape at
 * all). A census at the time (2026-08-21) found 67 call sites across 5 files
 * taking an immediate string-literal first argument to one of these methods:
 * 60 were bare identifier references with no space or operator (e.g. {@code
 * DSL.field("EXCLUDED.name", String.class)}, the Postgres {@code ON CONFLICT}
 * pseudo-table, hardcoded and never runtime-varying) and left alone; the
 * remaining handful genuinely assembled SQL text (an operator or a
 * space-separated function call, e.g. {@code "metadata ->> {0}"} or {@code
 * "GREATEST(a, b)"}) and were converted onto typed DSL this same step (see
 * {@link #EXEMPTION_REGISTRY}'s javadoc for what, if anything, remains
 * unconverted). {@link #scanDslTemplates} / {@link #looksLikeAssembledSql} close
 * this gap going forward — see {@link #noRawSqlDslTemplatesInMainOrTestSources}.
 *
 * <p><b>KNOWN RESIDUAL — a call shape this gate still does not scan.</b>
 * {@link #wrapperCallSites} matches direct invocations ({@code name(...)}) via
 * a {@code \bname\s*\(} pattern; a method reference to a raw-SQL wrapper
 * ({@code this::wrapperName} or {@code SomeClass::wrapperName} passed where a
 * functional interface is expected) has no {@code (} immediately following the
 * name and does not match at all. No such reference exists in the codebase
 * today; if one is ever introduced calling a raw-SQL-assembling wrapper, it
 * would be invisible to this gate.
 *
 * <p>Each sanctioned method's REGISTRATION (a key in {@link
 * #SANCTIONED_STATEMENTS}) still needs a {@code // SANCTIONED RAW
 * (nexus-mzuj9): <why>} comment at its definition site (auditable, not
 * silent). One method carries a registration with a genuinely EMPTY
 * statement multiset — {@code ChashRepository.lookup} — see its entry below
 * for why: it is a real, deliberate raw-SQL primitive, but this gate's
 * execute/fetch-call-shape detector structurally never observes a matching
 * call site inside the method's OWN body (its argument is a named constant,
 * not a literal, which evades the name-based heuristic above — a
 * pre-existing, documented KNOWN RESIDUAL). NOT kept for detection speed — a
 * registered zero-fingerprint entry and no entry at all behave IDENTICALLY
 * at scan time (both fail any future literal match immediately). Kept
 * instead because the registration is this class's own documented
 * AUDIT-TRAIL invariant — "a handful of read sites genuinely cannot be
 * expressed as typed jOOQ DSL... named here explicitly" (this class's own
 * top-level javadoc; see also {@link #EXEMPTION_REGISTRY}, the same
 * inventory promoted to a checked structure at step 4).
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
     * <p>nexus-zrcj7 step 4 (Sam's no-SQL-strings-in-Java directive, widened scan
     * deliverable): added {@code .prepareStatement("...")/.prepareStatement(sql...)} --
     * raw JDBC's SQL-TEXT-BEARING call. {@code Connection.createStatement()} itself never
     * carries SQL text (the text arrives on the SUBSEQUENT {@code .execute("...")}/
     * {@code .executeQuery("...")}/{@code .executeUpdate("...")} call against the
     * returned {@code Statement}, already covered by the alternatives above), but
     * {@code PreparedStatement}'s SQL text is bound once, at {@code prepareStatement(...)}
     * itself, and its later {@code .executeQuery()}/{@code .executeUpdate()} calls take
     * NO string argument at all -- structurally invisible to every alternative above
     * before this addition (confirmed empirically: {@code SchemaMigrator}'s three
     * {@code conn.prepareStatement("...")} call sites -- all three now either
     * SANCTIONED_STATEMENTS-registered ({@code preflightChashConstraints}) or converted
     * onto typed DSL ({@code countChangelogRowsSince}, this same review round -- see
     * {@link #EXEMPTION_REGISTRY} for the surviving exemption set) -- produced ZERO gate
     * signal before this addition).
     *
     * <p>nexus-zrcj7 step 4 review follow-up (critic, T2 [24235]): added jOOQ's plain-SQL
     * predicate/sort overloads -- {@code .where("...", binds...)}, {@code .and("...",
     * binds...)}, {@code .or("...", binds...)}, {@code .having("...", binds...)}, {@code
     * .orderBy("...")}. Each has a TYPED sibling overload taking a {@code Condition}/
     * {@code Field}/{@code SortField} argument, never a bare string literal -- the
     * anchor's immediate-quote requirement matches only the raw-string form, so a typed
     * call like {@code .where(TABLE.COL.eq(1))} never matches at all (see the pos/neg
     * synthetic pairs at {@link #rawExecute_whereRawStringOverload_isFlagged} and its four
     * siblings).
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
        + "|\\.resultQuery\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.prepareStatement\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.where\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.and\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.or\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.having\\(\\s*(\"|sql|SQL|new StringBuilder)"
        + "|\\.orderBy\\(\\s*(\"|sql|SQL|new StringBuilder))",
        Pattern.DOTALL);


    // ── nexus-zrcj7 step 4 (Sam directive 2026-09-03): the DRAFT, non-enforcing
    //    step-4 exemption-list comment that lived here through steps 1-3 is
    //    PROMOTED to the checked, enforced {@link #EXEMPTION_REGISTRY} below (a
    //    Java structure this class actually reads and verifies, not a sibling doc
    //    comment). Both entries the draft called "convertible, not true exemptions"
    //    (TaxonomyRepository.advanceTopicsIdSequence, TaxonomyCentroidRepository.
    //    annQuery) are CONVERTED this cycle, not carried forward as exemptions;
    //    CatalogRepository's two GREATEST(...) raw-text templates (found by this
    //    cycle's widened DSL-template scan, {@link #scanDslTemplates}) and
    //    PgVectorRepository.metadataCondition's `metadata ->> {0}` accessor +
    //    range-operator template (this class's own KNOWN RESIDUAL history) are
    //    likewise converted, not exempted. See {@link #EXEMPTION_REGISTRY}'s own
    //    javadoc for the resulting checked exemption set and its reduce-only
    //    discipline.

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
        // PgVectorRepository.java's "rawVectorFetch" entry: REMOVED (nexus-zrcj7,
        // 2026-09-03). rawVectorFetch (the single execution chokepoint for
        // searchWithTokens()/hybridSearch()'s raw SQL) is deleted outright --
        // both callers now go through generated jOOQ function tables
        // (plain_search_<dim>/text_gated_search_<dim>, vectors-009/011) like every
        // other combined-query shape. Per the dead-entry-avoidance discipline this
        // class already established (RekeyOps.java's entry, below), a registration
        // for a deleted method is removed outright, not kept as a no-op.
        //
        // TaxonomyCentroidRepository.java's "annQuery" entry: REMOVED (nexus-zrcj7
        // step 4, 2026-09-03). annQuery's raw ctx.fetch(...) (the pgvector `<=>`
        // distance query, string-concatenated table/column names) is retired onto
        // nexus.taxonomy_ann_query_<dim> (vectors-013), a generated jOOQ function
        // table -- same conversion, same reasoning, and the same dead-entry-
        // avoidance discipline as the rawVectorFetch removal immediately above.
        Map.entry("CatalogRepository.java", Map.of(
            // nexus-zrcj7: acquireIndexRunLock's entry (SANCTIONED RAW,
            // nexus-5xn3k.2 — pg_advisory_xact_lock over a hashtext'd
            // (tenant, doc_id) key) is REMOVED here: the method was retired
            // from a raw ctx.execute(...) onto the same typed
            // DSL.function(...) composition acquireSweepGateShared/
            // acquireSweepGateExclusive already use, so it no longer
            // matches RAW_EXECUTE at all — leaving the entry would be a
            // STALE SANCTIONED FINGERPRINT.
            //
            // SANCTIONED RAW (RDR-191 Phase 5, nexus-o8dil.29): SET CONSTRAINTS is
            // PostgreSQL transaction-control syntax with no jOOQ typed-DSL form —
            // same category as SchemaMigrator's NO FORCE/FORCE ROW LEVEL SECURITY
            // entry below (verified again, nexus-zrcj7: no matching jOOQ 3.21 DSL
            // method found via Context7). Deferred-constraint fix for
            // deleteCollectionTxn's chunk-before-manifest ordering under
            // fk_catalog_chunks_chunk (class-B site 2 — see the method's own
            // javadoc for the full derivation).
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
            // is DDL jOOQ has no typed-DSL form for at all. Two DISTINCT ALTER TABLE
            // statements (NO FORCE / FORCE), each executed once per loop iteration over
            // CHASH_LEN_CONSTRAINTS at runtime but appearing exactly once each in the
            // SOURCE — the fingerprint is a source-level construct, not a runtime count.
            // nexus-zrcj7 step 4 (widened-scan follow-up): the method's two
            // conn.prepareStatement("...") reads (existsNotValid probe against
            // pg_constraint; the pre-fix violatingCount COUNT against the target table)
            // were structurally invisible to RAW_EXECUTE before this cycle's
            // .prepareStatement(...) alternative was added (this class's own KNOWN
            // RESIDUAL history) -- both are the SAME bootstrap-JDBC-Connection /
            // system-catalog category as the rest of this method and SchemaMigrator's
            // other entries, registered here rather than left invisible.
            "preflightChashConstraints", Map.of(
                ".execute(\"ALTER TABLE nexus.\" + table + \" NO FORCE ROW LEVEL SECURITY\")", 1,
                ".execute(\"ALTER TABLE nexus.\" + table + \" FORCE ROW LEVEL SECURITY\")", 1,
                ".prepareStatement( \"SELECT NOT convalidated FROM pg_constraint WHERE conname = ?\")", 1,
                ".prepareStatement( \"SELECT COUNT(*) FROM nexus.\" + table + \" WHERE length(chash) != 32\")", 1),
            // SANCTIONED RAW (nexus-rph82): SET TIME ZONE is PostgreSQL session
            // syntax with no jOOQ typed-DSL form. Pins the migration connection's
            // session zone to UTC so databasechangelog.dateexecuted (stamped via
            // the server's now() rendered in the SESSION zone) is not JVM-local
            // against a GMT database — pgjdbc negotiates the session zone from
            // the JVM default at CONNECT time, so a pool opened before
            // pinJvmTimeZoneToUtc() still carries the old zone; this pins the
            // session directly. One statement, executed once per migrate() call.
            "migrate", Map.of(
                ".execute(\"SET TIME ZONE 'UTC'\")", 1))),
        // SchemaMigrator.java's "countChangelogRows"/"serverNow"/"countChangelogRowsSince"
        // entries: REMOVED (nexus-zrcj7 step 4 review follow-up, critic T2 [24235]). All
        // three retired onto DSL.using(conn, SQLDialect.POSTGRES) over the SAME bare
        // bootstrap Connection -- the architectural constraint (no DSLContext exists yet)
        // never actually precluded typed DSL, since jOOQ wraps any plain Connection. See
        // SchemaMigrator.java's own comment at these three methods for the full derivation.
        // TaxonomyRepository.java's "advanceTopicsIdSequence" entry: REMOVED
        // (nexus-zrcj7 step 4, 2026-09-03). Retired onto typed jOOQ DSL --
        // DSL.function("setval"/"pg_get_serial_sequence", ...) + DSL.greatest(...)
        // + a type-safe field(Select) scalar subquery over DSL.table(DSL.name(...))
        // -- same dead-entry-avoidance discipline as the removals above.
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

    // ── nexus-zrcj7 step 4 review follow-up (critic, T2 [24235]): jOOQ's plain-SQL
    //    predicate/sort overloads (.where/.and/.or/.having/.orderBy(String, ...)) --
    //    pos/neg synthetic pairs, one per method, mirroring the resultQuery proof above ──

    @Test
    void rawExecute_whereRawStringOverload_isFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void danger() {",
            "        ctx.selectFrom(TABLE).where(\"col = ?\", val).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic))
            .as(".where(\"...\", ...) -- jOOQ's plain-SQL predicate overload -- must fail loud")
            .anySatisfy(h -> assertThat(h).contains(".where("));
    }

    @Test
    void rawExecute_whereTypedConditionOverload_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void safe() {",
            "        ctx.selectFrom(TABLE).where(TABLE.COL.eq(1)).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic))
            .as(".where(Condition) -- the typed sibling overload -- must never match: no "
                + "string literal follows the open paren")
            .isEmpty();
    }

    @Test
    void rawExecute_andRawStringOverload_isFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void danger() {",
            "        ctx.selectFrom(TABLE).where(cond).and(\"col = ?\", val).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic))
            .as(".and(\"...\", ...) must fail loud")
            .anySatisfy(h -> assertThat(h).contains(".and("));
    }

    @Test
    void rawExecute_andTypedConditionOverload_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void safe() {",
            "        ctx.selectFrom(TABLE).where(cond).and(TABLE.COL.eq(1)).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic)).isEmpty();
    }

    @Test
    void rawExecute_orRawStringOverload_isFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void danger() {",
            "        ctx.selectFrom(TABLE).where(cond).or(\"col = ?\", val).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic))
            .as(".or(\"...\", ...) must fail loud")
            .anySatisfy(h -> assertThat(h).contains(".or("));
    }

    @Test
    void rawExecute_orTypedConditionOverload_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void safe() {",
            "        ctx.selectFrom(TABLE).where(cond).or(TABLE.COL.eq(2)).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic)).isEmpty();
    }

    @Test
    void rawExecute_havingRawStringOverload_isFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void danger() {",
            "        ctx.selectFrom(TABLE).groupBy(TABLE.COL).having(\"count(*) > ?\", n).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic))
            .as(".having(\"...\", ...) must fail loud")
            .anySatisfy(h -> assertThat(h).contains(".having("));
    }

    @Test
    void rawExecute_havingTypedConditionOverload_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void safe() {",
            "        ctx.selectFrom(TABLE).groupBy(TABLE.COL).having(TABLE.COL.gt(1)).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic)).isEmpty();
    }

    @Test
    void rawExecute_orderByRawStringOverload_isFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void danger() {",
            "        ctx.selectFrom(TABLE).orderBy(\"col DESC\").fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic))
            .as(".orderBy(\"...\") must fail loud")
            .anySatisfy(h -> assertThat(h).contains(".orderBy("));
    }

    @Test
    void rawExecute_orderByTypedFieldOverload_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class SomeRepo {",
            "    void safe() {",
            "        ctx.selectFrom(TABLE).orderBy(TABLE.COL.asc()).fetch();",
            "    }",
            "}");
        assertThat(scan("SomeRepo.java", synthetic)).isEmpty();
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

    // ── nexus-zrcj7 step 4 (Sam's no-SQL-strings-in-Java directive): widened scan for
    //    DSL.field/DSL.condition/DSL.query/DSL.table string-literal templates that carry
    //    ASSEMBLED SQL TEXT rather than a bare protocol-constant identifier reference.
    //    Scans BOTH src/main AND src/test -- the earlier RAW_EXECUTE-family scans
    //    ({@link #noRawExecuteSqlInMainSources} etc.) stay src/main-only this cycle; see
    //    this test's own javadoc for why the test-source population is a separate,
    //    reported (not silently absorbed) finding ──

    /**
     * Marks a {@code DSL.field}/{@code DSL.condition}/{@code DSL.query}/{@code
     * DSL.table} string-literal FIRST argument as assembled SQL TEXT rather than a bare
     * identifier/pseudo-column reference: any whitespace character, a substring matching
     * one of PostgreSQL's comparison/JSON operators, or one of {@code (}/{@code ,}/{@code
     * '} — a function-call or literal-bearing shape carries real SQL text even with no
     * space and no comparison operator at all (critic finding, nexus-zrcj7 review of
     * commit 3ae405673, T2 critique-24233: the original whitespace-or-operator check
     * alone let a space-free call template like {@code "GREATEST(a,b)"} or {@code
     * "nextval('nexus.seq')"} slip through undetected). Verified against the live tree's
     * census (nexus-zrcj7 gate javadoc history, 67 call sites / 5 files): 60 bare
     * references like {@code "EXCLUDED.name"} or {@code "catalog_owners.next_seq"} have
     * NEITHER a space, an operator, NOR a paren/comma/quote, and are left alone; every
     * genuine fragment this bead converted ({@code "metadata ->> {0}"}, {@code
     * "GREATEST(catalog_owners.next_seq, EXCLUDED.next_seq)"}, {@code
     * "CAST(split_part(tumbler_prefix, '.', 2) AS INTEGER)"}) has at least one trigger.
     * The pass-through set stays exactly the letters/digits/underscore/dot a bare or
     * dotted identifier is made of (plus double-quoted-identifier syntax, which uses
     * {@code "} rather than {@code (}/{@code ,}/{@code '} and so is unaffected by this
     * widening) — nothing else survives unflagged.
     */
    private static final String[] SQL_OPERATOR_SUBSTRINGS =
        {"->>", "->", "<=", ">=", "<>", "!=", "::", "=", "<", ">", "(", ",", "'"};

    static boolean looksLikeAssembledSql(String literalBody) {
        if (literalBody.chars().anyMatch(Character::isWhitespace)) {
            return true;
        }
        for (String op : SQL_OPERATOR_SUBSTRINGS) {
            if (literalBody.contains(op)) {
                return true;
            }
        }
        return false;
    }

    /** The literal TEXT between a string literal's opening quote (at {@code
     * openQuoteIdx}) and its matching unescaped closing quote -- delimiters excluded,
     * escape sequences copied through verbatim (mirrors {@link #firstArg}'s own
     * quote-skipping, narrowed to just the literal body). */
    static String stringLiteralBody(String src, int openQuoteIdx) {
        StringBuilder sb = new StringBuilder();
        int i = openQuoteIdx + 1;
        while (i < src.length() && src.charAt(i) != '"') {
            if (src.charAt(i) == '\\' && i + 1 < src.length()) {
                sb.append(src.charAt(i));
                i++;
            }
            sb.append(src.charAt(i));
            i++;
        }
        return sb.toString();
    }

    /** Per-file scan: every {@code DSL.(field|condition|query|table)("...")} call site
     * whose string-literal first argument is flagged by {@link #looksLikeAssembledSql},
     * PLUS every {@code DSL.sql(...)} call site unconditionally (critic finding,
     * nexus-zrcj7 step 4 review, T2 [24235]: {@code DSL.sql(...)} is jOOQ's raw-SQL-
     * fragment constructor -- it is raw SQL BY DEFINITION regardless of its argument's
     * content, so unlike {@code DSL.field}/{@code condition}/{@code query}/{@code table}
     * there is no bare-identifier pass-through category for it at all; any use is a hit).
     * Runs against {@link #blankComments} (like {@link #scanInlineNonLiteralArgs}) so a
     * javadoc/comment MENTION of one of these templates is never mistaken for a live
     * call site. {@code DSL.table(DSL.name(...))} -- the safe quoted-identifier idiom
     * {@code ChashCensus.java}/{@code StagingPromoteOps.java} already use for dynamic
     * relation names -- never matches: its first argument is a nested call, not an
     * immediate string literal. */
    static List<String> scanDslTemplates(String fileName, String rawSource) {
        String commentsBlanked = blankComments(rawSource);
        List<String> violations = new ArrayList<>();
        Matcher m = Pattern.compile("\\bDSL\\.(field|condition|query|table)\\(\\s*\"")
            .matcher(commentsBlanked);
        while (m.find()) {
            int openQuote = m.end() - 1;
            String body = stringLiteralBody(commentsBlanked, openQuote);
            if (!looksLikeAssembledSql(body)) {
                continue;
            }
            int line = 1 + (int) commentsBlanked.substring(0, m.start()).chars()
                .filter(c -> c == '\n').count();
            violations.add(fileName + ":" + line + "  DSL." + m.group(1) + "(\"" + body
                + "\", ...) -- string-literal argument carries assembled SQL text (a space "
                + "or an operator), not a bare identifier reference; move it onto a typed "
                + "jOOQ DSL call (DSL.jsonbGetAttribute(AsText)/DSL.greatest/DSL.function/"
                + "DSL.excluded and friends) or, if the expression genuinely has no typed "
                + "form, into a Liquibase function + generated jOOQ routine table (see "
                + "EXEMPTION_REGISTRY for the genuinely-unavoidable exception list)");
        }
        // DSL.sql(...) needs no argument-content check (unconditionally raw), so this
        // scans the FULLY blanked source (blank(), not blankComments()) -- unlike the
        // field/condition/query/table matcher above, which needs the argument literal's
        // CONTENT visible. Matching against blank() also protects this class's OWN
        // synthetic test fixtures: a fixture string like {@code "... DSL.sql(\"1 = 1\")
        // ..."} embeds the TEXT "DSL.sql(" inside an outer Java string literal in THIS
        // file's own source, which blank() blanks out along with every other string
        // literal's contents -- a live call site in real code is never inside a string
        // literal, so it stays visible.
        String fullyBlanked = blank(rawSource);
        Matcher sqlMethod = Pattern.compile("\\bDSL\\.sql\\(").matcher(fullyBlanked);
        while (sqlMethod.find()) {
            int line = 1 + (int) fullyBlanked.substring(0, sqlMethod.start()).chars()
                .filter(c -> c == '\n').count();
            violations.add(fileName + ":" + line + "  DSL.sql(...) -- jOOQ's raw-SQL-"
                + "fragment escape hatch is raw SQL by definition, regardless of its "
                + "argument's content; there is no bare-identifier pass-through for this "
                + "call -- move it onto a typed jOOQ DSL call or register a genuine "
                + "EXEMPTION_REGISTRY entry");
        }
        return violations;
    }

    /**
     * Scans BOTH {@code src/main/java} AND {@code src/test/java} for {@link
     * #scanDslTemplates} violations. Unlike the RAW_EXECUTE-family scans, this
     * detector's src/test walk is SAFE to enable today: a census taken before this
     * scan existed found ZERO real {@code DSL.field}/{@code DSL.condition}/{@code
     * DSL.query}/{@code DSL.table} string-literal-first-argument call sites anywhere
     * under {@code src/test/java} (the only hits were this class's OWN javadoc
     * mentions, already excluded by the {@link #blankComments} pass). Extending
     * {@link #noRawExecuteSqlInMainSources}'s RAW_EXECUTE-family walk to src/test the
     * same way would NOT be safe today — a census taken the same session found 554
     * pre-existing {@code .execute}/{@code .fetch}/{@code .resultQuery}-family raw-SQL
     * call sites across 129 test files (EXPLAIN-based plan-shape harnesses, Testcontainers
     * bootstrap DDL, Liquibase-driven schema tests), none reviewed or registered in any
     * exemption structure. Widening that walk is a SEPARATE, much larger piece of work
     * this bead's step 4 scope does not cover — reported, not silently absorbed here nor
     * silently left unmentioned (see this session's final report / T2 write-back for the
     * finding, per "stop and report" rather than either exploding the build or fabricating
     * an unreviewed 554-entry exemption list).
     */
    @Test
    void noRawSqlDslTemplatesInMainOrTestSources() throws IOException {
        List<String> violations = new ArrayList<>();
        for (String root : List.of("main", "test")) {
            Path r = Path.of("src", root, "java");
            assertThat(r).exists();
            try (Stream<Path> files = Files.walk(r)) {
                files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                    try {
                        violations.addAll(scanDslTemplates(
                            p.getFileName().toString(), Files.readString(p)));
                    } catch (IOException e) {
                        throw new RuntimeException(e);
                    }
                });
            }
        }
        assertThat(violations)
            .as("DSL.field/DSL.condition/DSL.query/DSL.table string-literal templates "
                + "carrying assembled SQL text -- see scanDslTemplates's own javadoc")
            .isEmpty();
    }

    @Test
    void dslTemplate_operatorTemplate_isFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void danger() {",
            "        Field<String> mv = DSL.field(\"metadata ->> {0}\", String.class, key);",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("an operator embedded in a DSL.field string template must fail loud")
            .anySatisfy(h -> assertThat(h).contains("metadata ->> {0}"));
    }

    @Test
    void dslTemplate_greatestFunctionCallTemplate_isFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void danger() {",
            "        Field<Long> f = DSL.field(\"GREATEST(a.x, EXCLUDED.x)\", Long.class);",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a space-separated function-call template must fail loud even with no "
                + "explicit comparison operator")
            .anySatisfy(h -> assertThat(h).contains("GREATEST"));
    }

    @Test
    void dslTemplate_bareExcludedIdentifierReference_isExcusedWithoutAnyRegistration() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void safe() {",
            "        Field<String> f = DSL.field(\"EXCLUDED.name\", String.class);",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a bare ON CONFLICT pseudo-table identifier reference (no space, no "
                + "operator) is not assembled SQL text and needs no exemption entry")
            .isEmpty();
    }

    /** Critic finding, T2 critique-24233: a SPACE-FREE function-call template (no
     * comparison operator, no whitespace at all) must still fail loud — the pre-widening
     * check missed exactly this shape. */
    @Test
    void dslTemplate_spaceFreeFunctionCallTemplate_isFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void danger() {",
            "        Field<Long> f = DSL.field(\"GREATEST(a,b)\", Long.class);",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a space-free, operator-free function-call template (a bare comma and "
                + "parens) must still fail loud")
            .anySatisfy(h -> assertThat(h).contains("GREATEST(a,b)"));
    }

    /** Critic finding, T2 critique-24233: a space-free literal-bearing template
     * ({@code nextval('nexus.seq')}) must fail loud too — the single-quoted SQL string
     * literal it carries is exactly the shape the pre-widening check missed alongside
     * the space-free function call above. */
    @Test
    void dslTemplate_spaceFreeLiteralBearingTemplate_isFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void danger() {",
            "        Field<Long> f = DSL.field(\"nextval('nexus.seq')\", Long.class);",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a space-free template embedding a single-quoted SQL literal must fail loud")
            .anySatisfy(h -> assertThat(h).contains("nextval"));
    }

    /** A bare DOTTED identifier reference (no {@code EXCLUDED.} prefix, no space, no
     * operator, no paren/comma/quote) stays excused — the pass-through category is
     * "letters/digits/underscore/dot", not merely "starts with EXCLUDED". */
    @Test
    void dslTemplate_bareDottedIdentifierReference_isExcusedWithoutAnyRegistration() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void safe() {",
            "        Field<Long> f = DSL.field(\"catalog_owners.next_seq\", Long.class);",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a bare dotted table.column identifier reference (no space, no operator, "
                + "no paren/comma/quote) is not assembled SQL text and needs no exemption "
                + "entry")
            .isEmpty();
    }

    @Test
    void dslTemplate_dslNameIdiomForDynamicRelationNames_isNeverMatched() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void safe() {",
            "        Table<?> t = DSL.table(DSL.name(\"staging\", \"chunks\"));",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("DSL.table(DSL.name(...)) is the safe quoted-identifier idiom, not a "
                + "string-literal template -- its first argument is a nested call, never "
                + "an immediate string literal, so the detector's own regex anchor never "
                + "matches it")
            .isEmpty();
    }

    @Test
    void dslTemplate_javadocMentionOfATemplate_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    /** formerly a raw {@code DSL.field(\"GREATEST(a, b)\", Long.class)} template */",
            "    void safe() {",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a javadoc/comment MENTION of a retired template must never be mistaken "
                + "for a live call site")
            .isEmpty();
    }

    /** Critic finding, T2 [24235]: {@code DSL.sql(...)} is raw SQL by definition --
     * unconditionally flagged, with no bare-identifier pass-through category at all
     * (unlike {@code DSL.field}/{@code condition}/{@code query}/{@code table}, whose
     * argument content decides the verdict). */
    @Test
    void dslTemplate_dslSqlEscapeHatch_isAlwaysFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    void danger() {",
            "        ctx.select(DSL.field(\"x\", Integer.class)).where(DSL.sql(\"1 = 1\")).fetch();",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("DSL.sql(...) must fail loud unconditionally")
            .anySatisfy(h -> assertThat(h).contains("DSL.sql(...)"));
    }

    @Test
    void dslTemplate_dslSqlJavadocMention_isNotFlagged() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    /** formerly used {@code DSL.sql(\"1 = 1\")} here */",
            "    void safe() {",
            "    }",
            "}");
        assertThat(scanDslTemplates("Whatever.java", synthetic))
            .as("a javadoc/comment MENTION of DSL.sql(...) must never be mistaken for a "
                + "live call site")
            .isEmpty();
    }

    // ── nexus-zrcj7 step 4 (Sam's no-SQL-strings-in-Java directive): the checked,
    //    ENFORCED exemption registry, promoted from the DRAFT comment this class
    //    carried through steps 1-3 ──

    /**
     * One raw-SQL-bearing method that remains genuinely UNCONVERTIBLE today.
     *
     * @param file        the bare file name, as used by {@link #SANCTIONED_STATEMENTS}'s
     *                    own keys (all six entries below live under {@code
     *                    dev.nexus.service.db}).
     * @param method      the exempted method's bare name.
     * @param reason      one-line justification (why no typed jOOQ DSL form exists) --
     *                    say specifically WHY no typed form exists (DDL, session syntax,
     *                    an admin meta-command, a system catalog jOOQ codegen does not
     *                    model), never a generic "runs before a DSLContext exists" --
     *                    that framing is a timing/sequencing fact about the ORIGINAL
     *                    code, not a reason typed DSL is impossible: any bare {@code
     *                    java.sql.Connection} can be wrapped via {@code DSL.using(conn,
     *                    dialect)} at any point in its lifecycle (nexus-zrcj7 step 4
     *                    review follow-up, critic T2 [24235] -- SchemaMigrator's
     *                    countChangelogRows/countChangelogRowsSince/serverNow carried
     *                    exactly this false reason and are converted, not exempted, as
     *                    of this commit).
     * @param convertible {@code false} for every entry today (every convertible site
     *                    census turned up this cycle -- CatalogRepository's two
     *                    GREATEST(...) templates, PgVectorRepository.metadataCondition,
     *                    TaxonomyRepository.advanceTopicsIdSequence,
     *                    TaxonomyCentroidRepository.annQuery, and SchemaMigrator's
     *                    databasechangelog/clock reads above -- is converted, not
     *                    exempted; the field stays for a FUTURE finding that is real but
     *                    not yet acted on, never as a place to park something merely
     *                    inconvenient).
     */
    record ExemptionEntry(String file, String method, String reason, boolean convertible) {}

    /**
     * REDUCE ONLY (Sam's step-4 directive: "the registry size ... may only go down").
     * A new exemption is a deliberate, reviewed decision — bump this ceiling in the
     * SAME edit as the new {@link #EXEMPTION_REGISTRY} entry, never as a side effect of
     * an unrelated change. {@link #exemptionRegistry_sizeStaysAtOrBelowCeiling} pins it.
     */
    private static final int EXEMPTION_REGISTRY_CEILING = 6;

    /**
     * The six sites this cycle's census confirmed have no typed jOOQ DSL form at all —
     * DDL, session/transaction-control syntax, a PgBouncer admin meta-command, a
     * Postgres system-catalog read jOOQ codegen does not model, and one EXPLAIN-pinned
     * probe constant executed by name. Every entry MUST have a live {@link
     * #SANCTIONED_STATEMENTS} registration for the same (file, method) pair AND a live
     * method declaration in that file on disk — both checked by {@link
     * #exemptionRegistry_everySiteStillExistsInSanctionedStatementsAndSource}.
     *
     * <p>Three entries this registry carried through the first version of this commit
     * (SchemaMigrator's {@code countChangelogRows}/{@code countChangelogRowsSince}/
     * {@code serverNow}) are GONE, not merely re-justified: their stated reason ("no
     * jOOQ typed-DSL form") was false — reading a table by name and reading the current
     * timestamp both have typed jOOQ forms ({@code DSL.table(DSL.name(...))}, {@code
     * DSL.currentTimestamp()}) — and the real, ARCHITECTURAL reason they were raw (no
     * DSLContext exists yet on the bare Liquibase-bootstrap Connection) does not
     * actually preclude typed DSL, since {@code DSL.using(conn, dialect)} wraps any
     * plain {@code Connection} regardless of when it was opened (critic finding,
     * nexus-zrcj7 step 4 review, T2 [24235]). All three are converted in SchemaMigrator.java.
     */
    private static final List<ExemptionEntry> EXEMPTION_REGISTRY = List.of(
        new ExemptionEntry("SchemaMigrator.java", "migrate",
            "SET TIME ZONE 'UTC' on the bare Liquibase-bootstrap JDBC Connection --  "
            + "PostgreSQL session syntax, no jOOQ typed-DSL form for a SET statement at "
            + "all (this is a genuine no-DSL-form gap, unlike the false reason SPOT-CHECKED "
            + "and REMOVED from the countChangelogRows/countChangelogRowsSince/serverNow "
            + "entries this same review round -- those had typed forms all along).",
            false),
        new ExemptionEntry("SchemaMigrator.java", "preflightChashConstraints",
            "ALTER TABLE ... {NO} FORCE ROW LEVEL SECURITY is DDL with no jOOQ typed-DSL "
            + "form; the constraint-validity probe reads pg_constraint, a Postgres system "
            + "catalog jOOQ codegen does not model.",
            false),
        new ExemptionEntry("CatalogRepository.java", "deferManifestChunkFk",
            "SET CONSTRAINTS ... DEFERRED is PostgreSQL transaction-control syntax with "
            + "no jOOQ typed-DSL form (re-verified against the jOOQ 3.21 manual via "
            + "Context7, nexus-zrcj7): the closest hits, alterConstraint().enforced()/"
            + ".notEnforced(), are a PERMANENT schema-level toggle, not this "
            + "transaction-scoped checking mode.",
            false),
        new ExemptionEntry("TenantScope.java", "vacuumAnalyze",
            "VACUUM is PostgreSQL maintenance syntax with no jOOQ typed-DSL form at all.",
            false),
        new ExemptionEntry("PoolerModeCheck.java", "fetchShowConfig",
            "SHOW CONFIG is a PgBouncer admin-console meta-command, not SQL against any "
            + "nexus-owned table/schema — no jOOQ DSL form exists.",
            false),
        new ExemptionEntry("ChashRepository.java", "lookup",
            "Executes the PUBLISHED PROBE_SQL constant BY NAME; a DSL rendering would "
            + "decouple the EXECUTED statement from the one ChashProbePlanShapeTest "
            + "EXPLAINs verbatim to pin index usage at 255k-row scale.",
            false)
    );

    @Test
    void exemptionRegistry_sizeStaysAtOrBelowCeiling() {
        assertThat(EXEMPTION_REGISTRY.size())
            .as("EXEMPTION_REGISTRY grew past its REDUCE-ONLY ceiling (%d) — a new raw-SQL "
                + "exemption is a deliberate, reviewed decision: bump "
                + "EXEMPTION_REGISTRY_CEILING in the SAME edit as the new entry, never as a "
                + "side effect of an unrelated change", EXEMPTION_REGISTRY_CEILING)
            .isLessThanOrEqualTo(EXEMPTION_REGISTRY_CEILING);
    }

    /**
     * Per-entry validation for {@link #EXEMPTION_REGISTRY}, extracted so a synthetic
     * fixture can exercise each of its three INDEPENDENT failure branches directly
     * (critic finding, T2 critique-24233: the real test had no falsification proof) —
     * see {@link #exemptionRegistry_missingSanctionedStatementsKey_isFlagged} / {@link
     * #exemptionRegistry_missingFile_isFlagged} / {@link
     * #exemptionRegistry_missingMethodRegion_isFlagged} / {@link
     * #exemptionRegistry_wellFormedEntry_producesNoViolations}. {@code sourceOrNull} is
     * the named file's content, or {@code null} when the file does not exist (the real
     * test passes {@code Files.readString(path)} only when {@code Files.exists(path)}).
     *
     * @return violation messages; empty means the entry is well-formed
     */
    static List<String> checkExemptionEntry(ExemptionEntry e,
            Map<String, Map<String, Map<String, Integer>>> sanctionedStatements,
            String sourceOrNull) {
        List<String> violations = new ArrayList<>();
        if (!sanctionedStatements.getOrDefault(e.file(), Map.of()).containsKey(e.method())) {
            violations.add("exemption registry entry " + e.file() + "#" + e.method()
                + " has no matching SANCTIONED_STATEMENTS registration -- either the "
                + "entry is stale (method converted/removed) or the SANCTIONED_STATEMENTS "
                + "registration was dropped without updating this registry");
            return violations;
        }
        if (sourceOrNull == null) {
            violations.add("exemption registry entry " + e.file() + "#" + e.method()
                + " names a file that no longer exists under dev.nexus.service.db");
            return violations;
        }
        List<int[]> regions = sanctionedRegions(blank(sourceOrNull), java.util.Set.of(e.method()));
        if (regions.isEmpty()) {
            violations.add("exemption registry entry " + e.file() + "#" + e.method()
                + ": method " + e.method() + " no longer has a live declaration in "
                + e.file() + " -- stale exemption, remove or update this registry entry");
        }
        return violations;
    }

    @Test
    void exemptionRegistry_everySiteStillExistsInSanctionedStatementsAndSource() throws IOException {
        List<String> violations = new ArrayList<>();
        for (ExemptionEntry e : EXEMPTION_REGISTRY) {
            Path path = Path.of("src", "main", "java", "dev", "nexus", "service", "db", e.file());
            String source = Files.exists(path) ? Files.readString(path) : null;
            violations.addAll(checkExemptionEntry(e, SANCTIONED_STATEMENTS, source));
        }
        assertThat(violations)
            .as("EXEMPTION_REGISTRY entries must each have a live SANCTIONED_STATEMENTS "
                + "registration and a live method declaration on disk -- see "
                + "checkExemptionEntry's own javadoc")
            .isEmpty();
    }

    private static final ExemptionEntry SYNTHETIC_EXEMPTION_ENTRY =
        new ExemptionEntry("Whatever.java", "someMethod", "synthetic fixture entry", false);

    @Test
    void exemptionRegistry_missingSanctionedStatementsKey_isFlagged() {
        List<String> hits = checkExemptionEntry(SYNTHETIC_EXEMPTION_ENTRY, Map.of(),
            "public final class Whatever { private void someMethod() { } }");
        assertThat(hits)
            .as("an entry with no matching SANCTIONED_STATEMENTS registration must fail loud")
            .anySatisfy(h -> assertThat(h).contains("no matching SANCTIONED_STATEMENTS"));
    }

    @Test
    void exemptionRegistry_missingFile_isFlagged() {
        Map<String, Map<String, Map<String, Integer>>> sanctioned =
            Map.of("Whatever.java", Map.of("someMethod", Map.of()));
        List<String> hits = checkExemptionEntry(SYNTHETIC_EXEMPTION_ENTRY, sanctioned, null);
        assertThat(hits)
            .as("an entry naming a file that no longer exists must fail loud")
            .anySatisfy(h -> assertThat(h).contains("no longer exists"));
    }

    @Test
    void exemptionRegistry_missingMethodRegion_isFlagged() {
        Map<String, Map<String, Map<String, Integer>>> sanctioned =
            Map.of("Whatever.java", Map.of("someMethod", Map.of()));
        // The file exists, but someMethod was renamed/removed -- no live declaration.
        String source = "public final class Whatever { private void otherMethod() { } }";
        List<String> hits = checkExemptionEntry(SYNTHETIC_EXEMPTION_ENTRY, sanctioned, source);
        assertThat(hits)
            .as("a SANCTIONED_STATEMENTS-registered method with no live declaration left "
                + "in the source must fail loud")
            .anySatisfy(h -> assertThat(h).contains("no longer has a live declaration"));
    }

    @Test
    void exemptionRegistry_wellFormedEntry_producesNoViolations() {
        Map<String, Map<String, Map<String, Integer>>> sanctioned =
            Map.of("Whatever.java", Map.of("someMethod", Map.of()));
        String source = "public final class Whatever { private void someMethod() { } }";
        assertThat(checkExemptionEntry(SYNTHETIC_EXEMPTION_ENTRY, sanctioned, source))
            .as("a registered method with a live declaration and a live "
                + "SANCTIONED_STATEMENTS entry is well-formed -- no violations")
            .isEmpty();
    }

    // ── RAW_SQL_ASSEMBLY_SENTINELS / RAW_SQL_WRAPPER_METHODS: DELETED (nexus-zrcj7
    //    step 4, deliverable 4). Both maps had been Map.of() since PgVectorRepository's
    //    rawVectorFetch wrapper (their sole former entry, "the bead's first NAMED
    //    motivating file") was deleted outright at step 1 -- searchWithTokens/
    //    hybridSearch retired onto generated jOOQ function tables (plain_search_<dim>/
    //    text_gated_search_<dim>, vectors-009/011). This bead's own step 4 mandate
    //    ("delete RAW_SQL_ASSEMBLY_SENTINELS if it is now empty") plus TaxonomyCentroid
    //    Repository.annQuery's conversion this same step (the only other raw-SQL-
    //    assembling method census ever found) means the corpus this pair of mechanisms
    //    polices is not merely empty today but has no live candidate anywhere in the
    //    tree. Deleted outright, per the dead-entry-avoidance discipline this class
    //    already established for a dead REGISTRATION (SANCTIONED_STATEMENTS' RekeyOps.
    //    java entry) -- here extended to the dead MECHANISM itself: scanAssemblySentinels/
    //    noUnreviewedRawSqlAssemblyChanges/assemblySentinel_unregisteredFileOrMethod_
    //    isUnaffected and scanWrapperCallSitesSentineled/noUnsentineledRawSqlWrapperCallSites
    //    are deleted along with their maps -- all of them existed only to tolerate or
    //    exercise machinery that, as of this commit, has nothing left to register.
    //    {@link #wrapperCallSites} and {@link
    //    #wrapperCallSites_realPgVectorRepository_zeroRawVectorFetchCallSitesRemain}
    //    below are KEPT: neither depends on either deleted map -- the test reads the
    //    real PgVectorRepository.java file directly and asserts zero rawVectorFetch call
    //    sites remain, a standalone regression guard for this bead's own success
    //    criterion, not scaffolding for the sentinel/wrapper-registry pair. If a FUTURE
    //    raw-SQL-assembling method funneled through a named non-execute/fetch wrapper
    //    ever resurfaces, this whole mechanism (whole-method-body sentinels keyed by
    //    file->method->{expected canonical bodies}, plus a companion wrapper-name
    //    registry whose every call site must fall inside an already-sentineled method)
    //    is recoverable from git history at the commit immediately preceding this one --
    //    write it back in rather than resurrecting it empty "just in case."

    /** Every CALL SITE start-offset of {@code name} in blanked source --
     * i.e. every {@code \bname(} match that is NOT that name's own
     * declaration. A declaration is distinguished from a call the same way
     * {@link #sanctionedRegions} does it: not preceded by {@code .} or an
     * identifier character, AND followed (after its parenthesized list is
     * matched) by a method body's opening {@code {}. Everything else
     * matching the name is an invocation -- including a receiver-qualified
     * call ({@code this.rawVectorFetch(...)}), which is still counted as a
     * call site, just not eligible to be misread as the declaration. */
    static List<Integer> wrapperCallSites(String blanked, String name) {
        List<Integer> sites = new ArrayList<>();
        Matcher m = Pattern.compile("\\b" + Pattern.quote(name) + "\\s*\\(").matcher(blanked);
        while (m.find()) {
            int start = m.start();
            int before = start - 1;
            int open = blanked.indexOf('(', start);
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
            int j = i;
            while (j < blanked.length() && Character.isWhitespace(blanked.charAt(j))) {
                j++;
            }
            boolean looksLikeDeclarationHead = before < 0
                || !(blanked.charAt(before) == '.' || Character.isJavaIdentifierPart(blanked.charAt(before)));
            boolean isDeclaration = looksLikeDeclarationHead
                && j < blanked.length() && blanked.charAt(j) == '{';
            if (!isDeclaration) {
                sites.add(start);
            }
        }
        return sites;
    }

    /** RENAMED and REPURPOSED (nexus-zrcj7, 2026-09-03; was
     * {@code wrapperCallSites_realPgVectorRepository_allFiveCallSitesAreSentineled},
     * which pinned "exactly 5 rawVectorFetch call sites, all inside
     * searchWithTokens/hybridSearch" as the non-overreach proof for the
     * nexus-8emxy extension). searchWithTokens and hybridSearch are now retired
     * onto generated jOOQ function tables (plain_search_<dim>/text_gated_search_
     * <dim>, vectors-009/011) -- rawVectorFetch itself is deleted, so the "5 call
     * sites, all sentineled" shape this test used to pin can never recur. This is
     * exactly the bead's own success criterion made a standing regression guard:
     * ZERO raw-SQL-assembling call sites on the search path, in the REAL tree.
     * KEPT after the RAW_SQL_ASSEMBLY_SENTINELS/RAW_SQL_WRAPPER_METHODS deletion
     * (step 4, deliverable 4): this test and {@link #wrapperCallSites} are the only
     * two survivors of that mechanism, and neither depends on either deleted map --
     * it reads the real file directly, unambiguous regardless of anything else in
     * the tree. */
    @Test
    void wrapperCallSites_realPgVectorRepository_zeroRawVectorFetchCallSitesRemain() throws IOException {
        Path path = Path.of("src", "main", "java", "dev", "nexus", "service", "vectors",
            "PgVectorRepository.java");
        assertThat(path).exists();
        String source = Files.readString(path);

        assertThat(wrapperCallSites(blank(source), "rawVectorFetch"))
            .as("nexus-zrcj7: rawVectorFetch (and every call to it) is deleted outright -- "
                + "searchWithTokens/hybridSearch now read through generated jOOQ function "
                + "tables (plain_search_<dim>/text_gated_search_<dim>, vectors-009/011). A "
                + "non-empty result here means a raw-SQL-assembling call was reintroduced; "
                + "see git history at the commit preceding the RAW_SQL_ASSEMBLY_SENTINELS/ "
                + "RAW_SQL_WRAPPER_METHODS deletion (nexus-zrcj7 step 4) for the mechanism "
                + "to write back before this can pass again")
            .isEmpty();
    }
}
