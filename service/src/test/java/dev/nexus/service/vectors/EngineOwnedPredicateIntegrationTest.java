// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Routines.reaperOwnsQuarantinedRow;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.57: the reaper's "engine-owned row" predicate has ONE definition,
 * {@code nexus.reaper_owns_quarantined_row(metadata)} (vectors-024-2), and every reader calls it.
 *
 * <p>Five things are pinned, each one a way the single definition could quietly stop being single, stop being total,
 * or stop being cheap:
 * <ul>
 *   <li>the truth table of the function itself (stamps agreeing, disagreeing, missing, another tagger): it is TOTAL,
 *       true or false and never NULL, so {@code NOT f(...)} and {@code f(...) IS NOT TRUE} read a row with no tag, which
 *       is most client rows, the same way, and a future {@code NOT} cannot silently stop client quarantine from
 *       expiring;</li>
 *   <li>its catalog properties: LANGUAGE sql, IMMUTABLE, PARALLEL SAFE, SECURITY INVOKER, executable by
 *       {@code nexus_svc}, which are what make the planner inline it;</li>
 *   <li>that it inlines: planned under {@code nexus_svc}, a statement that calls it shows no function in the plan and
 *       the same access path (nodes, index, index condition) as the open-coded predicate it replaced, for the SELECT,
 *       the DELETE, the negated shapes and the DISTINCT-origin read the call shapes use;</li>
 *   <li>that both expiry functions use it, by behaviour: seeded engine-tagged, client and stale-tagged rows are
 *       expired by exactly one of {@code reaper_expire_quarantine} and {@code gc_expire_quarantine}, whichever runs
 *       first;</li>
 *   <li>that nothing open-codes it again: no changelog outside the function's own body reads the
 *       {@code quarantined_by} or {@code reaper_quarantined_at} key by any operator, and no Java main source names
 *       either key. The Java half is behavioural too ({@code ReaperRepository#taggedOrigins} returns exactly the
 *       engine-owned origins).</li>
 * </ul>
 * Everything database-side is typed jOOQ, so this class adds nothing to the raw-SQL ratchet.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EngineOwnedPredicateIntegrationTest {

    private static final String SVC_ROLE = "svc_engine_owned";
    private static final String SVC_PASS = "svc_engine_owned_pass";
    private static final String TENANT = "engine-owned-t";
    private static final String QUAR = "quarantine-engine-owned-a";
    private static final String OTHER = "knowledge__engine-owned-b__minilm-l6-v2-384__v1";
    private static final String STAMP = "2026-09-01T00:00:00Z";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;

    @BeforeAll
    void seed() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, QUAR);
            PgContainerHelper.insertCollection(ctx, TENANT, OTHER);
            PgContainerHelper.insertCollection(ctx, TENANT, ORIGIN_P);
            for (int i = 0; i < 300; i++) {
                PgContainerHelper.insertCollection(ctx, TENANT, "knowledge__engine-owned-pad" + i + "__minilm-l6-v2-384__v1");
            }
            OffsetDateTime old = OffsetDateTime.now().minusDays(40);
            Vector zero = Vector.of(new float[384]);
            // Engine-owned: tag and both stamps agree.
            insertChunks(ctx, QUAR, "o", 3000, old, zero, tagged("origin-owned", STAMP, STAMP));
            // The client moved it again: the tag is stale (its reaper stamp is not the row's quarantined_at).
            insertChunks(ctx, QUAR, "s", 1500, old, zero, tagged("origin-stale", "2026-08-01T00:00:00Z", STAMP));
            // A client-moved row: no engine tag at all.
            insertChunks(ctx, QUAR, "u", 1500, old, zero,
                "{\"origin_collection\":\"origin-client\",\"quarantined_at\":\"" + STAMP + "\"}");
            insertChunks(ctx, OTHER, "x", 45000, old, zero, "{}");
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_COLLECTIONS);
        }
    }

    private static String tagged(String origin, String reaperStamp, String quarantinedAt) {
        return "{\"quarantined_by\":\"engine-reaper\",\"origin_collection\":\"" + origin
            + "\",\"reaper_quarantined_at\":\"" + reaperStamp + "\",\"quarantined_at\":\"" + quarantinedAt + "\"}";
    }

    private static Field<byte[]> chash(String prefix, Field<Integer> n) {
        return DSL.function("sha256", SQLDataType.BLOB,
            DSL.cast(DSL.concat(DSL.inline(prefix), DSL.cast(n, SQLDataType.VARCHAR)), SQLDataType.BLOB));
    }

    private static void insertChunks(DSLContext ctx, String collection, String prefix, int count,
                                     OffsetDateTime when, Vector vector, String metadataJson) {
        var series = DSL.generateSeries(1, count).as("g", "n");
        Field<Integer> n = series.field("n", Integer.class);
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                CHUNKS.EMBEDDING_384, CHUNKS.CREATED_AT, CHUNKS.LAST_WRITTEN_AT, CHUNKS.METADATA)
           .select(ctx.select(DSL.inline(TENANT), DSL.inline(collection), chash(prefix + collection, n), DSL.inline("x"),
                              DSL.val(vector, CHUNKS.EMBEDDING_384.getDataType()),
                              DSL.val(when), DSL.val(when), DSL.val(JSONB.valueOf(metadataJson))).from(series))
           .execute();
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ---- 1. the truth table ------------------------------------------------------------------------------------

    private Boolean owns(String metadataJson) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.select(reaperOwnsQuarantinedRow(JSONB.valueOf(metadataJson))).fetchOne(0, Boolean.class));
    }

    /** What gc_expire_quarantine asks of a row, spelled the way vectors-026-1 spells it: is it NOT the engine's. */
    private Boolean clientsRow(String metadataJson) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.select(DSL.field(reaperOwnsQuarantinedRow(JSONB.valueOf(metadataJson))).isDistinctFrom(true))
               .fetchOne(0, Boolean.class));
    }

    /** The spelling the function must make safe: a bare NOT, which is NULL on a NULL operand. */
    private Boolean notOwned(String metadataJson) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.select(DSL.not(reaperOwnsQuarantinedRow(JSONB.valueOf(metadataJson)))).fetchOne(0, Boolean.class));
    }

    @Test
    void truthTable_ownedOnlyWhenTagAndBothStampsAgree_andIsTotal() {
        assertThat(owns(tagged("o", STAMP, STAMP))).as("tag + equal stamps").isTrue();
        assertThat(clientsRow(tagged("o", STAMP, STAMP))).isFalse();
        assertThat(notOwned(tagged("o", STAMP, STAMP))).isFalse();

        // Every way to be not owned is a plain FALSE, never NULL (nexus-wbfpw.57 round 2): an untagged row, most
        // client rows, has no quarantined_by at all, and a NULL there made NOT f(...) NULL, which a WHERE reads as
        // "not the client's" and so never expired the row. Total, so every spelling of the negation agrees.
        List<String> notOwned = List.of(
            tagged("o", "2026-08-01T00:00:00Z", STAMP),
            "{\"quarantined_by\":\"engine-reaper\",\"quarantined_at\":\"" + STAMP + "\"}",
            "{\"quarantined_by\":\"engine-reaper\",\"reaper_quarantined_at\":\"" + STAMP + "\"}",
            "{\"quarantined_by\":\"engine-reaper\"}",
            "{\"quarantined_by\":\"client\",\"reaper_quarantined_at\":\"" + STAMP + "\",\"quarantined_at\":\""
                + STAMP + "\"}",
            "{\"reaper_quarantined_at\":\"" + STAMP + "\",\"quarantined_at\":\"" + STAMP + "\"}",
            "{\"origin_collection\":\"origin-client\",\"quarantined_at\":\"" + STAMP + "\"}",
            "{}");
        for (String m : notOwned) {
            assertThat(owns(m)).as("not owned, and not NULL: %s", m).isFalse();
            assertThat(notOwned(m)).as("NOT f(...) reads it as the client's: %s", m).isTrue();
            assertThat(clientsRow(m)).as("f(...) IS NOT TRUE reads it as the client's: %s", m).isTrue();
        }
    }

    // ---- 2. what makes it inlinable -----------------------------------------------------------------------------

    @Test
    void functionIsSqlImmutableParallelSafeSecurityInvokerAndExecutableByTheServiceRole() throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        var lang = DSL.table(DSL.name("pg_catalog", "pg_language"));
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
        try (Connection su = pg.createConnection("")) {
            var rec = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("l", "lanname"), String.class),
                        DSL.field(DSL.name("p", "provolatile"), String.class),
                        DSL.field(DSL.name("p", "prosecdef"), Boolean.class),
                        DSL.cast(DSL.field(DSL.name("p", "proconfig")), SQLDataType.VARCHAR),
                        DSL.field(DSL.name("p", "proretset"), Boolean.class),
                        DSL.field(DSL.name("p", "proparallel"), String.class),
                        DSL.function("has_function_privilege", Boolean.class, DSL.inline(SVC_ROLE),
                            DSL.field(DSL.name("p", "oid"), Object.class), DSL.inline("EXECUTE")))
                .from(proc.as("p"))
                .join(lang.as("l")).on(DSL.field(DSL.name("l", "oid")).eq(DSL.field(DSL.name("p", "prolang"))))
                .join(nsp.as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")
                    .and(DSL.field(DSL.name("p", "proname"), String.class).eq("reaper_owns_quarantined_row")))
                .fetch();
            assertThat(rec).as("exactly one function of that name (no overload to inline the wrong one)").hasSize(1);
            var r = rec.get(0);
            assertThat(r.value1()).as("LANGUAGE sql, the only language the planner inlines").isEqualTo("sql");
            assertThat(r.value2()).as("IMMUTABLE").isEqualTo("i");
            assertThat(r.value3()).as("SECURITY INVOKER: a SECURITY DEFINER function is never inlined").isFalse();
            assertThat(r.value4()).as("no SET clause: a function with proconfig is never inlined").isNull();
            assertThat(r.value5()).as("scalar, not set-returning").isFalse();
            assertThat(r.value6()).as("PARALLEL SAFE, as the DDL declares").isEqualTo("s");
            assertThat(r.value7()).as("nexus_svc can execute it").isTrue();
        }
    }

    /** A function's stored body with its SQL line comments removed: what the planner and the executor see. */
    private String bodyWithoutComments(String proname) throws Exception {
        var src = DSL.field(DSL.name("p", "prosrc"), String.class);
        try (Connection su = pg.createConnection("")) {
            List<String> bodies = DSL.using(su, SQLDialect.POSTGRES).select(src)
                .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                .where(DSL.field(DSL.name("p", "proname"), String.class).eq(proname))
                .fetch(src);
            assertThat(bodies).as("non-vacuity: exactly one %s", proname).hasSize(1);
            return SQL_LINE_COMMENT.matcher(bodies.get(0)).replaceAll("");
        }
    }

    /**
     * What a deploy census can read from the catalog: the function exists (the properties test above) and BOTH expiry
     * functions call it in code, comments removed. A comment naming the function proves nothing, so this strips
     * them; the behavioural probe below is the proof that the call does what it should.
     */
    @Test
    void bothExpiryFunctionsCallThePredicateInCode_notJustInAComment() throws Exception {
        assertThat(bodyWithoutComments("reaper_expire_quarantine")).contains("reaper_owns_quarantined_row(");
        assertThat(bodyWithoutComments("gc_expire_quarantine")).contains("reaper_owns_quarantined_row(");
    }

    /**
     * TRANSITION SHIM, not a contract. conexus's existing deploy census decides that vectors-026 landed by matching
     * the stored body of {@code nexus.gc_expire_quarantine} on {@code engine-reaper}. The literal moved into the
     * shared predicate, so the body keeps it in a comment until that census reads the catalog instead (it can ask
     * for {@code nexus.reaper_owns_quarantined_row} in pg_proc). This fails if a later edit drops the comment and
     * blinds the old census; it says nothing about behaviour, which the probe below pins.
     */
    @Test
    void gcExpireQuarantineBodyKeepsTheEngineReaperLiteral_asATransitionShimForTheOldCensus() throws Exception {
        var src = DSL.field(DSL.name("p", "prosrc"), String.class);
        try (Connection su = pg.createConnection("")) {
            List<String> bodies = DSL.using(su, SQLDialect.POSTGRES).select(src)
                .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                .where(DSL.field(DSL.name("p", "proname"), String.class).eq("gc_expire_quarantine"))
                .fetch(src);
            assertThat(bodies).as("non-vacuity: the function exists").hasSize(1);
            assertThat(bodies.get(0)).contains("engine-reaper");
        }
    }

    // ---- 3. the plan --------------------------------------------------------------------------------------------

    private String plan(java.util.function.Function<DSLContext, org.jooq.Query> query) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.explain(query.apply(ctx)).toString());
    }

    private static Field<String> key(String name) {
        return DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, name);
    }

    /** The predicate as it was open-coded in every site before this change. */
    private static Condition openCoded() {
        return key("quarantined_by").eq("engine-reaper").and(key("reaper_quarantined_at").eq(key("quarantined_at")));
    }

    private static Condition viaFunction() {
        return DSL.condition(reaperOwnsQuarantinedRow(CHUNKS.METADATA));
    }

    private static Condition scope() {
        return CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(QUAR));
    }

    private static final Pattern PLAN_NODE = Pattern.compile("^\\|\\s*(?:->\\s*)?(.*?)\\s+\\(cost=");
    private static final Pattern PLAN_COND = Pattern.compile("^\\|\\s*((?:Index|Recheck|Join) Cond:.*?)\\s*\\|?\\s*$");

    /**
     * The access path of a plan: every node (its type, the index it uses and the relation) and every index
     * condition, in plan order, with costs, row estimates and the {@code Filter:} text left out. The filter text
     * is where the predicate's own shape shows (the function's body, wrapped or not), so it is the one line that may
     * differ between the function form and the open-coded form; the path the rows are found by must not.
     */
    private static List<String> accessPath(String plan) {
        List<String> path = new ArrayList<>();
        for (String line : plan.split("\n")) {
            Matcher cond = PLAN_COND.matcher(line);
            if (cond.find()) {
                path.add(cond.group(1).replaceAll("\\s+", " ").trim());
                continue;
            }
            Matcher node = PLAN_NODE.matcher(line);
            if (node.find()) path.add(node.group(1).replaceAll("\\s+", " ").trim());
        }
        return path;
    }

    private void assertInlinedWithTheSameAccessPath(String what, String viaFn, String open) {
        System.out.println("\n=== " + what + " via function (EXPLAIN)\n" + viaFn);
        System.out.println("=== " + what + " open-coded (EXPLAIN)\n" + open);
        assertThat(viaFn).as("%s: inlined, not an opaque call:%n%s", what, viaFn)
            .doesNotContain("reaper_owns_quarantined_row").doesNotContain("Function Scan");
        assertThat(viaFn).as("%s: the predicate's keys are evaluated on the scanned row:%n%s", what, viaFn)
            .contains("quarantined_by").contains("reaper_quarantined_at");
        assertThat(viaFn).as("%s: candidates come from the (tenant, collection) primary-key range:%n%s", what, viaFn)
            .contains("chunks_pk");
        assertThat(viaFn).as("%s: no sequential scan of nexus.chunks:%n%s", what, viaFn)
            .doesNotContain("Seq Scan on chunks");
        List<String> path = accessPath(viaFn);
        assertThat(path).as("non-vacuity: the access path parser read the plan:%n%s", viaFn)
            .anyMatch(n -> n.startsWith("Index Scan using chunks_pk on chunks"))
            .anyMatch(n -> n.startsWith("Index Cond:"));
        assertThat(path).as("%s: the same access path as the open-coded predicate", what).isEqualTo(accessPath(open));
    }

    @Test
    void accessPathParser_readsNodesAndConditionsButNotFilters() {
        String plan = "|Limit  (cost=0.41..10028.49 rows=1 width=33)  |\n"
            + "|  ->  Index Scan using chunks_pk on chunks  (cost=0.41..9.49 rows=1 width=33)  |\n"
            + "|        Index Cond: ((tenant_id = 't'::text) AND (collection = 'c'::text))  |\n"
            + "|        Filter: ((metadata ->> 'quarantined_by'::text) = 'engine-reaper'::text)  |";
        assertThat(accessPath(plan)).containsExactly("Limit", "Index Scan using chunks_pk on chunks",
            "Index Cond: ((tenant_id = 't'::text) AND (collection = 'c'::text))");
    }

    @Test
    void select_inlines_andKeepsTheOpenCodedAccessPath() {
        assertInlinedWithTheSameAccessPath("SELECT",
            plan(ctx -> ctx.select(CHUNKS.CHASH).from(CHUNKS).where(scope().and(viaFunction())).orderBy(CHUNKS.CHASH)
                .limit(5000)),
            plan(ctx -> ctx.select(CHUNKS.CHASH).from(CHUNKS).where(scope().and(openCoded())).orderBy(CHUNKS.CHASH)
                .limit(5000)));
    }

    @Test
    void delete_inlines_andKeepsTheOpenCodedAccessPath() {
        String viaFn = plan(ctx -> ctx.deleteFrom(CHUNKS).where(scope().and(viaFunction())));
        assertThat(viaFn).contains("Delete on chunks");
        assertInlinedWithTheSameAccessPath("DELETE", viaFn,
            plan(ctx -> ctx.deleteFrom(CHUNKS).where(scope().and(openCoded()))));
    }

    @Test
    void negatedForm_inlines_andKeepsTheOpenCodedAccessPath() {
        // gc_expire_quarantine's shape: the client's rows are the ones the engine does not own.
        assertInlinedWithTheSameAccessPath("f(...) IS NOT TRUE",
            plan(ctx -> ctx.select(DSL.count()).from(CHUNKS)
                .where(scope().and(DSL.field(reaperOwnsQuarantinedRow(CHUNKS.METADATA)).isDistinctFrom(true)))),
            plan(ctx -> ctx.select(DSL.count()).from(CHUNKS)
                .where(scope().and(DSL.field(openCoded()).isDistinctFrom(true)))));
    }

    @Test
    void negatedWithNot_inlines_andKeepsTheOpenCodedAccessPath() {
        // The spelling a future caller reaches for first. Total function: safe, and it must cost the same.
        assertInlinedWithTheSameAccessPath("NOT f(...)",
            plan(ctx -> ctx.select(DSL.count()).from(CHUNKS)
                .where(scope().and(DSL.not(reaperOwnsQuarantinedRow(CHUNKS.METADATA))))),
            plan(ctx -> ctx.select(DSL.count()).from(CHUNKS)
                .where(scope().and(DSL.not(DSL.field(openCoded()))))));
    }

    @Test
    void distinctOriginRead_inlines() {
        Field<String> origin = key("origin_collection");
        String viaFn = plan(ctx -> ctx.selectDistinct(origin).from(CHUNKS)
            .where(scope().and(viaFunction()).and(origin.isNotNull())));
        assertThat(viaFn).doesNotContain("reaper_owns_quarantined_row").doesNotContain("Function Scan")
            .contains("chunks_pk").contains("quarantined_by");
    }

    // ---- 3b. the two expiry functions split the rows by the predicate, by behaviour -------------------------------

    private static final String ORIGIN_P = "knowledge__engine-owned-probe__minilm-l6-v2-384__v1";
    private static final String OLD = "2026-01-01T00:00:00Z";
    private static final String CUTOFF = "2026-02-01T00:00:00Z";

    /**
     * Six chunks in a fresh quarantine collection, all stamped before the cutoff and none named by a manifest row:
     * two the reaper owns, two a client moved (no tag), one whose tag is stale (a client moved it again), one tagged
     * with no reaper stamp at all (the NULL shape). Returns the collection.
     */
    private String seedProbe(String slug) throws Exception {
        String quarantine = "quarantine-engine-owned-probe-" + slug;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, quarantine);
            Vector zero = Vector.of(new float[384]);
            OffsetDateTime old = OffsetDateTime.now().minusDays(40);
            insertChunks(ctx, quarantine, "own", 2, old, zero, tagged(ORIGIN_P, OLD, OLD));
            insertChunks(ctx, quarantine, "cli", 2, old, zero,
                "{\"origin_collection\":\"" + ORIGIN_P + "\",\"quarantined_at\":\"" + OLD + "\"}");
            insertChunks(ctx, quarantine, "stl", 1, old, zero, tagged(ORIGIN_P, "2025-12-01T00:00:00Z", OLD));
            insertChunks(ctx, quarantine, "nul", 1, old, zero,
                "{\"quarantined_by\":\"engine-reaper\",\"origin_collection\":\"" + ORIGIN_P
                    + "\",\"quarantined_at\":\"" + OLD + "\"}");
        }
        return quarantine;
    }

    private long remaining(String quarantine) {
        return tenantScope.withTenant(TENANT, ctx -> (long) ctx.fetchCount(CHUNKS,
            CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(quarantine))));
    }

    private long remainingOwned(String quarantine) {
        return tenantScope.withTenant(TENANT, ctx -> (long) ctx.fetchCount(CHUNKS,
            CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(quarantine)).and(viaFunction())));
    }

    private PgVectorRepository vectorRepository() {
        Embedder zero = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        return new PgVectorRepository(tenantScope, zero, zero);
    }

    @Test
    void reaperExpiryTakesOnlyTheEngineOwnedRows_andGcExpiryTheRest() throws Exception {
        String q = seedProbe("reaper-first");
        assertThat(remaining(q)).isEqualTo(6);
        assertThat(remainingOwned(q)).as("only the two tagged-and-stamped rows are owned").isEqualTo(2);

        var expiry = new ReaperRepository(tenantScope).expire(TENANT, q, ORIGIN_P, CUTOFF, 1000, 60_000, 2_000);
        assertThat(expiry.expired()).as("reaper_expire_quarantine: the two owned rows, no others").isEqualTo(2);
        assertThat(remaining(q)).isEqualTo(4);
        assertThat(remainingOwned(q)).isZero();

        var gc = vectorRepository().expireQuarantine(TENANT, q, ORIGIN_P, CUTOFF, 1.0, 1_000_000, true);
        assertThat(gc.expired()).as("gc_expire_quarantine: the client's two, the stale tag and the missing stamp")
            .isEqualTo(4);
        assertThat(remaining(q)).isZero();
    }

    @Test
    void gcExpirySkipsTheEngineOwnedRows_andReaperExpiryTakesThemAfter() throws Exception {
        String q = seedProbe("gc-first");
        assertThat(remaining(q)).isEqualTo(6);

        var gc = vectorRepository().expireQuarantine(TENANT, q, ORIGIN_P, CUTOFF, 1.0, 1_000_000, true);
        assertThat(gc.expired()).as("gc_expire_quarantine skips the owned rows: the other four go").isEqualTo(4);
        assertThat(remaining(q)).as("the two engine-owned rows stay").isEqualTo(2);
        assertThat(remainingOwned(q)).isEqualTo(2);

        var expiry = new ReaperRepository(tenantScope).expire(TENANT, q, ORIGIN_P, CUTOFF, 1000, 60_000, 2_000);
        assertThat(expiry.expired()).as("reaper_expire_quarantine takes exactly what gc left").isEqualTo(2);
        assertThat(remaining(q)).isZero();
    }

    // ---- 4. nothing open-codes it again --------------------------------------------------------------------------

    @Test
    void taggedOrigins_returnsExactlyTheEngineOwnedOrigins() {
        var repo = new ReaperRepository(tenantScope);
        assertThat(repo.taggedOrigins(TENANT, QUAR, 60_000))
            .as("the stale-tag origin and the client-moved origin are not the engine's")
            .containsExactly("origin-owned");
    }

    private static final Pattern XML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);
    private static final Pattern SQL_BLOCK = Pattern.compile("<sql[^>]*>(.*?)</sql>", Pattern.DOTALL);
    private static final Pattern ROLLBACK = Pattern.compile("<rollback>.*?</rollback>", Pattern.DOTALL);
    private static final Pattern SQL_LINE_COMMENT = Pattern.compile("--[^\\n]*");

    /**
     * The tag keys as bare words, so every way to read them is caught: {@code ->>}, {@code ->}, {@code #>>},
     * {@code #>}, {@code @>}, {@code ?}, jsonb_extract_path_text, jsonb_path_query. Any operator, any quoting.
     */
    private static final Pattern TAG_KEY = Pattern.compile("\\b(quarantined_by|reaper_quarantined_at)\\b");

    /** The one write of the tag (vectors-024-1's jsonb_build_object pairs), the exact pairs and no others. */
    private static final Pattern TAG_WRITE = Pattern.compile(
        "'quarantined_by'\\s*,\\s*'engine-reaper'|'reaper_quarantined_at'\\s*,\\s*p_quarantined_at");

    /** The removal of the tag by the restore functions (vectors-025): {@code - 'key'}. Not {@code ->> 'key'}. */
    private static final Pattern TAG_REMOVAL = Pattern.compile("-\\s*'(?:quarantined_by|reaper_quarantined_at)'");

    /** A {@code COMMENT ON FUNCTION ... IS '...'} statement is documentation, and names the keys in prose. */
    private static final Pattern COMMENT_ON_FUNCTION = Pattern.compile(
        "(?is)COMMENT\\s+ON\\s+FUNCTION\\b.*?\\bIS\\s+'(?:[^']|'')*'\\s*;");

    /** The body of the one definition, so a read inside it is not an offender. */
    private static final Pattern DEFINITION = Pattern.compile(
        "(?is)CREATE\\s+(?:OR\\s+REPLACE\\s+)?FUNCTION\\s+nexus\\.reaper_owns_quarantined_row\\b.*?\\$\\$.*?\\$\\$");

    private static int count(Pattern p, String text) {
        Matcher m = p.matcher(text);
        int n = 0;
        while (m.find()) n++;
        return n;
    }

    /** Mentions of a tag key in {@code sql} that are neither the one write nor a removal: reads, by whatever operator. */
    private static int readsOfTheTagKeys(String sql) {
        return count(TAG_KEY, TAG_REMOVAL.matcher(TAG_WRITE.matcher(sql).replaceAll("")).replaceAll(""));
    }

    @Test
    void noChangesetOutsideTheDefinitionReadsTheTagKeys() throws IOException {
        List<String> offenders = new ArrayList<>();
        int definitions = 0;
        int callSites = 0;
        int writes = 0;
        int removals = 0;
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "resources", "db", "changelog"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".xml")).sorted().toList()) {
                String forward = ROLLBACK.matcher(XML_COMMENT.matcher(Files.readString(p)).replaceAll("")).replaceAll("");
                Matcher blocks = SQL_BLOCK.matcher(forward);
                while (blocks.find()) {
                    String sql = COMMENT_ON_FUNCTION.matcher(SQL_LINE_COMMENT.matcher(blocks.group(1)).replaceAll(""))
                        .replaceAll("");
                    if (DEFINITION.matcher(sql).find()) definitions++;
                    String outside = DEFINITION.matcher(sql).replaceAll("");
                    writes += count(TAG_WRITE, outside);
                    removals += count(TAG_REMOVAL, outside);
                    if (readsOfTheTagKeys(outside) > 0) {
                        offenders.add(p.getFileName() + ": names quarantined_by / reaper_quarantined_at outside the"
                            + " tag write and the restore removal; call nexus.reaper_owns_quarantined_row");
                    }
                    callSites += count(Pattern.compile("reaper_owns_quarantined_row\\s*\\("), outside);
                }
            }
        }
        assertThat(definitions).as("non-vacuity: the definition was found, exactly once").isEqualTo(1);
        assertThat(callSites).as("non-vacuity: reaper_expire_quarantine (3) and gc_expire_quarantine (2) call it")
            .isGreaterThanOrEqualTo(5);
        assertThat(writes).as("non-vacuity: vectors-024-1 writes both keys, and the scan saw it").isEqualTo(2);
        assertThat(removals).as("non-vacuity: vectors-025 removes both keys, and the scan saw it").isGreaterThanOrEqualTo(2);
        assertThat(offenders).isEmpty();
    }

    /** The scan is only as good as its pattern: prove it flags every way to read the keys and spares the write and removal. */
    @Test
    void theTagKeyScan_flagsEveryReadOperator_andSparesTheWriteAndTheRemoval() {
        List<String> reads = List.of(
            "c.metadata->>'quarantined_by' = 'engine-reaper'",
            "c.metadata ->> 'reaper_quarantined_at'",
            "c.metadata -> 'quarantined_by'",
            "c.metadata #>> '{quarantined_by}'",
            "c.metadata #> '{reaper_quarantined_at}'",
            "c.metadata @> '{\"quarantined_by\":\"engine-reaper\"}'",
            "c.metadata ? 'quarantined_by'",
            "jsonb_extract_path_text(c.metadata, 'quarantined_by')",
            "jsonb_extract_path_text(c.metadata, 'reaper_quarantined_at', 'x')",
            "jsonb_path_exists(c.metadata, '$.quarantined_by')");
        for (String r : reads) assertThat(readsOfTheTagKeys(r)).as("flagged: %s", r).isPositive();
        List<String> spared = List.of(
            "jsonb_build_object('quarantined_by', 'engine-reaper', 'reaper_quarantined_at', p_quarantined_at)",
            "c.metadata - 'quarantined_at' - 'origin_collection' - 'quarantined_by' - 'reaper_quarantined_at'",
            "nexus.reaper_owns_quarantined_row(c.metadata) IS NOT TRUE");
        for (String sp : spared) assertThat(readsOfTheTagKeys(sp)).as("not a read: %s", sp).isZero();
    }

    @Test
    void noJavaMainSourceNamesTheTagKeys() throws IOException {
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        Pattern block = Pattern.compile("/\\*.*?\\*/", Pattern.DOTALL);
        Pattern line = Pattern.compile("//[^\\n]*");
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "java"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                String code = line.matcher(block.matcher(Files.readString(p)).replaceAll("")).replaceAll("");
                scanned++;
                if (TAG_KEY.matcher(code).find()) {
                    offenders.add(p + ": names the quarantined_by / reaper_quarantined_at key outside a comment"
                        + " (a string, a text block, a jOOQ path); call Routines.reaperOwnsQuarantinedRow");
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the scan walked the main sources").isGreaterThan(50);
        assertThat(offenders).isEmpty();
        // The scan's own blind spot would be a pattern that matches nothing; prove it matches what it exists to catch.
        assertThat(TAG_KEY.matcher("DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, \"quarantined_by\")").find()).isTrue();
        assertThat(TAG_KEY.matcher("  AND c.metadata #>> '{reaper_quarantined_at}' = x").find()).isTrue();
        assertThat(TAG_KEY.matcher("c.metadata @> '{\"quarantined_by\":\"engine-reaper\"}'").find()).isTrue();
    }
}
