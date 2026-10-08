// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-mz9jv: {@code nexus.collection_vector_stats} (vectors-032-1, a set-based join) returns exactly what the
 * vectors-019-5 definition (a {@code chunk_live_owners} probe per stored chunk) returned.
 *
 * <p>The old definition is created here under another name from the SAME text vectors-032-1's rollback
 * carries, and both views are read over one fixture as the service role (row-level security) and as the
 * superuser (every tenant). The fixture exercises each way liveness can differ: a chunk with one live
 * owner, one with two live owners (must count once), one owned only by a tombstoned document, one with no
 * owner, one owned only in ANOTHER collection (live(c) is per collection), a collection whose chunks are
 * all unowned (a row with {@code chunk_count} 0 and {@code last_write} NULL), mixed widths, a registered
 * collection with no chunk (absent), and a second tenant. The expected figures are asserted outright as
 * well, so the equality cannot hold vacuously over two empty views.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionVectorStatsSetBasedTest {

    static final String SVC_ROLE = "svc_cvs_sb";
    static final String SVC_PASS = "svc_cvs_sb_pass";
    static final String TENANT_A = "cvs-sb-a";
    static final String TENANT_B = "cvs-sb-b";
    static final String C_MIXED = "knowledge__cvs-sb-mixed__minilm-l6-v2-384__v1";
    static final String C_UNOWNED = "knowledge__cvs-sb-unowned__voyage-context-3__v1";
    static final String C_EMPTY = "knowledge__cvs-sb-empty__minilm-l6-v2-384__v1";
    static final String C_ELSEWHERE = "knowledge__cvs-sb-elsewhere__minilm-l6-v2-384__v1";
    static final String C_B = "knowledge__cvs-sb-b__minilm-l6-v2-384__v1";

    /** The vectors-019-5 definition, under a probe name (the rollback text of vectors-032-1, renamed). */
    static final String OLD_VIEW_DDL = """
        CREATE VIEW nexus.collection_vector_stats_v019
            WITH (security_invoker = true)
        AS
        SELECT c.tenant_id,
               c.collection,
               CASE WHEN c.embedding_384  IS NOT NULL THEN 384
                    WHEN c.embedding_768  IS NOT NULL THEN 768
                    WHEN c.embedding_1024 IS NOT NULL THEN 1024
               END AS dim,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash)))          AS chunk_count,
               max(c.created_at) FILTER (WHERE EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))) AS last_write,
               count(*)                                                AS stored_count
          FROM nexus.chunks c
         GROUP BY c.tenant_id, c.collection, 3
        """;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            // The statements jOOQ has no typed form for go through the test changelog (RawSqlGateTest keeps
            // raw SQL out of new test files).
            PgContainerHelper.runSuperuserDdl(su, OLD_VIEW_DDL);
            PgContainerHelper.runSuperuserDdl(su, "GRANT SELECT ON nexus.collection_vector_stats_v019 TO PUBLIC");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        seed();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    static String h(String s) {
        return Chash.ofText(s).toHex();
    }

    private static float[] vec(int dim, float x) {
        float[] v = new float[dim];
        v[0] = x;
        return v;
    }

    private static void chunks(DSLContext dsl, String tenant, String coll, int dim, String... names) {
        List<String> hex = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        int i = 0;
        for (String n : names) {
            hex.add(h(n));
            texts.add("text " + n);
            vecs.add(vec(dim, ++i));
            metas.add(Map.of());
        }
        PgContainerHelper.insertChunks(dsl, tenant, coll, hex, texts, vecs, metas);
    }

    /** A manifest row: {@code doc} owns {@code chash} in {@code coll}. */
    private static void owns(DSLContext dsl, String tenant, String doc, int position, String coll, String chashName) {
        PgContainerHelper.insertCatalogDocument(dsl, tenant, doc);
        String model = PgContainerHelper.collectionModel(dsl, tenant, coll);
        dsl.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
            .values(tenant, doc, position, Chash.ofText(chashName).toBytes(), coll, model)
            .execute();
    }

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(C_MIXED, C_EMPTY, C_ELSEWHERE)) {
                PgContainerHelper.insertCollection(dsl, TENANT_A, c);
            }
            PgContainerHelper.insertCollection(dsl, TENANT_A, C_UNOWNED, "voyage-context-3");
            PgContainerHelper.insertCollection(dsl, TENANT_B, C_B);

            // C_MIXED: c1,c2 one live owner; c3 two live owners; c4 only a tombstoned owner; c5 no owner.
            chunks(dsl, TENANT_A, C_MIXED, 384, "m1", "m2", "m3", "m4", "m5");
            owns(dsl, TENANT_A, "live-1", 0, C_MIXED, "m1");
            owns(dsl, TENANT_A, "live-1", 1, C_MIXED, "m2");
            owns(dsl, TENANT_A, "live-1", 2, C_MIXED, "m3");
            owns(dsl, TENANT_A, "live-2", 0, C_MIXED, "m3");
            owns(dsl, TENANT_A, "tomb-1", 0, C_MIXED, "m4");
            dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("tomb-1"))
                .execute();

            // C_UNOWNED (1024): three chunks, no manifest row at all.
            chunks(dsl, TENANT_A, C_UNOWNED, 1024, "u1", "u2", "u3");

            // C_ELSEWHERE: two chunks whose chash is owned only in C_MIXED's namespace (a manifest row names
            // its own collection), plus one properly owned: live(c) is per collection.
            chunks(dsl, TENANT_A, C_ELSEWHERE, 384, "e1", "e2", "e3");
            chunks(dsl, TENANT_A, C_MIXED, 384, "e1", "e2");   // the same text, in C_MIXED
            owns(dsl, TENANT_A, "live-1", 3, C_MIXED, "e1");
            owns(dsl, TENANT_A, "live-1", 4, C_MIXED, "e2");
            owns(dsl, TENANT_A, "live-3", 0, C_ELSEWHERE, "e3");

            // The other tenant.
            chunks(dsl, TENANT_B, C_B, 384, "b1", "b2");
            owns(dsl, TENANT_B, "live-b", 0, C_B, "b1");
        }
    }

    record Row(String tenant, String collection, Integer dim, long chunkCount, Object lastWrite, long storedCount) {}

    private static final org.jooq.Field<String> F_TENANT = DSL.field(DSL.name("tenant_id"), String.class);
    private static final org.jooq.Field<String> F_COLL = DSL.field(DSL.name("collection"), String.class);
    private static final org.jooq.Field<Integer> F_DIM = DSL.field(DSL.name("dim"), Integer.class);
    private static final org.jooq.Field<Long> F_LIVE = DSL.field(DSL.name("chunk_count"), Long.class);
    private static final org.jooq.Field<Object> F_LAST = DSL.field(DSL.name("last_write"), Object.class);
    private static final org.jooq.Field<Long> F_STORED = DSL.field(DSL.name("stored_count"), Long.class);

    private static org.jooq.Select<?> viewQuery(DSLContext ctx, String view) {
        return ctx.select(F_TENANT, F_COLL, F_DIM, F_LIVE, F_LAST, F_STORED)
            .from(DSL.table(DSL.name("nexus", view)))
            .orderBy(F_TENANT, F_COLL, F_DIM);
    }

    private static List<Row> rows(DSLContext ctx, String view) {
        List<Row> out = new ArrayList<>();
        for (var r : viewQuery(ctx, view).fetch()) {
            out.add(new Row(r.get(F_TENANT), r.get(F_COLL), r.get(F_DIM), r.get(F_LIVE), r.get(F_LAST),
                r.get(F_STORED)));
        }
        return out;
    }

    private List<Row> readAs(String tenant, String view) {
        return tenantScope.withTenant(tenant, ctx -> rows(ctx, view));
    }

    @Test
    void theSetBasedViewReturnsWhatTheVectors019ViewReturned_asEachTenantAndAsTheSuperuser() throws Exception {
        for (String tenant : List.of(TENANT_A, TENANT_B)) {
            List<Row> old = readAs(tenant, "collection_vector_stats_v019");
            List<Row> now = readAs(tenant, "collection_vector_stats");
            assertThat(now).as("tenant %s: the set-based view equals the per-chunk-probe view", tenant)
                .isEqualTo(old);
            assertThat(now).as("tenant %s: not vacuous", tenant).isNotEmpty();
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(rows(dsl, "collection_vector_stats")).isEqualTo(rows(dsl, "collection_vector_stats_v019"));
        }
    }

    @Test
    void theFigures_areTheLiveCountPerCollection_notTheStoredCount() {
        List<Row> a = readAs(TENANT_A, "collection_vector_stats");
        // C_MIXED: m1 m2 m3 (m3 has two owners, counted once) + e1 e2 (owned in C_MIXED) are live; m4 (tombstoned
        // owner) and m5 (no owner) are stored only.
        Row mixed = a.stream().filter(r -> r.collection().equals(C_MIXED)).findFirst().orElseThrow();
        assertThat(mixed.dim()).isEqualTo(384);
        assertThat(mixed.chunkCount()).as("live: m1 m2 m3 e1 e2").isEqualTo(5L);
        assertThat(mixed.storedCount()).as("stored: m1..m5 e1 e2").isEqualTo(7L);
        assertThat(mixed.lastWrite()).isNotNull();
        // C_UNOWNED: all unowned, still a row: inventory, with nothing live.
        Row unowned = a.stream().filter(r -> r.collection().equals(C_UNOWNED)).findFirst().orElseThrow();
        assertThat(unowned.dim()).isEqualTo(1024);
        assertThat(unowned.chunkCount()).isZero();
        assertThat(unowned.lastWrite()).isNull();
        assertThat(unowned.storedCount()).isEqualTo(3L);
        // C_ELSEWHERE: e3 is owned in its own collection; e1 e2 are owned only in C_MIXED, not here.
        Row elsewhere = a.stream().filter(r -> r.collection().equals(C_ELSEWHERE)).findFirst().orElseThrow();
        assertThat(elsewhere.chunkCount()).as("live(c) is per collection").isEqualTo(1L);
        assertThat(elsewhere.storedCount()).isEqualTo(3L);
        // A registered collection with no chunk has no row; the other tenant's collection is invisible.
        assertThat(a).extracting(Row::collection).doesNotContain(C_EMPTY, C_B);
        assertThat(readAs(TENANT_B, "collection_vector_stats")).extracting(Row::collection)
            .containsExactly(C_B);
    }

    /**
     * The point of the change, as a plan: the old view probes {@code chunk_live_owners} once per stored chunk
     * (a correlated SubPlan, twice), the new one joins the chunks to the live set once. Read as the service
     * role so the plan is the one the engine gets.
     */
    @Test
    void thePlan_hasNoPerChunkProbe_andTheOldViewsPlanDoes() {
        String oldPlan = tenantScope.withTenant(TENANT_A, ctx ->
            ctx.explain(ctx.selectFrom(DSL.table(DSL.name("nexus", "collection_vector_stats_v019")))).plan());
        String newPlan = tenantScope.withTenant(TENANT_A, ctx ->
            ctx.explain(ctx.selectFrom(DSL.table(DSL.name("nexus", "collection_vector_stats")))).plan());
        assertThat(oldPlan).as("control: the vectors-019-5 view correlates a probe per chunk:%n%s", oldPlan)
            .contains("SubPlan");
        assertThat(newPlan).as("the set-based view has no correlated SubPlan:%n%s", newPlan)
            .doesNotContain("SubPlan");
        assertThat(newPlan).as("it joins the live set once:%n%s", newPlan).contains("Join");
    }

    @Test
    void disableJit_turnsJitOffForTheTransaction() {
        String inside = tenantScope.withTenant(TENANT_A, ctx -> {
            PgSession.disableJit(ctx);
            return ctx.select(DSL.function("current_setting", org.jooq.impl.SQLDataType.VARCHAR, DSL.inline("jit"))).fetchOne(0, String.class);
        });
        assertThat(inside).isEqualTo("off");
    }
}
