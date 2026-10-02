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
 * <p>Four things are pinned, each one a way the single definition could quietly stop being single or stop being cheap:
 * <ul>
 *   <li>the truth table of the function itself (stamps agreeing, disagreeing, missing, another tagger), and that it
 *       NULL semantics, which the negated form {@code f(...) IS NOT TRUE} (what gc_expire_quarantine writes) depends on;</li>
 *   <li>its catalog properties: LANGUAGE sql, IMMUTABLE, SECURITY INVOKER, executable by {@code nexus_svc}, which are
 *       what make the planner inline it;</li>
 *   <li>that it inlines: planned under {@code nexus_svc}, a statement that calls it shows no function in the plan and
 *       the same plan, node for node, as the open-coded predicate it replaced, for the SELECT, the DELETE and the
 *       DISTINCT-origin read the three call shapes use;</li>
 *   <li>that nothing open-codes it again: no changelog outside the function's own body, and no Java main source, reads
 *       the {@code quarantined_by} or {@code reaper_quarantined_at} key. The Java half is behavioural too
 *       ({@code ReaperRepository#taggedOrigins} returns exactly the engine-owned origins).</li>
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

    /** What gc_expire_quarantine asks of a row: is it NOT the engine's. */
    private Boolean clientsRow(String metadataJson) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.select(DSL.field(reaperOwnsQuarantinedRow(JSONB.valueOf(metadataJson))).isDistinctFrom(true))
               .fetchOne(0, Boolean.class));
    }

    @Test
    void truthTable_ownedOnlyWhenTagAndBothStampsAgree() {
        assertThat(owns(tagged("o", STAMP, STAMP))).as("tag + equal stamps").isTrue();
        assertThat(clientsRow(tagged("o", STAMP, STAMP))).isFalse();

        // Every way to be not owned is non-TRUE (false, or NULL when a stamp is missing, exactly as the open-coded
        // AND was) and reads as the client's row through IS NOT TRUE, the spelling vectors-026-1 uses.
        List<String> notOwned = List.of(
            tagged("o", "2026-08-01T00:00:00Z", STAMP),
            "{\"quarantined_by\":\"engine-reaper\",\"quarantined_at\":\"" + STAMP + "\"}",
            "{\"quarantined_by\":\"engine-reaper\",\"reaper_quarantined_at\":\"" + STAMP + "\"}",
            "{\"quarantined_by\":\"engine-reaper\"}",
            "{\"quarantined_by\":\"client\",\"reaper_quarantined_at\":\"" + STAMP + "\",\"quarantined_at\":\""
                + STAMP + "\"}",
            "{\"reaper_quarantined_at\":\"" + STAMP + "\",\"quarantined_at\":\"" + STAMP + "\"}",
            "{}");
        for (String m : notOwned) {
            assertThat(owns(m)).as("not owned: %s", m).isNotEqualTo(true);
            assertThat(clientsRow(m)).as("IS NOT TRUE reads it as the client's: %s", m).isTrue();
        }
        assertThat(owns(notOwned.get(0))).as("stale reaper stamp is a plain false").isFalse();
    }

    // ---- 2. what makes it inlinable -----------------------------------------------------------------------------

    @Test
    void functionIsSqlImmutableSecurityInvokerAndExecutableByTheServiceRole() throws Exception {
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
            assertThat(r.value6()).as("nexus_svc can execute it").isTrue();
        }
    }

    /**
     * conexus's deploy census decides that vectors-026 landed by matching the stored body of
     * {@code nexus.gc_expire_quarantine} on {@code engine-reaper}. The literal moved into the shared predicate, so
     * the body keeps it in a comment; this fails if a later edit drops it and silently blinds that census.
     */
    @Test
    void gcExpireQuarantineBodyStillNamesEngineReaper_forTheDeployCensus() throws Exception {
        var src = DSL.field(DSL.name("p", "prosrc"), String.class);
        try (Connection su = pg.createConnection("")) {
            List<String> bodies = DSL.using(su, SQLDialect.POSTGRES).select(src)
                .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                .where(DSL.field(DSL.name("p", "proname"), String.class).eq("gc_expire_quarantine"))
                .fetch(src);
            assertThat(bodies).as("non-vacuity: the function exists").isNotEmpty();
            assertThat(bodies).allMatch(b -> b.contains("engine-reaper") && b.contains("reaper_owns_quarantined_row"));
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

    /**
     * A plan as the planner chose it: every node, index and condition, with the costs and row estimates left out of
     * the comparison. The function's body is the open-coded text, so after inlining the two plans are the same string.
     */
    private static String shape(String plan) {
        return plan.replaceAll("\\(cost=[^)]*\\)", "").replaceAll("Explain \\[[^\\]]*\\]", "")
            .replaceAll("[ \\t]+", " ").replaceAll(" ?\\| ?\\n", "\n").trim();
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
        assertThat(shape(viaFn)).as("%s: the same plan, node for node, as the open-coded predicate", what)
            .isEqualTo(shape(open));
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
    void distinctOriginRead_inlines() {
        Field<String> origin = key("origin_collection");
        String viaFn = plan(ctx -> ctx.selectDistinct(origin).from(CHUNKS)
            .where(scope().and(viaFunction()).and(origin.isNotNull())));
        assertThat(viaFn).doesNotContain("reaper_owns_quarantined_row").doesNotContain("Function Scan")
            .contains("chunks_pk").contains("quarantined_by");
    }

    // ---- 4. nothing open-codes it again --------------------------------------------------------------------------

    @Test
    void taggedOrigins_returnsExactlyTheEngineOwnedOrigins() {
        var repo = new ReaperRepository(tenantScope);
        assertThat(repo.taggedOrigins(TENANT, QUAR, 60_000))
            .as("the stale-tag origin and the client-moved origin are not the engine's")
            .containsExactly("origin-owned");
    }

    private static final Pattern KEY_READ = Pattern.compile("(?i)(quarantined_by|reaper_quarantined_at)");
    private static final Pattern XML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);
    private static final Pattern SQL_BLOCK = Pattern.compile("<sql[^>]*>(.*?)</sql>", Pattern.DOTALL);
    private static final Pattern ROLLBACK = Pattern.compile("<rollback>.*?</rollback>", Pattern.DOTALL);
    private static final Pattern SQL_LINE_COMMENT = Pattern.compile("--[^\\n]*");

    /** A read of the tag keys: {@code ->> 'key'} (any spacing), not a jsonb_build_object write or a {@code - 'key'} removal. */
    private static final Pattern SQL_READ = Pattern.compile("->>\\s*'(quarantined_by|reaper_quarantined_at)'");

    /** The body of the one definition, so a read inside it is not an offender. */
    private static final Pattern DEFINITION = Pattern.compile(
        "(?is)CREATE\\s+(?:OR\\s+REPLACE\\s+)?FUNCTION\\s+nexus\\.reaper_owns_quarantined_row\\b.*?\\$\\$.*?\\$\\$");

    @Test
    void noChangesetOutsideTheDefinitionReadsTheTagKeys() throws IOException {
        List<String> offenders = new ArrayList<>();
        int definitions = 0;
        int callSites = 0;
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "resources", "db", "changelog"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".xml")).sorted().toList()) {
                String forward = ROLLBACK.matcher(XML_COMMENT.matcher(Files.readString(p)).replaceAll("")).replaceAll("");
                Matcher blocks = SQL_BLOCK.matcher(forward);
                while (blocks.find()) {
                    String sql = SQL_LINE_COMMENT.matcher(blocks.group(1)).replaceAll("");
                    Matcher def = DEFINITION.matcher(sql);
                    if (def.find()) definitions++;
                    String outside = DEFINITION.matcher(sql).replaceAll("");
                    if (SQL_READ.matcher(outside).find()) {
                        offenders.add(p.getFileName() + ": reads quarantined_by / reaper_quarantined_at itself;"
                            + " call nexus.reaper_owns_quarantined_row");
                    }
                    Matcher call = Pattern.compile("reaper_owns_quarantined_row\\s*\\(").matcher(outside);
                    while (call.find()) callSites++;
                }
            }
        }
        assertThat(definitions).as("non-vacuity: the definition was found, exactly once").isEqualTo(1);
        assertThat(callSites).as("non-vacuity: reaper_expire_quarantine (3) and gc_expire_quarantine (2) call it")
            .isGreaterThanOrEqualTo(5);
        assertThat(offenders).isEmpty();
    }

    @Test
    void noJavaMainSourceReadsTheTagKeys() throws IOException {
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        Pattern block = Pattern.compile("/\\*.*?\\*/", Pattern.DOTALL);
        Pattern line = Pattern.compile("//[^\\n]*");
        Pattern literal = Pattern.compile("\"[^\"\\n]*(quarantined_by|reaper_quarantined_at)[^\"\\n]*\"");
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "java"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                String code = line.matcher(block.matcher(Files.readString(p)).replaceAll("")).replaceAll("");
                scanned++;
                if (literal.matcher(code).find()) {
                    offenders.add(p + ": names the quarantined_by / reaper_quarantined_at key in a string;"
                        + " call Routines.reaperOwnsQuarantinedRow");
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the scan walked the main sources").isGreaterThan(50);
        assertThat(offenders).isEmpty();
        // The scan's own blind spot would be a pattern that matches nothing; prove it matches what it exists to catch.
        assertThat(KEY_READ.matcher("c.metadata->>'quarantined_by'").find()).isTrue();
        assertThat(SQL_READ.matcher("c.metadata->>'reaper_quarantined_at'").find()).isTrue();
        assertThat(SQL_READ.matcher("c.metadata ->> 'quarantined_by'").find()).isTrue();
        assertThat(literal.matcher("DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, \"quarantined_by\")").find()).isTrue();
    }
}
