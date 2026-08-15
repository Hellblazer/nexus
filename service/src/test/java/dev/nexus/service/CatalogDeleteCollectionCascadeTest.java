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
        // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 and
        // taxonomy_centroids_384/768/1024 are unified into single nexus.chunks /
        // nexus.taxonomy_centroids tables -- the cascade-count keys collapse from
        // three to one each, and the per-table count sums what three fixture
        // rows (2 + 1 + 1, one per dim) used to split across three keys.
        assertThat(counts.get("chunks")).as("chunks (unified, 2+1+1 across the three seeded dims)").isEqualTo(4);
        assertThat(counts).as("RDR-187: no chash_index leg in the cascade").doesNotContainKey("chash_index");
        assertThat(counts.get("topic_assignments")).as("topic_assignments (by source_collection)").isEqualTo(2);
        assertThat(counts.get("topics")).as("topics").isEqualTo(1);
        assertThat(counts.get("taxonomy_meta")).as("taxonomy_meta (RESTRICT child)").isEqualTo(1);
        // CROSS-LANGUAGE CONTRACT (T2 nexus/rdr-191-batch-E3-2026-08-13 [22462]):
        // collection_rename.py / collection_purge.py read this key literally as
        // "taxonomy_centroids" -- must not regress to a per-dim key shape.
        assertThat(counts.get("taxonomy_centroids")).as("taxonomy_centroids (unified, cugrk, 1+1+1)").isEqualTo(3);
        assertThat(counts.get("document_aspects")).as("document_aspects (incl doc-less)").isEqualTo(2);
        assertThat(counts.get("document_highlights")).as("document_highlights").isEqualTo(1);
        assertThat(counts.get("aspect_extraction_queue")).as("aspect_extraction_queue (tquoj, incl doc-less)").isEqualTo(2);
        assertThat(counts.get("catalog_documents")).as("catalog_documents").isEqualTo(1);
        assertThat(counts.get("catalog_collections")).as("registry row").isEqualTo(1);
    }

    @Test @Order(20)
    void deleteCollection_leavesNoOrphansForTenantA() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 and
            // taxonomy_centroids_384/768/1024 collapse to "chunks" and
            // "taxonomy_centroids" -- one orphan-check each, not three.
            for (String tbl : List.of("chunks", "topics", "taxonomy_meta",
                    "taxonomy_centroids",
                    "document_aspects", "document_highlights", "aspect_extraction_queue")) {
                assertThat(rows(su, "SELECT COUNT(*) FROM nexus." + tbl
                    + " WHERE tenant_id='" + TENANT_A + "' AND collection='" + COLL + "'"))
                    .as("no orphan rows in " + tbl).isZero();
            }
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
            // RDR-191 (nexus-o8dil.48): nexus.chunks unified -- all 4 seeded rows
            // (2x embedding_384 + 1x embedding_768 + 1x embedding_1024) are one
            // table now, not 2 in chunks_384 alone.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B chunks intact").isEqualTo(4);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B queue intact").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B centroids intact").isEqualTo(3);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + COLL + "'")).as("tenant B taxonomy_meta intact").isEqualTo(1);
        }
    }

    // ── F8d: collection-cascade scoping asymmetry (nexus-o8dil.40) ────────────

    private static final String TENANT_F8D = "del-casc-f8d";
    private static final String COLL_HOME = "knowledge__del-casc-f8d-home__voyage-context-3__v1";
    private static final String COLL_DEL  = "knowledge__del-casc-f8d-del__voyage-context-3__v1";

    /**
     * RDR-191 F8d: {@code deleteCollectionTxn} scopes T3 chunk deletion by
     * {@code chunks_*.collection} (step 1) but reaches the manifest ONLY via
     * the {@code catalog_documents} cascade, scoped by {@code
     * physical_collection} (step 6/fk-001). A manifest row whose OWN {@code
     * collection} column names the collection being deleted, but whose
     * PARENT DOCUMENT is homed in a DIFFERENT collection, sits outside both
     * scopes as currently wired: its chunk (scoped by chunks.collection) IS
     * deleted in step 1, but its manifest row (parent doc homed elsewhere)
     * is NOT reached by step 6's cascade -- a dangling manifest row,
     * produced by the very operation meant to clean up after itself. This
     * reproduces exactly that state and asserts the manifest row does not
     * survive the deletion of the collection it (denormalized-)names.
     */
    @Test @Order(40)
    void deleteCollection_scopingAsymmetry_manifestRowNamingDeletedCollection_alsoRemoved() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT_F8D + "', '" + COLL_HOME + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT_F8D + "', '" + COLL_DEL + "')");
            // The document is homed in COLL_HOME.
            su.createStatement().execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT_F8D + "', 'f8d-doc', 'F8D Doc', '" + COLL_HOME + "')");
            // Its chunk content lives in COLL_DEL (the collection about to be deleted).
            // RDR-191 (nexus-o8dil.48): nexus.chunks unified -- embedding_1024
            // replaces the bare embedding column now that chunks_1024 is retired.
            su.createStatement().execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024) "
                + "VALUES ('" + TENANT_F8D + "', '" + COLL_DEL + "', '" + chash("f8dasym") + "', 'text', " + vec(1024) + "::vector)");
            // The manifest row's OWN denormalized `collection` names COLL_DEL
            // too (drifted from the doc's real physical_collection -- the
            // exact shape F8c's reclassification-without-reindex leaves
            // behind), so it is reachable by neither scope symmetrically.
            su.createStatement().execute("INSERT INTO nexus.catalog_document_chunks "
                + "(tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + TENANT_F8D + "', 'f8d-doc', 0, '" + chash("f8dasym") + "', '" + COLL_DEL + "')");
        }

        repo.deleteCollection(TENANT_F8D, COLL_DEL);

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE tenant_id='" + TENANT_F8D + "' AND doc_id='f8d-doc' AND collection='" + COLL_DEL + "'"))
                .as("F8d: a manifest row denormalized to the deleted collection must not "
                    + "survive though its owning document is homed elsewhere")
                .isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks "
                + "WHERE tenant_id='" + TENANT_F8D + "' AND collection='" + COLL_DEL + "'"))
                .as("the T3 chunk content for the deleted collection is gone").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents "
                + "WHERE tenant_id='" + TENANT_F8D + "' AND tumbler='f8d-doc'"))
                .as("the owning document, homed in a DIFFERENT collection, must "
                    + "survive the deletion of COLL_DEL")
                .isEqualTo(1);
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
        // chunks: 2/1/1 across three dims, one unified nexus.chunks table (RDR-191).
        st.execute(chunkInsert(tenant, 384, "dc384a"));
        st.execute(chunkInsert(tenant, 384, "dc384b"));
        st.execute(chunkInsert(tenant, 768, "dc768a"));
        st.execute(chunkInsert(tenant, 1024, "dc1024a"));
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the manifest insert below -- reuse dc384a's
        // chash (rather than minting a fifth, distinct chunk) so the "chunks
        // (unified, 2+1+1)" count assertion above stays exactly 4.
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — the document above is
        // already registered under COLL, so stamp the manifest row the same.
        st.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
            + "VALUES ('" + tenant + "', 'dc-doc-1', 0, '" + chash("dc384a") + "', '" + COLL + "')");
        // (chash_index seeds removed — RDR-187/nexus-piwya.9: router dropped)
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
        // centroids: one per dim (cugrk), one unified nexus.taxonomy_centroids table
        // (RDR-191). PK is (tenant_id, collection, topic_id) -- three DIFFERENT
        // topic_ids, not the shared `topicId` above (which would collide on the
        // unified PK; pre-unification these lived in three separate physical
        // tables and could legally share one topic_id).
        st.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, embedding_384) "
            + "VALUES ('" + tenant + "', '" + COLL + "', " + topicId + ", " + vec(384) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, embedding_768) "
            + "VALUES ('" + tenant + "', '" + COLL + "', " + (topicId + 1) + ", " + vec(768) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, embedding_1024) "
            + "VALUES ('" + tenant + "', '" + COLL + "', " + (topicId + 2) + ", " + vec(1024) + "::vector)");
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

    /** RDR-191 (nexus-o8dil.48): chunks_384/768/1024 unified into nexus.chunks --
     *  {@code dim} now selects the target embedding_&lt;dim&gt; column, not a table. */
    private static String chunkInsert(String tenant, int dim, String seed) {
        return "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_" + dim + ") "
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
