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
 * <p><b>WIDENED SCOPE, SAME-FILE EVOLUTION (nexus-8emxy comment, 2026-08-09;
 * closed here):</b> the fix above is itself NAME-KEYED, exactly like {@link
 * #SANCTIONED_STATEMENTS} — a DIFFERENTLY-NAMED future method in {@code
 * PgVectorRepository.java} ITSELF that assembles SQL dynamically and calls
 * the same private {@code rawVectorFetch(ctx, sql, binds)} wrapper is
 * equally invisible to {@link #RAW_SQL_ASSEMBLY_SENTINELS}, which only ever
 * inspects the bodies of methods that are already keys in its map — a
 * caller that has not yet been named produces zero loop iterations, not a
 * failing one. {@link #RAW_SQL_WRAPPER_METHODS} / {@link
 * #scanWrapperCallSitesSentineled} INVERTS this failure mode: instead of
 * trusting a fixed allowlist of caller names, it finds every actual CALL
 * SITE of the registered wrapper method(s) by scanning the file for
 * invocations of the wrapper's own name, and requires each call site to
 * fall inside a method that already has a {@link #RAW_SQL_ASSEMBLY_SENTINELS}
 * registration. A brand-new method added anywhere in the file that calls
 * {@code rawVectorFetch} therefore FAILS the gate on sight — silent-miss
 * becomes loud-add. See {@link #wrapperCallSites_newUnsentineledCaller_isFlagged}
 * for the falsification proof and {@link
 * #wrapperCallSites_realPgVectorRepository_zeroRawVectorFetchCallSitesRemain}
 * for the non-overreach proof (the extension does not simply ban the
 * wrapper — nexus-zrcj7, 2026-09-03, retired the wrapper itself: the real tree
 * now carries ZERO rawVectorFetch call sites, which that test pins directly).
 * This closes the same-file half of nexus-8emxy's P2 for
 * {@code PgVectorRepository.java} specifically; a hypothetical brand-new
 * private raw-SQL wrapper method appearing in some OTHER file still needs a
 * conscious {@link #RAW_SQL_WRAPPER_METHODS} registration, AND that
 * registration is NOT mechanically forced by anything else in this class
 * (critic finding, nexus-8emxy review, 2026-08-21): {@link
 * #SANCTIONED_STATEMENTS} and {@link #RAW_SQL_WRAPPER_METHODS} are
 * INDEPENDENT maps checked by INDEPENDENT tests. Registering the new
 * wrapper's own body in {@code SANCTIONED_STATEMENTS} (forced by {@link
 * #RAW_EXECUTE} flagging its literal {@code .fetch(}/{@code .execute(}
 * call) satisfies only that one test; {@code RAW_SQL_WRAPPER_METHODS}
 * itself is never touched by that, and {@link #noUnsentineledRawSqlWrapperCallSites}
 * would still run zero checks against the new wrapper's callers unless a
 * human separately adds the entry. The honest claim is narrower: a new
 * wrapper elsewhere is *likely* to be caught by a reviewer's attention
 * (the maintainer is already editing this file for the first registration,
 * a natural point to notice the second), not that it is *structurally*
 * guaranteed the way same-file caller coverage now is.
 *
 * <p><b>KNOWN RESIDUAL — call shapes this gate does not scan at all</b>
 * (nexus-8emxy critique, 2026-08-21). Two call shapes carry raw or
 * caller-influenced SQL text and are invisible to every check in this
 * class — {@link #RAW_EXECUTE}, {@link #RAW_SQL_ASSEMBLY_SENTINELS}, and
 * {@link #RAW_SQL_WRAPPER_METHODS} alike — because none of them anchor on
 * these shapes at all, the same "not excused, structurally no anchor"
 * situation the class javadoc above describes for the wrapper problem:
 * <ul>
 *   <li><b>jOOQ's plain-SQL-template overloads</b> — {@code DSL.field("...",
 *       Class, binds...)}, {@code DSL.condition("...", binds...)}, {@code
 *       DSL.table("...", binds...)}, which parse a Java string as raw SQL
 *       text with {@code {0}}/{@code {1}} bind placeholders. Verified by a
 *       direct count against the live tree (2026-08-21): 67 call sites take
 *       an immediate string-literal first argument to one of these three
 *       methods, across 5 files ({@code CatalogRepository.java}, {@code
 *       LadderRepository.java}, {@code PipelineRepository.java}, {@code
 *       PgVectorRepository.java}, {@code RemapRepository.java}). Of those,
 *       60 are fully static literal identifier references with no {@code
 *       {n}} placeholder at all (e.g. {@code DSL.field("EXCLUDED.name",
 *       String.class)} — the Postgres {@code ON CONFLICT} pseudo-table,
 *       hardcoded, never runtime-varying); the remaining ~7 carry a genuine
 *       bind-placeholder template, 5 of them real code (2 in {@code
 *       PgVectorRepository.metadataCondition}, 3 in {@code
 *       CatalogRepository}) and 2 merely comment mentions. Separately,
 *       {@code StagingHandler.java} and {@code ChashCensus.java} use the
 *       differently-shaped, properly-quoting {@code DSL.field(DSL.name(...),
 *       Class)} / {@code DSL.table(DSL.name(...))} idiom to build genuinely
 *       DYNAMIC (loop-driven) identifiers — jOOQ's own safe quoted-identifier
 *       construction, not a raw-text template, but still a call shape this
 *       gate never inspects. Checked the one instance that concatenates
 *       non-constant text into a template rather than binding it —
 *       {@code metadataCondition}'s {@code cmp} comparator, spliced
 *       directly into {@code DSL.condition("metadata->>{0} " + cmp + "
 *       {1}", ...)} — and confirmed it safe today: {@code cmp} is drawn
 *       from a closed 4-branch {@code switch} over {@code $gte}/{@code
 *       $lte}/{@code $gt}/{@code $lt}, never caller-supplied text
 *       directly. No live defect exists today. Left unscanned deliberately
 *       rather than chased: scanning this shape well enough to be
 *       trustworthy (distinguishing a bind placeholder from a spliced
 *       comparator, distinguishing a closed-switch value from genuinely
 *       caller-derived text) is a materially different, larger mechanism
 *       than the wrapper-call-site check above, and no known instance
 *       needs it today — a bead for a mechanism verified safe with no
 *       known instance is backlog padding, not a fix.</li>
 *   <li><b>Method-reference call shapes.</b> {@link #wrapperCallSites}
 *       matches direct invocations ({@code rawVectorFetch(...)}) via a
 *       {@code \bname\s*\(} pattern; a method reference to the same wrapper
 *       ({@code this::rawVectorFetch} or {@code
 *       PgVectorRepository::rawVectorFetch} passed where a functional
 *       interface is expected) has no {@code (} immediately following the
 *       name and does not match at all. No such reference exists in the
 *       codebase today (the 5 real call sites are all direct invocations);
 *       if one is ever introduced, it would be exactly as invisible to
 *       {@link #scanWrapperCallSitesSentineled} as the original bug this
 *       bead closes.</li>
 * </ul>
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
        // PgVectorRepository.java's "rawVectorFetch" entry: REMOVED (nexus-zrcj7,
        // 2026-09-03). rawVectorFetch (the single execution chokepoint for
        // searchWithTokens()/hybridSearch()'s raw SQL) is deleted outright --
        // both callers now go through generated jOOQ function tables
        // (plain_search_<dim>/text_gated_search_<dim>, vectors-009/010) like every
        // other combined-query shape. Per the dead-entry-avoidance discipline this
        // class already established (RekeyOps.java's entry, below), a registration
        // for a deleted method is removed outright, not kept as a no-op.
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
                ".execute(\"ALTER TABLE nexus.\" + table + \" FORCE ROW LEVEL SECURITY\")", 1),
            // SANCTIONED RAW (nexus-rph82): SET TIME ZONE is PostgreSQL session
            // syntax with no jOOQ typed-DSL form. Pins the migration connection's
            // session zone to UTC so databasechangelog.dateexecuted (stamped via
            // the server's now() rendered in the SESSION zone) is not JVM-local
            // against a GMT database — pgjdbc negotiates the session zone from
            // the JVM default at CONNECT time, so a pool opened before
            // pinJvmTimeZoneToUtc() still carries the old zone; this pins the
            // session directly. One statement, executed once per migrate() call.
            "migrate", Map.of(
                ".execute(\"SET TIME ZONE 'UTC'\")", 1),
            // SANCTIONED RAW (nexus-x0s52): databasechangelog is LIQUIBASE'S
            // OWN bookkeeping table — outside jOOQ codegen (which models the
            // nexus/staging application schemas), and deliberately referenced
            // UNQUALIFIED on the SAME connection Liquibase itself uses, so the
            // reads resolve to exactly the changelog Liquibase reads and
            // writes (a DSL rendering over the pooled DSLContext would be a
            // different session with a different search path). to_regclass()
            // probes first-boot absence; both statements execute once per
            // migrate() call, before and after the update.
            "countChangelogRows", Map.of(
                ".executeQuery(\"SELECT to_regclass('databasechangelog')\")", 1,
                ".executeQuery(\"SELECT count(*) FROM databasechangelog\")", 1),
            // SANCTIONED RAW (nexus-x0s52): now()::timestamp read on the
            // migration connection itself — the same clock and session zone
            // Liquibase stamps dateexecuted from (see the SET TIME ZONE entry
            // above); no table involved, no jOOQ typed form for a bare
            // server-clock read on a specific connection.
            "serverNow", Map.of(
                ".executeQuery(\"SELECT now()::timestamp\")", 1))),
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
        Map.of();

    // RETIRED (nexus-zrcj7, 2026-09-03): this map used to carry a PgVectorRepository.java
    // entry naming searchWithTokens/hybridSearch, the two StringBuilder-assembled-raw-SQL
    // methods this whole mechanism was built for (this class's own javadoc: "the
    // bead's first NAMED motivating file"). Both methods are retired onto generated
    // jOOQ function tables (plain_search_<dim>/text_gated_search_<dim>, vectors-
    // 009/010) -- zero raw-SQL-assembling methods remain on the search path, the
    // corpus this mechanism polices is now permanently empty by construction, and
    // RAW_SQL_ASSEMBLY_SENTINELS above is Map.of() rather than carrying a stale or
    // vacuous entry, matching the dead-entry-avoidance discipline this class already
    // established elsewhere (SANCTIONED_STATEMENTS' RekeyOps.java removal, above;
    // TombstoneFilterGateTest's floor_stagingPromoteOpsRawSqlSites retirement, same
    // reasoning). {@link #noUnreviewedRawSqlAssemblyChanges} keeps running (walks
    // src/main for every registered file -- there are none today, so it always
    // finds zero violations) as a live, non-dormant guard should a FUTURE raw-SQL-
    // assembling method ever need this registration again; only the DATA that named
    // PgVectorRepository.java is gone, not the mechanism.
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

    // assemblySentinel_bodyChange_isFlagged: RETIRED (nexus-zrcj7, 2026-09-03). It
    // proved a body that matches none of RAW_SQL_ASSEMBLY_SENTINELS's registered
    // snapshots fails loud "even though a method of that name IS registered" -- a
    // precondition (SOME method registered for PgVectorRepository.java) that no
    // longer holds now that the map is Map.of() (searchWithTokens/hybridSearch
    // retired onto generated jOOQ function tables, vectors-009/010: see the
    // RAW_SQL_ASSEMBLY_SENTINELS retirement comment above). Same disposition as
    // TombstoneFilterGateTest's floor_stagingPromoteOpsRawSqlSites: a proof whose
    // fixture went permanently empty is removed, not adjusted to assert nothing.
    // assemblySentinel_unregisteredFileOrMethod_isUnaffected (below) still covers
    // the scan-logic-returns-empty-for-an-unregistered-file/method shape this test
    // used to distinguish from -- PgVectorRepository.java now behaves exactly like
    // that case, so no coverage is lost, only the SPECIFIC "registered but wrong
    // body" branch, which nothing in the real tree can exercise any more.

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

    // ── nexus-8emxy (comment, 2026-08-09): SAME-FILE evolution closure --
    //    invert the failure mode. RAW_SQL_ASSEMBLY_SENTINELS is name-keyed
    //    (it only inspects methods already registered as keys); this finds
    //    every actual CALL SITE of the wrapper by name and requires its
    //    enclosing method to already be registered, so an unnamed future
    //    caller fails loud instead of producing zero signal ──

    /** Private raw-SQL wrapper methods (by file) whose CALL SITES must each
     * fall inside a method registered in {@link #RAW_SQL_ASSEMBLY_SENTINELS}
     * for that same file. Unlike {@link #SANCTIONED_STATEMENTS} / {@link
     * #RAW_SQL_ASSEMBLY_SENTINELS} (which enumerate TRUSTED caller method
     * names — a list a new caller can simply not appear on), this registry
     * enumerates WRAPPER method names — the dangerous thing whose every
     * invocation must be found and checked, regardless of what calls it or
     * what that caller happens to be named. Adding a private raw-SQL
     * funnel method elsewhere already requires registering its own body in
     * {@link #SANCTIONED_STATEMENTS} (its {@code .fetch(}/{@code .execute(}
     * call is a literal {@link #RAW_EXECUTE} match); this map is the
     * companion registration for treating it as a wrapper whose callers
     * must all be accounted for. */
    // Empty (nexus-zrcj7, 2026-09-03): the sole entry, PgVectorRepository.java's
    // "rawVectorFetch", is REMOVED -- that method is deleted outright (searchWithTokens/
    // hybridSearch retired onto generated jOOQ function tables, vectors-009/010), so
    // there is no wrapper method left anywhere in this class's corpus to register. Kept
    // as a live (not deleted) mechanism, dormant by construction until a future raw-
    // SQL-assembling wrapper needs it again -- same "dormant, not enforced, noted per
    // spec" disposition TombstoneFilterGateTest gives its own catalog_links case.
    private static final Map<String, java.util.Set<String>> RAW_SQL_WRAPPER_METHODS =
        Map.of();

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

    /** Per-file scan (nexus-8emxy, same-file closure): every call site of a
     * {@link #RAW_SQL_WRAPPER_METHODS}-registered wrapper must fall inside
     * a method that already has a {@link #RAW_SQL_ASSEMBLY_SENTINELS}
     * registration for the SAME file. The check does not need to know the
     * enclosing method's NAME to be correct -- it only needs to know
     * whether the call site sits inside the UNION of regions belonging to
     * already-registered methods; a call site outside that union is
     * uncovered regardless of what its enclosing method is called. */
    static List<String> scanWrapperCallSitesSentineled(String fileName, String rawSource) {
        java.util.Set<String> wrapperNames = RAW_SQL_WRAPPER_METHODS.getOrDefault(fileName, java.util.Set.of());
        if (wrapperNames.isEmpty()) {
            return List.of();
        }
        String blanked = blank(rawSource);
        java.util.Set<String> sentineledNames =
            RAW_SQL_ASSEMBLY_SENTINELS.getOrDefault(fileName, Map.of()).keySet();
        List<int[]> sentineledRegions = sanctionedRegions(blanked, sentineledNames);

        List<String> violations = new ArrayList<>();
        for (String wrapperName : wrapperNames) {
            for (int at : wrapperCallSites(blanked, wrapperName)) {
                boolean covered = false;
                for (int[] r : sentineledRegions) {
                    if (r[0] <= at && at < r[1]) {
                        covered = true;
                        break;
                    }
                }
                if (!covered) {
                    int line = 1 + (int) blanked.substring(0, at).chars()
                        .filter(c -> c == '\n').count();
                    violations.add(fileName + ":" + line + "  UNSENTINELED WRAPPER CALL SITE: a call "
                        + "to " + wrapperName + "(...) was found in a method with no "
                        + "RAW_SQL_ASSEMBLY_SENTINELS registration for " + fileName + " -- add the "
                        + "enclosing method's whole-body canonical text to RAW_SQL_ASSEMBLY_SENTINELS "
                        + "(see its own javadoc) before this raw-SQL-assembling caller can be assumed "
                        + "reviewed");
                }
            }
        }
        return violations;
    }

    @Test
    void noUnsentineledRawSqlWrapperCallSites() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    violations.addAll(scanWrapperCallSitesSentineled(
                        p.getFileName().toString(), Files.readString(p)));
                } catch (IOException e) {
                    throw new RuntimeException(e);
                }
            });
        }

        assertThat(violations)
            .as("a call to a RAW_SQL_WRAPPER_METHODS-registered wrapper (e.g. "
                + "PgVectorRepository.rawVectorFetch) was found outside every method already "
                + "registered in RAW_SQL_ASSEMBLY_SENTINELS -- either the new caller genuinely "
                + "needs review (add its whole-body snapshot to RAW_SQL_ASSEMBLY_SENTINELS) or "
                + "it should be refactored to route through an already-sentineled method")
            .isEmpty();
    }

    // wrapperCallSites_newUnsentineledCaller_isFlagged: RETIRED (nexus-zrcj7,
    // 2026-09-03). It proved a brand-new, differently-named method calling
    // rawVectorFetch fails loud even though the wrapper's OWN existence is what
    // RAW_SQL_WRAPPER_METHODS registers ("PgVectorRepository.java" ->
    // "rawVectorFetch") -- a precondition that no longer holds now that
    // RAW_SQL_WRAPPER_METHODS is Map.of() (rawVectorFetch itself deleted;
    // searchWithTokens/hybridSearch retired onto generated jOOQ function tables,
    // vectors-009/010). scanWrapperCallSitesSentineled short-circuits to
    // List.of() for any file with no registered wrapper name, so this synthetic
    // source can no longer produce the finding it asserted. The falsification
    // this test demonstrated (an unnamed future caller must produce a FAILING
    // signal, not zero signal) is preserved for a FUTURE raw-SQL wrapper: the
    // moment RAW_SQL_WRAPPER_METHODS names one again, this same shape of proof
    // is the one to write back in.

    /** RENAMED and REPURPOSED (nexus-zrcj7, 2026-09-03; was
     * {@code wrapperCallSites_realPgVectorRepository_allFiveCallSitesAreSentineled},
     * which pinned "exactly 5 rawVectorFetch call sites, all inside
     * searchWithTokens/hybridSearch" as the non-overreach proof for the
     * nexus-8emxy extension). searchWithTokens and hybridSearch are now retired
     * onto generated jOOQ function tables (plain_search_<dim>/text_gated_search_
     * <dim>, vectors-009/010) -- rawVectorFetch itself is deleted, so the "5 call
     * sites, all sentineled" shape this test used to pin can never recur. This is
     * exactly the bead's own success criterion made a standing regression guard:
     * ZERO raw-SQL-assembling call sites on the search path, in the REAL tree, not
     * just in RAW_SQL_ASSEMBLY_SENTINELS's (now-empty) data. Reads the real file
     * directly (not the tree-walking {@link #noUnsentineledRawSqlWrapperCallSites}
     * test) so this assertion is unambiguous even if some other file elsewhere in
     * the walk were to fail. */
    @Test
    void wrapperCallSites_realPgVectorRepository_zeroRawVectorFetchCallSitesRemain() throws IOException {
        Path path = Path.of("src", "main", "java", "dev", "nexus", "service", "vectors",
            "PgVectorRepository.java");
        assertThat(path).exists();
        String source = Files.readString(path);

        assertThat(wrapperCallSites(blank(source), "rawVectorFetch"))
            .as("nexus-zrcj7: rawVectorFetch (and every call to it) is deleted outright -- "
                + "searchWithTokens/hybridSearch now read through generated jOOQ function "
                + "tables (plain_search_<dim>/text_gated_search_<dim>, vectors-009/010). A "
                + "non-empty result here means a raw-SQL-assembling call was reintroduced; "
                + "register the wrapper in RAW_SQL_WRAPPER_METHODS and its enclosing method's "
                + "whole-body canonical text in RAW_SQL_ASSEMBLY_SENTINELS before this can "
                + "pass again")
            .isEmpty();
    }
}
