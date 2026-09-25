// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-iygza (indexing-brittleness P0.1, engine half) — {@code
 * TaxonomyRepository#unassignedChashes} / {@code
 * nexus.taxonomy_unassigned_chashes_<dim>} contract suite
 * (taxonomy-019-unassigned-chashes.xml).
 *
 * <p>Sam's design (bd comment, 2026-09-25): derive from state, never a
 * pending table. "Unassigned" is defined precisely as: a chunk with a
 * non-null embedding at the collection's own dim, in a collection that HAS
 * centroids at that dim, carrying NO {@code topic_assignments} row whose
 * topic belongs to THIS collection ({@code topics.collection}). A chunk that
 * already carries a foreign ("projection") cross-collection assignment but
 * no own-collection one is STILL reported — the two passes are independent.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyUnassignedChashesRepositoryTest {

    private static final String SVC_ROLE = "svc_unassigned_test";
    private static final String SVC_PASS = "svc_unassigned_test_pass";

    private static final String TENANT_A = "unassigned-tenant-a";
    private static final String TENANT_B = "unassigned-tenant-b";

    private static final int DIM = 1024;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TaxonomyRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.taxonomy_unassigned_chashes_" + dim
                    + "(text, int) TO " + SVC_ROLE);
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.assign_from_chashes_" + dim
                    + "(text, text[], boolean) TO " + SVC_ROLE);
            }
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @Test
    void noCentroids_hasTaxonomyFalse_chashesEmpty() throws Exception {
        String col = "code__unassigned_nocentroids__voyage-code-3__v1";
        String c1 = hexChash("unassigned-nocentroids-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        // Deliberately NO centroids seeded for this collection.

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100);
        assertThat(out.get("has_taxonomy")).as("no centroids at all for this"
            + " collection's own dim").isEqualTo(false);
        assertThat((List<?>) out.get("chashes")).as("chashes must be empty when"
            + " has_taxonomy is false, never a partial/best-effort list").isEmpty();
    }

    @Test
    void unassignedChunk_listed_assignedChunk_not() throws Exception {
        String col = "code__unassigned_basic__voyage-code-3__v1";
        String cUnassigned = hexChash("unassigned-basic-unassigned");
        String cAssigned   = hexChash("unassigned-basic-assigned");
        seedChunk(TENANT_A, col, cUnassigned, unit(1.0f, 0.0f));
        seedChunk(TENANT_A, col, cAssigned, unit(0.0f, 1.0f));
        seedCentroid(TENANT_A, col, "unassigned-basic-topic", unit(1.0f, 0.0f));

        // Only cAssigned gets an own-collection assignment.
        repo.assignFromChashes(TENANT_A, col, List.of(cAssigned), false);

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100);
        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        assertThat((List<String>) (List<?>) out.get("chashes"))
            .as("only the chunk with no own-collection topic_assignments row")
            .containsExactly(cUnassigned);
    }

    @Test
    void chunkWithOnlyCrossCollectionAssignment_stillReportedUnassigned() throws Exception {
        // A chunk that carries a foreign ("projection") cross-collection
        // assignment but NO own-collection one is still "unassigned" for this
        // route's purpose -- the own and cross passes are independent (bd
        // comment, nexus-iygza). assignFromChashes always runs BOTH passes in
        // one call, so this test seeds the cross ('projection') row directly
        // (raw INSERT, same idiom TaxonomyAssignFromChashesRepositoryTest's
        // crossPass_onConflict_greatestSimilarityWins_bothDirections uses) to
        // isolate the scenario: col carries an own centroid of its own
        // (has_taxonomy=true) that c1 was simply never run against.
        String col = "code__unassigned_crossonly__voyage-code-3__v1";
        String colForeign = "code__unassigned_crossonlyfgn__voyage-code-3__v1";
        String c1 = hexChash("unassigned-crossonly-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, "unassigned-crossonly-topic-own", unit(0.0f, 1.0f));
        long tForeign = seedCentroid(TENANT_A, colForeign,
            "unassigned-crossonly-topic-foreign", unit(1.0f, 0.0f));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.topic_assignments"
                    + " (tenant_id, doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)"
                    + " VALUES (?, decode(?, 'hex'), ?, 'projection', 1.0, now(), ?)")) {
                ps.setString(1, TENANT_A);
                ps.setString(2, c1);
                ps.setLong(3, tForeign);
                ps.setString(4, col);
                ps.executeUpdate();
            }
        }

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100);
        assertThat(out.get("has_taxonomy")).as("col has its own centroid").isEqualTo(true);
        assertThat((List<String>) (List<?>) out.get("chashes"))
            .as("c1's ONLY topic_assignments row belongs to a topic in colForeign,"
                + " not col itself -- still reported unassigned")
            .containsExactly(c1);
    }

    @Test
    void limitHonoured() throws Exception {
        String col = "code__unassigned_limit__voyage-code-3__v1";
        seedCentroid(TENANT_A, col, "unassigned-limit-topic", unit(1.0f, 0.0f));
        String[] chashes = new String[5];
        for (int i = 0; i < 5; i++) {
            chashes[i] = hexChash("unassigned-limit-" + i);
            seedChunk(TENANT_A, col, chashes[i], unit(0.0f, 1.0f));
        }

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 2);
        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        assertThat((List<?>) out.get("chashes")).as("limit=2 honoured").hasSize(2);
    }

    @Test
    void limitOverMax_rejected() {
        assertThatThrownBy(() -> repo.unassignedChashes(TENANT_A,
                "code__unassigned_overlimit__voyage-code-3__v1",
                TaxonomyRepository.MAX_UNASSIGNED_CHASHES + 1))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void limitZeroOrNegative_rejected() {
        assertThatThrownBy(() -> repo.unassignedChashes(TENANT_A,
                "code__unassigned_zerolimit__voyage-code-3__v1", 0))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void anotherTenant_rowsInvisible() throws Exception {
        String col = "code__unassigned_rls__voyage-code-3__v1";
        String c1 = hexChash("unassigned-rls-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, "unassigned-rls-topic", unit(1.0f, 0.0f));

        // TENANT_B has never registered this collection at all -- fails loud via
        // CollectionRegistry, the same discipline assignFromChashes uses.
        assertThatThrownBy(() -> repo.unassignedChashes(TENANT_B, col, 100))
            .isInstanceOf(dev.nexus.service.db.UnregisteredCollectionException.class);
    }

    @Test
    void anotherTenant_sameCollectionName_seesOnlyItsOwnRows() throws Exception {
        // A DIFFERENT tenant registering the SAME collection NAME must see only
        // its own chunks/centroids (RLS), never TENANT_A's.
        String col = "code__unassigned_rls_shared__voyage-code-3__v1";
        String cA = hexChash("unassigned-rls-shared-a");
        String cB = hexChash("unassigned-rls-shared-b");
        seedChunk(TENANT_A, col, cA, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, "unassigned-rls-shared-topic-a", unit(1.0f, 0.0f));
        seedChunk(TENANT_B, col, cB, unit(0.0f, 1.0f));
        seedCentroid(TENANT_B, col, "unassigned-rls-shared-topic-b", unit(0.0f, 1.0f));

        Map<String, Object> outA = repo.unassignedChashes(TENANT_A, col, 100);
        Map<String, Object> outB = repo.unassignedChashes(TENANT_B, col, 100);
        assertThat((List<String>) (List<?>) outA.get("chashes")).containsExactly(cA);
        assertThat((List<String>) (List<?>) outB.get("chashes")).containsExactly(cB);
    }

    // ── helpers (mirrors TaxonomyAssignFromChashesRepositoryTest's own idiom) ────

    private static float[] unit(float x, float y) {
        float[] v = new float[DIM];
        v[0] = x;
        v[1] = y;
        return v;
    }

    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void seedChunk(String tenant, String collection, String hexChashValue, float[] emb) throws Exception {
        registerCollection(tenant, collection);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks"
                    + " (tenant_id, collection, chash, chunk_text, embedding_1024)"
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::nexus.vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setString(3, hexChashValue);
                ps.setString(4, "seed text " + hexChashValue);
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
    }

    private void registerCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    /** Seeds one topic + its centroid in {@code collection} (own-collection). */
    private long seedCentroid(String tenant, String collection, String label, float[] emb) throws Exception {
        registerCollection(tenant, collection);
        long topicId = repo.insertTopic(tenant, label, null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids"
                    + " (tenant_id, collection, topic_id, label, embedding_1024) VALUES (?, ?, ?, ?, ?::nexus.vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, "seed-centroid-" + label);
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
        return topicId;
    }

    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }
}
