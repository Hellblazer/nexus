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
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

/**
 * nexus-v4pj4 (round-2 review decision) — {@code TaxonomyRepository#crossPreview}
 * contract suite: the READ-ONLY twin of {@code assignFromChashes}'s cross branch
 * that backs {@code nx doctor --check-assignments}'s same-moment ANN-vs-exact
 * comparison.
 *
 * <p>Covers: the preview's pick matches what {@code assignFromChashes} actually
 * persists for the SAME chunk over the SAME live centroids (the two functions'
 * {@code batch}/{@code nearest} CTEs are byte-identical per dim — see {@link
 * CrossPreviewDriftTest} for the prosrc proof that keeps them that way); the
 * preview NEVER writes to {@code topic_assignments}, even when called
 * repeatedly or after a real assignment already exists; a tenant cannot see
 * another tenant's centroids through this route (the same RLS isolation
 * {@code assignFromChashes} and {@code annQuery} already rely on); and the
 * {@code MAX_CROSS_PREVIEW_CHASHES} cap is enforced.
 *
 * <p>Fixture idiom matches {@link TaxonomyAssignFromChashesRepositoryTest}
 * exactly (unit vectors so cosine similarity is hand-computable; a dedicated
 * collection/tenant per test that seeds its own centroids, since a shared
 * PER_CLASS fixture would let one test's centroid win another's argmax).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CrossPreviewRepositoryTest {

    private static final String SVC_ROLE = "svc_xprev_test";
    private static final String SVC_PASS = "svc_xprev_test_pass";

    private static final String TENANT_A = "xprev-tenant-a";

    private static final String COL_CAP = "code__xprev_cap__voyage-code-3__v1";
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
            // EXECUTE ON FUNCTION is not part of bootstrapServiceRole's fixed grant
            // set (nexus-cbo4a batch 1b precedent) — both the preview functions
            // AND assign_from_chashes are exercised in this suite (the "matches
            // what got persisted" test calls both), so both need explicit grants.
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.cross_preview_" + dim
                    + "(text, text[]) TO " + SVC_ROLE);
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
    void crossPreview_matchesWhatAssignFromChashesActuallyPersists() throws Exception {
        String tenant = "xprev-tenant-match";
        String col = "code__xprev_match__voyage-code-3__v1";
        String colForeign = "code__xprev_matchfgn__voyage-code-3__v1";
        String c1 = hexChash("xprev-chash-match-1");
        seedChunk(tenant, col, c1, unit(1.0f, 0.0f));
        long tForeign = seedTopic(tenant, colForeign, "xprev-match-topic");
        seedCentroid(tenant, colForeign, tForeign, unit(0.6f, 0.8f));

        // The REAL persisting route, first — this is the ground truth the
        // preview must match exactly.
        Map<String, Object> persistedOut = repo.assignFromChashes(tenant, col, List.of(c1), true);
        assertThat(persistedOut.get("cross_assigned")).isEqualTo(1);
        List<Map<String, Object>> persisted = repo.getAssignmentDetails(tenant, List.of(c1));
        assertThat(persisted).singleElement();
        long persistedTopicId = (Long) persisted.get(0).get("topic_id");
        double persistedSimilarity = (Double) persisted.get(0).get("similarity");

        // The preview, over the SAME still-unchanged live centroid set.
        List<Map<String, Object>> preview = repo.crossPreview(tenant, col, List.of(c1));
        assertThat(preview).singleElement().satisfies(r -> {
            assertThat(r.get("chash")).isEqualTo(c1);
            assertThat(r.get("topic_id")).isEqualTo(persistedTopicId).isEqualTo(tForeign);
            assertThat((Double) r.get("similarity"))
                .isCloseTo(persistedSimilarity, within(1e-9))
                .isCloseTo(0.6, within(1e-4));
        });
    }

    @Test
    void crossPreview_neverPersists_evenWhenItDisagreesWithAnExistingAssignment() throws Exception {
        String tenant = "xprev-tenant-nopersist";
        String col = "code__xprev_nopersist__voyage-code-3__v1";
        String colForeignOld = "code__xprev_nopersist_old__voyage-code-3__v1";
        String colForeignNew = "code__xprev_nopersist_new__voyage-code-3__v1";
        String c1 = hexChash("xprev-chash-nopersist-1");
        seedChunk(tenant, col, c1, unit(1.0f, 0.0f));

        // Real assignment, made when only the WEAK foreign topic existed.
        long tOld = seedTopic(tenant, colForeignOld, "xprev-nopersist-old");
        seedCentroid(tenant, colForeignOld, tOld, unit(0.6f, 0.8f));
        Map<String, Object> out = repo.assignFromChashes(tenant, col, List.of(c1), true);
        assertThat(out.get("cross_assigned")).isEqualTo(1);
        assertThat(repo.getAssignmentDetails(tenant, List.of(c1)))
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(tOld));

        // A NEW, strictly closer foreign topic is discovered afterward.
        long tNew = seedTopic(tenant, colForeignNew, "xprev-nopersist-new");
        seedCentroid(tenant, colForeignNew, tNew, unit(1.0f, 0.0f)); // perfect match

        // The preview now disagrees with the stored row (tNew, not tOld) —
        // this is the exact "same-moment ANN vs an older decision" shape the
        // round-1 review's eligibility-cutoff mitigation existed to route
        // around; the preview route makes that mitigation moot for the
        // CLIENT (it now compares two same-moment values), but the ENGINE
        // side must still prove it never writes as a side effect of being
        // asked, called once or several times.
        List<Map<String, Object>> preview1 = repo.crossPreview(tenant, col, List.of(c1));
        assertThat(preview1).singleElement()
            .satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(tNew));
        List<Map<String, Object>> preview2 = repo.crossPreview(tenant, col, List.of(c1));
        assertThat(preview2).singleElement()
            .satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(tNew));

        // The STORED row must be completely unchanged by either preview call.
        assertThat(repo.getAssignmentDetails(tenant, List.of(c1)))
            .as("crossPreview must never write to topic_assignments")
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(tOld));
    }

    @Test
    void crossPreview_tenantCannotSeeAnotherTenantsCentroids() throws Exception {
        String owner = "xprev-tenant-owner";
        String viewer = "xprev-tenant-viewer";
        // Same collection NAME reused across tenants deliberately: RLS is the
        // only thing that should prevent viewer from seeing owner's centroid,
        // not accidental name-collision avoidance.
        String col = "code__xprev_shared_name__voyage-code-3__v1";
        String colForeign = "code__xprev_shared_namefgn__voyage-code-3__v1";

        long ownerTopic = seedTopic(owner, colForeign, "xprev-owner-topic");
        seedCentroid(owner, colForeign, ownerTopic, unit(1.0f, 0.0f));

        String c1 = hexChash("xprev-chash-tenant-isolation-1");
        seedChunk(viewer, col, c1, unit(1.0f, 0.0f));

        List<Map<String, Object>> preview = repo.crossPreview(viewer, col, List.of(c1));
        assertThat(preview)
            .as("viewer's tenant has a matching chunk but NO foreign centroid of its"
                + " own (owner's centroid is RLS-invisible) — must return no row, never"
                + " leak owner's topic_id")
            .isEmpty();
    }

    @Test
    void crossPreview_emptyChashes_returnsEmptyList() {
        assertThat(repo.crossPreview(TENANT_A, COL_CAP, List.of())).isEmpty();
    }

    @Test
    void crossPreview_maxChashesCap_atLimit_accepted() throws Exception {
        registerCollection(TENANT_A, COL_CAP);
        List<String> chashes = IntStream.range(0, TaxonomyRepository.MAX_CROSS_PREVIEW_CHASHES)
            .mapToObj(i -> hexChash("xprev-cap-at-limit-" + i))
            .toList();
        // None of these chashes were ever upserted as chunks — the cap check
        // happens before the query runs, so this proves acceptance, not a match.
        assertThat(repo.crossPreview(TENANT_A, COL_CAP, chashes)).isEmpty();
    }

    @Test
    void crossPreview_overCap_rejected() {
        List<String> chashes = IntStream.range(0, TaxonomyRepository.MAX_CROSS_PREVIEW_CHASHES + 1)
            .mapToObj(i -> hexChash("xprev-cap-over-limit-" + i))
            .toList();
        assertThatThrownBy(() -> repo.crossPreview(TENANT_A, COL_CAP, chashes))
            .as("MAX_CROSS_PREVIEW_CHASHES + 1 must be rejected")
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── helpers (same idiom as TaxonomyAssignFromChashesRepositoryTest) ─────────

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
                    + " (tenant_id, collection, chash, chunk_text, embedding_" + DIM + ")"
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

    private long seedTopic(String tenant, String collection, String label) throws Exception {
        registerCollection(tenant, collection);
        return repo.insertTopic(tenant, label, null, collection, 0, "2026-01-01T00:00:00Z", null);
    }

    private void seedCentroid(String tenant, String collection, long topicId, float[] emb) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids"
                    + " (tenant_id, collection, topic_id, label, embedding_" + DIM + ") VALUES (?, ?, ?, ?, ?::nexus.vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, "seed-centroid-label");
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
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
