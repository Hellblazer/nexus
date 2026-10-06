// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import static dev.nexus.service.PartitionScratch.CODE_3;
import static dev.nexus.service.PartitionScratch.MINILM_384;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 3 Step 2 (nexus-3wh8d.21 runbook): {@code nexus.drop_tenant_partitions(tenant)} removes a
 * tenant's chunk and centroid leaves, and the rows that reference its chunks. It is what the tenant-removal
 * runbook calls and what the Python test substrate calls when a test's minted tenant is done.
 *
 * <p>Run as the schema owner of a store migrated by a NOSUPERUSER NOBYPASSRLS role (production's nexus_admin
 * shape): the function is not SECURITY DEFINER, so the deletes are subject to FORCE ROW LEVEL SECURITY and the
 * function has to set the tenant scope itself. A superuser connection would bypass RLS and prove nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DropTenantPartitionsIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p225drop";
    private static final String ADMIN_PASS = "nexus_admin_p225drop_pass";

    /** The bystander whose every row and leaf must survive every drop; each test seeds the tenant it drops. */
    private static final String TB = "p225-drop-b";

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(cfg);
        SchemaMigrator.migrate(adminDs);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seed(ctx, TB, 2000L);
            // 'default' holds a token-less leaf set from the walk and one row of each kind
            PgContainerHelper.insertCollection(ctx, "default", coll1("dflt"));
            PgContainerHelper.insertChunk1024(ctx, "default", coll1("dflt"), hash(90), vec(1024));
        }
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private static String coll1(String tag) {
        return "code__" + tag + "-o__voyage-code-3__v1";
    }

    private static String coll2(String tag) {
        return "knowledge__" + tag + "-o__minilm-l6-v2-384__v1";
    }

    private static byte[] hash(int n) {
        byte[] b = new byte[32];
        b[0] = (byte) n;
        b[31] = (byte) (n + 1);
        return b;
    }

    private static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }

    private static Vector vec(int dim) {
        float[] f = new float[dim];
        Arrays.fill(f, 0.1f);
        f[0] = 0.5f;
        return Vector.of(f);
    }

    /**
     * A tenant with a token (so the trigger gives it leaves), two collections on two models, two chunks in each,
     * manifest rows for the 1024-wide pair, an assignment, an orphaned-at row and a centroid per collection.
     */
    private static void seed(DSLContext su, String tenant, long topicId) {
        PgContainerHelper.seedServiceToken(su, "tok-" + tenant, tenant, "p225drop");
        String c1 = coll1(tenant);
        String c2 = coll2(tenant);
        PgContainerHelper.insertCollection(su, tenant, c1);
        PgContainerHelper.insertCollection(su, tenant, c2);
        byte[] a = hash(1), b = hash(2), x = hash(3), y = hash(4);
        PgContainerHelper.insertChunk1024(su, tenant, c1, a, vec(1024));
        PgContainerHelper.insertChunk1024(su, tenant, c1, b, vec(1024));
        PgContainerHelper.insertChunk384(su, tenant, c2, x, vec(384));
        PgContainerHelper.insertChunk384(su, tenant, c2, y, vec(384));
        PgContainerHelper.ownChunks(su, tenant, c1, hex(a), hex(b));
        su.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(topicId, tenant, "t", c1, 0, OffsetDateTime.now(), "pending").execute();
        su.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID,
                TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
            .values(tenant, a, topicId, "projection", c1, OffsetDateTime.now(), CODE_3).execute();
        su.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH,
                CHUNK_ORPHANED_AT.ORPHANED_AT, CHUNK_ORPHANED_AT.EMBEDDING_MODEL)
            .values(tenant, c2, x, OffsetDateTime.now().minusDays(2), MINILM_384).execute();
        su.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                TAXONOMY_CENTROIDS.EMBEDDING_1024)
            .values(tenant, c1, topicId, CODE_3, "c1", vec(1024)).execute();
        su.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL,
                TAXONOMY_CENTROIDS.EMBEDDING_384)
            .values(tenant, c2, topicId + 1, MINILM_384, "c2", vec(384)).execute();
    }

    private Connection admin() throws SQLException {
        return DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
    }

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private static String sqlState(Throwable t) {
        for (Throwable x = t; x != null; x = x.getCause()) {
            if (x instanceof DataAccessException d && d.sqlState() != null) return d.sqlState();
            if (x instanceof SQLException s && s.getSQLState() != null) return s.getSQLState();
        }
        return null;
    }

    /** The tenant's leaves under both live parents, found by bound the way the function finds them. */
    private static List<String> leaves(DSLContext ctx, String tenant) {
        var out = new ArrayList<String>();
        String bound = "FOR VALUES IN ('" + tenant + "')";
        for (String parent : List.of("chunks", "taxonomy_centroids")) {
            for (var mp : PartitionScratch.children(ctx, parent)) {
                for (var leaf : PartitionScratch.children(ctx, mp.name())) {
                    if (leaf.bound().equals(bound)) out.add(leaf.name());
                }
            }
        }
        return out;
    }

    private static int modelPartitions(DSLContext ctx) {
        return PartitionScratch.children(ctx, "chunks").size() + PartitionScratch.children(ctx, "taxonomy_centroids").size();
    }

    /** Row counts of every table the function clears, for one tenant (read as superuser: RLS does not apply). */
    private Map<String, Integer> rowCounts(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            var m = new TreeMap<String, Integer>();
            m.put("chunks", ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant)));
            m.put("taxonomy_centroids", ctx.fetchCount(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID.eq(tenant)));
            m.put("catalog_document_chunks", ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)));
            m.put("topic_assignments", ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)));
            m.put("chunk_orphaned_at", ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)));
            return m;
        }
    }

    // ── the tests ────────────────────────────────────────────────────────────

    @Test
    void dropsTheTenantsLeavesAndItsReferencingRows_andLeavesAnotherTenantUntouched() throws Exception {
        final String TA = "p225-drop-a";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 1000L);
        }
        Map<String, Integer> bBefore = rowCounts(TB);
        Map<String, Integer> aBefore = rowCounts(TA);
        assertThat(aBefore).containsEntry("chunks", 4).containsEntry("taxonomy_centroids", 2)
            .containsEntry("catalog_document_chunks", 2).containsEntry("topic_assignments", 1)
            .containsEntry("chunk_orphaned_at", 1);
        List<String> bLeaves;
        int expectedLeaves;
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            assertThat(leaves(ctx, TA)).hasSize(modelPartitions(ctx));
            bLeaves = leaves(ctx, TB);
            expectedLeaves = modelPartitions(ctx);
        }

        int dropped;
        try (Connection a = admin()) {
            dropped = PartitionScratch.dropTenantPartitions(dsl(a), TA);
        }

        assertThat(dropped).isEqualTo(expectedLeaves);
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            assertThat(leaves(ctx, TA)).isEmpty();
            assertThat(leaves(ctx, TB)).containsExactlyInAnyOrderElementsOf(bLeaves);
        }
        assertThat(rowCounts(TA)).allSatisfy((t, n) -> assertThat(n).as("rows left in %s", t).isZero());
        assertThat(rowCounts(TB)).isEqualTo(bBefore);
    }

    @Test
    void aSecondCall_andACallForATenantWithNoLeaves_dropNothing() throws Exception {
        final String TA = "p225-drop-twice";
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), TA, 5000L);
        }
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            assertThat(PartitionScratch.dropTenantPartitions(ctx, TA)).isPositive();
            assertThat(PartitionScratch.dropTenantPartitions(ctx, TA)).isZero();
            assertThat(PartitionScratch.dropTenantPartitions(ctx, "p225-never-existed")).isZero();
            assertThat(leaves(ctx, TB)).isNotEmpty();
        }
    }

    @Test
    void theDefaultTenantAndANullTenantAreRefused_andNothingIsDropped() throws Exception {
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            int before = leaves(ctx, "default").size();
            assertThat(before).isEqualTo(modelPartitions(ctx));
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(ctx, "default"))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22023"));
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(ctx, null))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22004"));
            assertThat(leaves(ctx, "default")).hasSize(before);
        }
        assertThat(rowCounts("default").get("chunks")).isEqualTo(1);
    }

    @Test
    void theCallersTenantScopeIsRestored_andTheFunctionIsNotDefinerAndReachesNoEngineRole() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            seed(ctx, "p225-drop-scope", 3000L);
        }
        try (Connection a = admin()) {
            a.setAutoCommit(false);
            DSLContext ctx = dsl(a);
            PgContainerHelper.setTenant(a, "nexus.tenant", "caller-scope", true);
            PartitionScratch.dropTenantPartitions(ctx, "p225-drop-scope");
            assertThat(PartitionScratch.setting(ctx, "nexus.tenant")).isEqualTo("caller-scope");
            a.rollback();
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            // rolled back: the tenant is whole again, which also shows the drop is one transaction
            assertThat(leaves(ctx, "p225-drop-scope")).hasSize(modelPartitions(ctx));
            assertThat(PartitionScratch.functionFacts(ctx, "drop_tenant_partitions"))
                .as("not SECURITY DEFINER, fixed search_path, lock_timeout set, no PUBLIC or engine-role execute")
                .isEqualTo("definer=false;config=search_path=pg_catalog, pg_temp,lock_timeout=10s;public=false;nexus_svc=false");
        }
        try (Connection svc = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME,
                PgContainerHelper.SVC_PASSWORD)) {
            assertThatThrownBy(() -> PartitionScratch.dropTenantPartitions(dsl(svc), "p225-drop-scope"))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42501"));
        }
    }

    @Test
    void aDroppedTenantCanBeGivenItsLeavesAgain() throws Exception {
        try (Connection su = pg.createConnection("")) {
            seed(dsl(su), "p225-drop-redo", 4000L);
        }
        try (Connection a = admin()) {
            DSLContext ctx = dsl(a);
            PartitionScratch.dropTenantPartitions(ctx, "p225-drop-redo");
            assertThat(leaves(ctx, "p225-drop-redo")).isEmpty();
            assertThat(PartitionScratch.createTenantPartitions(ctx, "chunks", "p225-drop-redo", true)).isPositive();
            assertThat(PartitionScratch.createTenantPartitions(ctx, "taxonomy_centroids", "p225-drop-redo", true)).isPositive();
            assertThat(leaves(ctx, "p225-drop-redo")).hasSize(modelPartitions(ctx));
        }
    }
}
