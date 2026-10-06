// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 (bead nexus-wbfpw.15): the plan {@code nexus.chunk_is_reapable} gets under the REAL
 * RLS-subject role, and the schema decision "no index on {@code last_written_at}".
 *
 * <p>Planned through {@code nexus_svc} (a NOSUPERUSER NOBYPASSRLS role with the tenant GUC stamped
 * transaction-locally), never a superuser connection, which skips the security barrier that decides which
 * quals may become index conditions. The fixture has the shape of a production collection: 30,000 chunks of
 * which 10,000 are orphans, 20,000 manifest rows, 1,000 documents, and a second collection that must not be
 * touched. Everything is typed jOOQ (the plan is EXPLAIN without ANALYZE, so it asserts shape, not timing),
 * so this class adds nothing to the raw-SQL ratchet.
 *
 * <p>What it pins:
 * <ul>
 *   <li>the function inlines: no function scan and no function name in any plan;</li>
 *   <li>the candidate scan is the {@code chunks_pk} range over (tenant, collection);</li>
 *   <li>the manifest probe is {@code idx_catalog_chunks_chash};</li>
 *   <li>the quarantine probe is the primary key of {@code catalog_collections};</li>
 *   <li>the orphaning-record probe is the primary key of {@code chunk_orphaned_at}, with no sequential scan;</li>
 *   <li>and there is NO index on {@code nexus.chunks (last_written_at)}: a btree on it would stop HOT updates
 *       for every client re-write (nexus-wbfpw.43 review item 7). That is asserted against the schema the
 *       changelog builds, so a changeset that adds one turns this red. (An earlier version of this test
 *       created its own temporary index and measured that, which no production edit could fail. The
 *       measurement stands as the justification: with page room, a 3,000-row refresh was 100% HOT without
 *       the index and 0% with it.)</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkIsReapablePlanIntegrationTest {

    private static final String SVC_ROLE = "svc_reapable_plan";
    private static final String SVC_PASS = "svc_reapable_plan_pass";
    private static final String TENANT = "reap-plan-t";
    private static final String COL = "knowledge__reap-plan-a__minilm-l6-v2-384__v1";
    private static final String OTHER = "knowledge__reap-plan-b__minilm-l6-v2-384__v1";

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
            PgContainerHelper.insertCollection(ctx, TENANT, COL);
            PgContainerHelper.insertCollection(ctx, TENANT, OTHER);
            // A realistic catalog_collections: with two rows the planner rightly seq-scans it.
            for (int i = 0; i < 300; i++) {
                PgContainerHelper.insertCollection(ctx, TENANT, "knowledge__reap-plan-pad" + i + "__minilm-l6-v2-384__v1");
            }

            OffsetDateTime old = OffsetDateTime.now().minusDays(40);
            Vector zero = Vector.of(new float[384]);
            insertChunks(ctx, COL, "a", 30000, old, zero);
            insertChunks(ctx, OTHER, "b", 15000, old, zero);

            var docs = DSL.generateSeries(0, 999).as("g", "n");
            Field<Integer> docN = docs.field("n", Integer.class);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .select(ctx.select(DSL.inline(TENANT), DSL.concat(DSL.inline("doc"), DSL.cast(docN, SQLDataType.VARCHAR)),
                                  DSL.inline("t"), DSL.inline(COL)).from(docs))
               .execute();

            // 20,000 of the 30,000 chunks are owned.
            var rows = DSL.generateSeries(1, 20000).as("g", "n");
            Field<Integer> n = rows.field("n", Integer.class);
            // RDR-225: the manifest and orphaning rows carry the model of the chunk they reference.
            final String model = PgContainerHelper.collectionModel(ctx, TENANT, COL);
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
               .select(ctx.select(DSL.inline(TENANT),
                                  DSL.concat(DSL.inline("doc"), DSL.cast(n.mod(1000), SQLDataType.VARCHAR)),
                                  n, chash("a", n), DSL.inline(COL), DSL.inline(model)).from(rows))
               .execute();

            // 8,000 of the 10,000 orphans carry an orphaning record, as they would after a deletion or a
            // re-index dropped their owners. The table must be non-trivial and analyzed for the planner to
            // prefer the primary-key probe to a sequential scan, which is also what production looks like
            // once anything has been orphaned (the header of vectors-021 says what an empty table plans).
            var orphans = DSL.generateSeries(20001, 28000).as("g", "n");
            Field<Integer> on = orphans.field("n", Integer.class);
            ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
                    CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.ORPHANED_AT, CHUNK_ORPHANED_AT.EMBEDDING_MODEL)
               .select(ctx.select(DSL.inline(TENANT), DSL.inline(COL), chash("a", on), DSL.val(old),
                                  DSL.inline(model)).from(orphans))
               .execute();

            PgContainerHelper.analyzeTable(su, CHUNK_ORPHANED_AT);
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_COLLECTIONS);
        }
    }

    private static Field<byte[]> chash(String prefix, Field<Integer> n) {
        return DSL.function("sha256", SQLDataType.BLOB,
            DSL.cast(DSL.concat(DSL.inline(prefix), DSL.cast(n, SQLDataType.VARCHAR)), SQLDataType.BLOB));
    }

    private static void insertChunks(DSLContext ctx, String collection, String prefix, int count,
                                     OffsetDateTime when, Vector vector) {
        var series = DSL.generateSeries(1, count).as("g", "n");
        Field<Integer> n = series.field("n", Integer.class);
        // RDR-225: a chunk carries its collection's model (minilm-l6-v2-384, matching the 384-wide vector).
        final String model = PgContainerHelper.collectionModel(ctx, TENANT, collection);
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.CREATED_AT, CHUNKS.LAST_WRITTEN_AT)
           .select(ctx.select(DSL.inline(TENANT), DSL.inline(collection), chash(prefix, n), DSL.inline(model),
                              DSL.inline("x"),
                              DSL.val(vector, CHUNKS.EMBEDDING_384.getDataType()),
                              DSL.val(when), DSL.val(when)).from(series))
           .execute();
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static org.jooq.Condition reapable() {
        return DSL.exists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
            CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.LAST_WRITTEN_AT,
            DSL.val((YearToSecond) null, SQLDataType.INTERVAL))));
    }

    private String plan(java.util.function.Function<DSLContext, org.jooq.Query> query) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.explain(query.apply(ctx)).toString());
    }

    @Test
    void listingPlan_inlines_andProbesTheirIndexes() {
        String plan = plan(ctx -> ctx.select(CHUNKS.CHASH).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(COL))).and(reapable())
            .orderBy(CHUNKS.CHASH).limit(300));
        System.out.println("\n=== reapable listing, nexus_svc (EXPLAIN)\n" + plan);

        assertPlanShape(plan);
    }

    @Test
    void deletePlan_hasTheGraceQualOnTheTargetRow_soAReadCommittedRecheckSeesARacingRefresh() {
        String plan = plan(ctx -> ctx.deleteFrom(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(COL))).and(reapable()));
        System.out.println("\n=== reapable DELETE, nexus_svc (EXPLAIN)\n" + plan);

        assertPlanShape(plan);
        assertThat(plan).as("the grace comparison is evaluated on the DELETE's own rows").contains("last_written_at");
        assertThat(plan).contains("Delete on chunks");
    }

    /** {@code nexus.partition_name('chunks', model, tenant)}: the tenant's leaf under the model partition. */
    private static String leafName(String model, String tenant) {
        return "chunks_m" + sha256Hex(model).substring(0, 8) + "_t_" + sha256Hex(tenant).substring(0, 16);
    }

    private static String sha256Hex(String s) {
        try {
            return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256")
                .digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static void assertPlanShape(String plan) {
        assertThat(plan).as("inlined, not an opaque call:%n%s", plan)
            .doesNotContain("chunk_is_reapable").doesNotContain("Function Scan");
        // RDR-225: nexus.chunks is LIST-partitioned by model then tenant, so the primary-key range scanned is
        // the one of the tenant's LEAF under the collection's model partition (Postgres names a leaf's primary
        // key index <leaf>_pkey), not the parent's chunks_pk.
        assertThat(plan).as("candidates come from the (tenant, collection) primary-key range:%n%s", plan)
            .contains(leafName("minilm-l6-v2-384", TENANT) + "_pkey");
        assertThat(plan).as("manifest probe:%n%s", plan).contains("idx_catalog_chunks_chash");
        assertThat(plan).as("quarantine probe is the catalog_collections primary key:%n%s", plan)
            .containsPattern("Index (Only )?Scan using \\w*catalog_collections\\w*");
        assertThat(plan).as("the orphaning record is probed by its primary key, never scanned:%n%s", plan)
            .containsPattern("Index (Only )?Scan using chunk_orphaned_at_pk")
            .doesNotContainPattern("Seq Scan on chunk_orphaned_at");
    }

    @Test
    void thereIsNoIndexOnLastWrittenAt_becauseItWouldStopHotUpdates() throws Exception {
        var indexes = DSL.table(DSL.name("pg_catalog", "pg_indexes"));
        var schema = DSL.field(DSL.name("schemaname"), String.class);
        var table = DSL.field(DSL.name("tablename"), String.class);
        var def = DSL.field(DSL.name("indexdef"), String.class);
        List<String> defs;
        try (Connection su = pg.createConnection("")) {
            defs = DSL.using(su, SQLDialect.POSTGRES).select(def).from(indexes)
                .where(schema.eq("nexus").and(table.eq("chunks"))).fetch(def);
        }

        assertThat(defs).as("non-vacuity: nexus.chunks has indexes, so the scan looked at something")
            .isNotEmpty();
        assertThat(defs).as("a btree on last_written_at stops HOT updates for every client re-write"
            + " (nexus-wbfpw.43 review item 7)").noneMatch(d -> d.contains("last_written_at"));
    }
}
