// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-156 P4.2b (bead nexus-joesk) — repository-layer contract for the combined-query
 * methods {@link PgVectorRepository#searchMetadataScoped} and
 * {@link PgVectorRepository#searchTopicScoped}, which the Python {@code query}/search
 * composition repoints onto.
 *
 * <p>The SQL-function behaviour (joins, NULL-skip, distance ordering, tombstone filter,
 * topic chash identity, EXPLAIN/HNSW) is exhaustively pinned by
 * {@code CombinedQueryParityTest}. This suite covers the REPOSITORY LAYER concerns that
 * test does not: server-side query embedding, per-dim dispatch + mixed-dim fail-loud,
 * the {@code nResults} guard, empty/blank input, the flat {@code (id, content,
 * collection, distance)} row envelope, and RLS scoping through the svc-role pool.
 *
 * <p>Chunks are superuser-seeded with explicit unit-vector literals (exact distances);
 * the query text is embedded by {@link PgVectorRepositoryContractTest.FakeEmbedder}
 * (registered {@code Q → (1,0)}), so the repository's server-side embed path is the
 * thing under test, not the embedder.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorCombinedQueryContractTest {

    private static final String SVC_ROLE = "svc_cqr_test";
    private static final String SVC_PASS = "svc_cqr_test_pass";

    private static final String TENANT_A = "cqr-tenant-a";
    private static final String TENANT_B = "cqr-tenant-b";

    private static final String COLL_M    = "knowledge__cqr-meta__voyage-context-3__v1";   // 1024
    private static final String COLL_T    = "knowledge__cqr-topic__voyage-context-3__v1";   // 1024
    private static final String COLL_MINI = "knowledge__cqr-mini__minilm-l6-v2-384__v1";    // 384
    private static final String COLL_B    = "knowledge__cqr-b__voyage-context-3__v1";       // 1024, tenant B

    private static final String TOPIC_VEC = "Vector Search";
    private static final String Q = "combined query probe";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : List.of("nexus_svc", SVC_ROLE)) {
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + role
                    + "') THEN CREATE ROLE " + role + " LOGIN PASSWORD '" + role
                    + "_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            }
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(),
                          DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                              new JdbcConnection(su))).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of("catalog_collections", "catalog_documents",
                    "catalog_document_chunks", "topics", "topic_assignments",
                    "chunks_384", "chunks_768", "chunks_1024")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder.register(Q, 1.0f, 0.0f);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // metadata fixture: m1 paper (1,0) d0; m2 paper (0.8,0.6) d0.2; m3 code (0.6,0.8) d0.4
            insertCollection(su, TENANT_A, COLL_M);
            seedMetaDoc(su, TENANT_A, 1024, COLL_M, "m1", "paper", 1.0, 0.0);
            seedMetaDoc(su, TENANT_A, 1024, COLL_M, "m2", "paper", 0.8, 0.6);
            seedMetaDoc(su, TENANT_A, 1024, COLL_M, "m3", "code",  0.6, 0.8);

            // topic fixture: tv1 (1,0) d0 in topic; tv2 (0.6,0.8) d0.4 in topic
            insertCollection(su, TENANT_A, COLL_T);
            long topic = insertTopic(su, TENANT_A, TOPIC_VEC, COLL_T);
            seedTopicChunk(su, TENANT_A, 1024, COLL_T, "tv1", topic, 1.0, 0.0);
            seedTopicChunk(su, TENANT_A, 1024, COLL_T, "tv2", topic, 0.6, 0.8);

            // tenant-B fixture (RLS)
            insertCollection(su, TENANT_B, COLL_B);
            seedMetaDoc(su, TENANT_B, 1024, COLL_B, "b1", "paper", 1.0, 0.0);
        }
    }

    // ── repository-layer behaviour ──────────────────────────────────────────────

    @Test
    void metadataScoped_filtersAndRanks_flatRowShape() {
        List<Map<String, Object>> rows =
            repo.searchMetadataScoped(TENANT_A, Q, List.of(COLL_M), "paper", null, null, null, 10);
        assertThat(ids(rows))
            .as("type=paper ranked by distance → [m1, m2]")
            .containsExactly("m1", "m2");
        Map<String, Object> top = rows.get(0);
        assertThat(top.keySet())
            .as("flat envelope: id, content, distance, collection, chash (catalog-008 "
                + "added the matched-chunk chash for the query() repoint / RDR-086)")
            .containsExactlyInAnyOrder("id", "content", "distance", "collection", "chash");
        assertThat(top.get("collection")).isEqualTo(COLL_M);
        assertThat((String) top.get("chash")).as("chash is the matched chunk's hash").isNotBlank();
        assertThat(((Number) top.get("distance")).doubleValue()).isCloseTo(0.0, within());
    }

    @Test
    void metadataScoped_multiCollection_arrayBind_rankedUnion() {
        // Exercises the ARRAY[?,?]::text[] bind path with >1 same-dim collection
        // (the single-element happy paths never reach the multi-element SQL).
        // COLL_T also holds tenant-A paper docs (tv1/tv2 via seedTopicChunk).
        List<String> got = ids(repo.searchMetadataScoped(
            TENANT_A, Q, List.of(COLL_M, COLL_T), "paper", null, null, null, 10));
        // m1 & tv1 both embed (1,0) → distance 0 (tie, no secondary sort), then
        // m2 (0.2), tv2 (0.4); m3 is code → excluded. Tie-safe assertion.
        assertThat(got).hasSize(4);
        assertThat(got.subList(0, 2))
            .as("the two distance-0 papers (m1, tv1) rank first, any tie order")
            .containsExactlyInAnyOrder("m1", "tv1");
        assertThat(got.subList(2, 4))
            .as("then m2 (0.2) then tv2 (0.4), in distance order")
            .containsExactly("m2", "tv2");
    }

    @Test
    void metadataScoped_nResultsTruncates() {
        assertThat(ids(repo.searchMetadataScoped(TENANT_A, Q, List.of(COLL_M), "paper", null, null, null, 1)))
            .as("nResults=1 → nearest paper only").containsExactly("m1");
    }

    @Test
    void metadataScoped_mixedDimensions_failLoud() {
        assertThatThrownBy(() ->
            repo.searchMetadataScoped(TENANT_A, Q, List.of(COLL_M, COLL_MINI), null, null, null, null, 10))
            .as("1024 + 384 cannot share one query vector — fail loud")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void metadataScoped_nonPositiveN_failLoud() {
        assertThatThrownBy(() ->
            repo.searchMetadataScoped(TENANT_A, Q, List.of(COLL_M), null, null, null, null, 0))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void metadataScoped_emptyCollections_returnsEmpty() {
        assertThat(repo.searchMetadataScoped(TENANT_A, Q, List.of(), null, null, null, null, 10)).isEmpty();
        assertThat(repo.searchMetadataScoped(TENANT_A, Q, null, null, null, null, null, 10)).isEmpty();
    }

    @Test
    void metadataScoped_rlsScoped_otherTenantSeesNothing() {
        assertThat(repo.searchMetadataScoped(TENANT_A, Q, List.of(COLL_B), "paper", null, null, null, 10))
            .as("tenant-A cannot see tenant-B's collection rows (RLS via SECURITY INVOKER)")
            .isEmpty();
    }

    @Test
    void topicScoped_chunkLevel_rankedByDistance() {
        List<Map<String, Object>> rows =
            repo.searchTopicScoped(TENANT_A, Q, TOPIC_VEC, COLL_T, 10);
        assertThat(ids(rows))
            .as("topic-scoped is chunk-level: ids are chunk chashes, ranked by distance")
            .containsExactly(chashOf("tv1"), chashOf("tv2"));
    }

    @Test
    void topicScoped_blankCollection_returnsEmpty() {
        assertThat(repo.searchTopicScoped(TENANT_A, Q, TOPIC_VEC, "", 10)).isEmpty();
    }

    @Test
    void topicScoped_nonPositiveN_failLoud() {
        assertThatThrownBy(() -> repo.searchTopicScoped(TENANT_A, Q, TOPIC_VEC, COLL_T, 0))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────

    private static org.assertj.core.data.Offset<Double> within() {
        return org.assertj.core.data.Offset.offset(1e-6);
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    private void seedMetaDoc(Connection su, String tenant, int dim, String collection,
                             String tumbler, String contentType, double x, double y) throws Exception {
        String chash = chashOf(tumbler);
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, content_type, physical_collection) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 'Doc', '" + contentType + "', '" + collection + "') "
            + "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
        insertManifest(su, tenant, tumbler, chash, collection);
        insertChunk(su, tenant, dim, collection, chash, tumbler, x, y);
    }

    private void seedTopicChunk(Connection su, String tenant, int dim, String collection,
                                String tumbler, long topicId, double x, double y) throws Exception {
        String chash = chashOf(tumbler);
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, content_type, physical_collection) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 'Doc', 'paper', '" + collection + "') "
            + "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
        insertManifest(su, tenant, tumbler, chash, collection);
        insertChunk(su, tenant, dim, collection, chash, tumbler, x, y);
        // chunk-level topic membership: topic_assignments.doc_id = chash (nexus-sa14p)
        su.createStatement().execute(
            "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', '" + chash + "', " + topicId + ", '" + collection + "', "
            + "'2026-01-01T00:00:00+00'::timestamptz) ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING");
    }

    private static void insertCollection(Connection su, String tenant, String name) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '" + name + "') "
            + "ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void insertManifest(Connection su, String tenant, String docId, String chash,
                                       String collection) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
            + "VALUES ('" + tenant + "', '" + docId + "', 0, '" + chash + "', '" + collection + "') "
            + "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    private static void insertChunk(Connection su, String tenant, int dim, String collection,
                                    String chash, String text, double x, double y) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim + " (tenant_id, collection, chash, chunk_text, embedding) "
            + "VALUES ('" + tenant + "', '" + collection + "', '" + chash + "', '" + text + "', "
            + vec2(dim, x, y) + "::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    private static long insertTopic(Connection su, String tenant, String label, String collection)
            throws Exception {
        try (var st = su.createStatement();
             var rs = st.executeQuery(
                "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) VALUES ('"
                + tenant + "', '" + label + "', '" + collection + "', '2026-01-01T00:00:00+00'::timestamptz) "
                + "RETURNING id")) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private static String vec2(int dim, double x, double y) {
        StringBuilder sb = new StringBuilder("'[").append(x).append(',').append(y);
        sb.append(",0".repeat(dim - 2));
        return sb.append("]'").toString();
    }

    private static String chashOf(String seed) {
        try {
            byte[] h = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(64);
            for (byte b : h) sb.append(String.format("%02x", b));
            return sb.substring(0, 32);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
