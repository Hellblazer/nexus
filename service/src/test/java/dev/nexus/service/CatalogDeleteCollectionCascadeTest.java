// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-164 P2 (bead nexus-ybdoc) — CatalogRepository.deleteCollection ordered-DELETE cascade.
 *
 * <p>Verifies the single transactional service-side collection delete: it purges every
 * in-Postgres lifecycle table in dependency order (registry row last, RESTRICT FKs as a
 * safety net), returns per-table counts, leaves no orphans, and is tenant-isolated via RLS.
 * Two regression anchors: nexus-tquoj (aspect_extraction_queue purged, incl. doc-less
 * NULL-doc_id rows the fk-001 document cascade cannot reach) and nexus-cugrk
 * (taxonomy_centroids_* purged by collection — no FK to topics).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogDeleteCollectionCascadeTest {

    private static final String TENANT_A = "del-casc-a";
    private static final String TENANT_B = "del-casc-b";
    private static final String COLL = "knowledge__del-casc__voyage-context-3__v1";
    private static final String SVC_ROLE = "svc_del_casc";
    private static final String SVC_PASS = "svc_del_casc_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));

        // Seed an identical collection under BOTH tenants (superuser bypasses RLS).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedFullCollection(su, TENANT_A);
            seedFullCollection(su, TENANT_B);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(10)
    void deleteCollection_returnsExactPerTableCounts() {
        // nexus-h8rf6 wave review: a pre-delete CollectionRegistry entry must be
        // evicted by the delete — a stale entry would make later writers skip
        // re-registration if the collection name is reused.
        dev.nexus.service.db.CollectionRegistry.markKnown(TENANT_A, COLL);
        Map<String, Integer> counts = repo.deleteCollection(TENANT_A, COLL);
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(TENANT_A, COLL))
            .as("registry cache evicted post-delete").isFalse();
        assertThat(counts.get("chunks_384")).as("chunks_384").isEqualTo(2);
        assertThat(counts.get("chunks_768")).as("chunks_768").isEqualTo(1);
        assertThat(counts.get("chunks_1024")).as("chunks_1024").isEqualTo(1);
        assertThat(counts.get("chash_index")).as("chash_index").isEqualTo(2);
        assertThat(counts.get("topic_assignments")).as("topic_assignments (by source_collection)").isEqualTo(2);
        assertThat(counts.get("topics")).as("topics").isEqualTo(1);
        assertThat(counts.get("taxonomy_meta")).as("taxonomy_meta (RESTRICT child)").isEqualTo(1);
        assertThat(counts.get("taxonomy_centroids_384")).as("centroids_384 (cugrk)").isEqualTo(1);
        assertThat(counts.get("taxonomy_centroids_768")).as("centroids_768").isEqualTo(1);
        assertThat(counts.get("taxonomy_centroids_1024")).as("centroids_1024").isEqualTo(1);
        assertThat(counts.get("document_aspects")).as("document_aspects (incl doc-less)").isEqualTo(2);
        assertThat(counts.get("document_highlights")).as("document_highlights").isEqualTo(1);
        assertThat(counts.get("aspect_extraction_queue")).as("aspect_extraction_queue (tquoj, incl doc-less)").isEqualTo(2);
        assertThat(counts.get("catalog_documents")).as("catalog_documents").isEqualTo(1);
        assertThat(counts.get("catalog_collections")).as("registry row").isEqualTo(1);
    }

    @Test @Order(20)
    void deleteCollection_leavesNoOrphansForTenantA() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String tbl : List.of("chunks_384", "chunks_768", "chunks_1024", "topics", "taxonomy_meta",
                    "taxonomy_centroids_384", "taxonomy_centroids_768", "taxonomy_centroids_1024",
                    "document_aspects", "document_highlights", "aspect_extraction_queue")) {
                assertThat(rows(su, "SELECT COUNT(*) FROM nexus." + tbl
                    + " WHERE tenant_id='" + TENANT_A + "' AND collection='" + COLL + "'"))
                    .as("no orphan rows in " + tbl).isZero();
            }
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chash_index WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + COLL + "'")).as("chash_index orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT_A
                + "' AND source_collection='" + COLL + "'")).as("assignment orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + COLL + "'")).as("catalog_documents orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + TENANT_A
                + "' AND doc_id='dc-doc-1'")).as("manifest rows cascaded via catalog_documents").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + COLL + "'")).as("registry row gone").isZero();
        }
    }

    @Test @Order(30)
    void deleteCollection_isTenantIsolated_tenantBUntouched() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_B
                + "' AND name='" + COLL + "'")).as("tenant B registry intact").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B chunks intact").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B queue intact").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids_384 WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B centroids intact").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B taxonomy_meta intact").isEqualTo(1);
        }
    }

    // ── fixture ──────────────────────────────────────────────────────────────

    /** Seed one full collection (all lifecycle tables) for {@code tenant}. Superuser; bypasses RLS. */
    private static void seedFullCollection(Connection su, String tenant) throws Exception {
        var st = su.createStatement();
        st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '" + COLL + "')");
        // catalog_documents + manifest
        st.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
            + "VALUES ('" + tenant + "', 'dc-doc-1', 'Doc 1', '" + COLL + "')");
        st.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
            + "VALUES ('" + tenant + "', 'dc-doc-1', 0, '" + chash("dcman1") + "')");
        // chunks: 2/1/1
        st.execute(chunkInsert(tenant, "chunks_384", 384, "dc384a"));
        st.execute(chunkInsert(tenant, "chunks_384", 384, "dc384b"));
        st.execute(chunkInsert(tenant, "chunks_768", 768, "dc768a"));
        st.execute(chunkInsert(tenant, "chunks_1024", 1024, "dc1024a"));
        // chash_index: 2
        st.execute("INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
            + "VALUES ('" + tenant + "', '" + chash("dcci1") + "', '" + COLL + "', NOW())");
        st.execute("INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
            + "VALUES ('" + tenant + "', '" + chash("dcci2") + "', '" + COLL + "', NOW())");
        // topics: 1 (explicit id)
        long topicId = Math.abs((long) (tenant + COLL).hashCode());
        st.execute("INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) "
            + "VALUES (" + topicId + ", '" + tenant + "', 'topic-dc', '" + COLL + "', 0, NOW(), 'pending')");
        // taxonomy_meta: 1 (fk-003-4 RESTRICT — must be purged before the registry row)
        st.execute("INSERT INTO nexus.taxonomy_meta (tenant_id, collection) VALUES ('" + tenant + "', '" + COLL + "')");
        // topic_assignments: 2, both with source_collection=COLL, referencing the topic
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', 'dc-doc-1', " + topicId + ", 'projection', '" + COLL + "', NOW())");
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', 'dc-doc-2', " + topicId + ", 'projection', '" + COLL + "', NOW())");
        // centroids: one per dim (cugrk)
        st.execute("INSERT INTO nexus.taxonomy_centroids_384 (tenant_id, collection, topic_id, embedding) "
            + "VALUES ('" + tenant + "', '" + COLL + "', " + topicId + ", " + vec(384) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids_768 (tenant_id, collection, topic_id, embedding) "
            + "VALUES ('" + tenant + "', '" + COLL + "', " + topicId + ", " + vec(768) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids_1024 (tenant_id, collection, topic_id, embedding) "
            + "VALUES ('" + tenant + "', '" + COLL + "', " + topicId + ", " + vec(1024) + "::vector)");
        // document_aspects: 2 — one doc-rooted, one DOC-LESS (doc_id=NULL)
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '/p/a1.md', NOW(), 'v1', 'docling', 'dc-doc-1')");
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '/p/a2.md', NOW(), 'v1', 'docling', NULL)");
        // document_highlights: 1 (doc-rooted)
        st.execute("INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, highlights_md, ingested_at) "
            + "VALUES ('" + tenant + "', 'dc-doc-1', '" + COLL + "', 'hi', NOW())");
        // aspect_extraction_queue: 2 — one doc-rooted, one DOC-LESS (doc_id=NULL) = the tquoj orphan class
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '/p/q1.md', 'pending', NOW(), 'dc-doc-1')");
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '/p/q2.md', 'pending', NOW(), NULL)");
    }

    private static String chunkInsert(String tenant, String table, int dim, String seed) {
        return "INSERT INTO nexus." + table + " (tenant_id, collection, chash, chunk_text, embedding) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '" + chash(seed) + "', 'text', " + vec(dim) + "::vector)";
    }

    private static String vec(int dim) {
        return IntStream.range(0, dim).mapToObj(i -> "0.1").collect(Collectors.joining(",", "'[", "]'"));
    }

    private static String chash(String seed) {
        return (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
    }

    private static int rows(Connection su, String sql) throws Exception {
        var rs = su.createStatement().executeQuery(sql);
        rs.next();
        return rs.getInt(1);
    }
}
