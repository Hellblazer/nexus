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
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-164 P3 (bead nexus-77vve) — CatalogRepository.renameCollection coherent re-home.
 *
 * <p>Verifies the single transactional service-side collection rename: under the fk-002/fk-003
 * {@code ON UPDATE NO ACTION} FKs, it re-homes every in-Postgres denorm-collection table X-&gt;Y
 * by INSERT-new-registry / re-home-children / DELETE-old-registry (never UPDATEs
 * catalog_collections.name), returns per-table counts, leaves no orphan under the old name,
 * round-trips X-&gt;Y-&gt;X, is tenant-isolated via RLS, and preserves the RDR-162 cross-model
 * COPY branch (target already registered → repoint documents only, both registry rows kept).
 * The chunks-present case is the one the pre-P3 bare-UPDATE rename could not satisfy.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogRenameCollectionTest {

    private static final String TENANT_A = "ren-a";
    private static final String TENANT_B = "ren-b";
    private static final String TENANT_C = "ren-c";
    private static final String OLD  = "knowledge__ren__minilm-l6-v2-384__v1";
    private static final String NEW  = "knowledge__ren__minilm-l6-v2-384__v2";
    private static final String SVC_ROLE = "svc_ren_casc";
    private static final String SVC_PASS = "svc_ren_casc_pass";

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

        // Seed an identical full collection under BOTH tenants (superuser bypasses RLS).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedFullCollection(su, TENANT_A, OLD);
            seedFullCollection(su, TENANT_B, OLD);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(10)
    void renameCollection_returnsExactPerTableCounts_chunksPresent() {
        // The chunks-present case: the pre-P3 bare UPDATE catalog_collections.name was blocked
        // by NO-ACTION children; the coherent re-home must succeed and report every table.
        // nexus-h8rf6 wave review: the canonical branch deletes registry row X — the
        // CollectionRegistry cache must evict OLD and mark NEW known.
        dev.nexus.service.db.CollectionRegistry.markKnown(TENANT_A, OLD);
        Map<String, Integer> c = repo.renameCollection(TENANT_A, OLD, NEW);
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(TENANT_A, OLD))
            .as("registry cache evicted for old name").isFalse();
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(TENANT_A, NEW))
            .as("registry cache marked for new name").isTrue();
        assertThat(c.get("catalog_collections_inserted")).as("registry Y inserted").isEqualTo(1);
        assertThat(c.get("chunks_384")).as("chunks_384").isEqualTo(2);
        assertThat(c.get("chunks_768")).as("chunks_768").isEqualTo(1);
        assertThat(c.get("chunks_1024")).as("chunks_1024").isEqualTo(1);
        assertThat(c.get("chash_index")).as("chash_index").isEqualTo(2);
        assertThat(c.get("topic_assignments")).as("topic_assignments (by source_collection)").isEqualTo(2);
        assertThat(c.get("topics")).as("topics").isEqualTo(1);
        assertThat(c.get("taxonomy_meta")).as("taxonomy_meta (RESTRICT child)").isEqualTo(1);
        assertThat(c.get("taxonomy_centroids_384")).as("centroids_384").isEqualTo(1);
        assertThat(c.get("taxonomy_centroids_768")).as("centroids_768").isEqualTo(1);
        assertThat(c.get("taxonomy_centroids_1024")).as("centroids_1024").isEqualTo(1);
        assertThat(c.get("document_aspects")).as("document_aspects (incl doc-less)").isEqualTo(2);
        assertThat(c.get("document_highlights")).as("document_highlights").isEqualTo(1);
        assertThat(c.get("aspect_extraction_queue")).as("aspect_extraction_queue (incl doc-less)").isEqualTo(2);
        assertThat(c.get("catalog_documents")).as("catalog_documents").isEqualTo(1);
        assertThat(c.get("relevance_log")).as("relevance_log (re-homed, no FK)").isEqualTo(2);
        assertThat(c.get("search_telemetry")).as("search_telemetry (re-homed, no FK)").isEqualTo(2);
        assertThat(c.get("hook_failures")).as("hook_failures (re-homed, no FK)").isEqualTo(1);
        assertThat(c.get("catalog_collections_deleted")).as("registry X deleted").isEqualTo(1);
    }

    @Test @Order(20)
    void renameCollection_noOrphanUnderOldName_allPresentUnderNew() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String tbl : List.of("chunks_384", "chunks_768", "chunks_1024", "topics", "taxonomy_meta",
                    "taxonomy_centroids_384", "taxonomy_centroids_768", "taxonomy_centroids_1024",
                    "document_aspects", "document_highlights", "aspect_extraction_queue",
                    "relevance_log", "search_telemetry")) {
                assertThat(rows(su, "SELECT COUNT(*) FROM nexus." + tbl
                    + " WHERE tenant_id='" + TENANT_A + "' AND collection='" + OLD + "'"))
                    .as("no orphan in " + tbl + " under OLD").isZero();
            }
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("hook_failures orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chash_index WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + OLD + "'")).as("chash_index orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT_A
                + "' AND source_collection='" + OLD + "'")).as("assignment orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + OLD + "'")).as("catalog_documents orphans").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + OLD + "'")).as("old registry row gone").isZero();
            // Present under NEW.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + NEW + "'")).as("new registry row present").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("chunks under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("taxonomy_meta under NEW").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("hook_failures under NEW").isEqualTo(1);
            // Symmetric presence sweep for the remaining re-homed tables.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_highlights WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("document_highlights under NEW").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_aspects WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("document_aspects under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("aspect_extraction_queue under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids_384 WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("centroids_384 under NEW").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT_A
                + "' AND source_collection='" + NEW + "'")).as("topic_assignments under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chash_index WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + NEW + "'")).as("chash_index under NEW").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.relevance_log WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + NEW + "'")).as("relevance_log under NEW").isEqualTo(2);
        }
    }

    @Test @Order(30)
    void renameCollection_isTenantIsolated_tenantBUntouched() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_B
                + "' AND name='" + OLD + "'")).as("tenant B old registry intact").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_B
                + "' AND name='" + NEW + "'")).as("tenant B has no NEW row").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + OLD + "'")).as("tenant B chunks intact under OLD").isEqualTo(2);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_B
                + "' AND collection='" + OLD + "'")).as("tenant B taxonomy_meta intact").isEqualTo(1);
        }
    }

    @Test @Order(40)
    void renameCollection_roundTrip_newBackToOld() throws Exception {
        // Y -> X: tenant A currently lives under NEW; rename it back and confirm the inverse.
        Map<String, Integer> c = repo.renameCollection(TENANT_A, NEW, OLD);
        assertThat(c.get("catalog_collections_inserted")).as("registry X re-inserted").isEqualTo(1);
        assertThat(c.get("chunks_384")).as("chunks_384 back").isEqualTo(2);
        assertThat(c.get("catalog_collections_deleted")).as("registry Y deleted").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + NEW + "'")).as("NEW gone after round-trip").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + OLD + "'")).as("OLD restored").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("chunks restored under OLD").isEqualTo(2);
            // Back-direction must restore the derived tables too (not just chunks/registry).
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_meta WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("taxonomy_meta restored").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_highlights WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("document_highlights restored").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.taxonomy_centroids_384 WHERE tenant_id='" + TENANT_A
                + "' AND collection='" + OLD + "'")).as("centroids_384 restored").isEqualTo(1);
        }
    }

    @Test @Order(50)
    void renameCollection_crossModelCopyBranch_targetExists_repointsDocsOnly() throws Exception {
        // RDR-162 regression: pre-register the TARGET (simulating the bge-768 cross-model
        // chunk upsert), then rename. Only catalog_documents.physical_collection must repoint;
        // both registry rows must remain (the source is NOT deleted, the target NOT collided).
        final String src = "code__ren-xm__minilm-l6-v2-384__v1";
        final String tgt = "code__ren-xm__bge-768__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // source registry + a document pointing at it
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + src + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT_A + "', 'xm-doc-1', 'XM Doc', '" + src + "')");
            // target registry already exists (cross-model copy registered it)
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_A + "', '" + tgt + "')");
        }
        Map<String, Integer> c = repo.renameCollection(TENANT_A, src, tgt);
        assertThat(c).as("cross-model branch returns only catalog_documents").containsOnlyKeys("catalog_documents");
        assertThat(c.get("catalog_documents")).as("one doc repointed").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + src + "'")).as("source registry row KEPT").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_A
                + "' AND name='" + tgt + "'")).as("target registry row KEPT").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + tgt + "'")).as("doc now under target").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents WHERE tenant_id='" + TENANT_A
                + "' AND physical_collection='" + src + "'")).as("no doc left under source").isZero();
        }
    }

    @Test @Order(60)
    void renameCollection_midTransactionFailure_rollsBackEverything() throws Exception {
        // Atomicity regression (replaces the old FK-violation test): the whole re-home is
        // ONE withTenant transaction. Force a mid-sequence failure AFTER the registry INSERT
        // and child re-homes by colliding the search_telemetry PK (tenant_id, ts, query_hash,
        // collection): a row pre-seeded under NEW shares (ts, query_hash) with an OLD row, so
        // UPDATE ...SET collection=NEW collides -> the entire transaction must roll back,
        // leaving OLD fully intact and NEW absent. search_telemetry has no FK, so the NEW-side
        // row needs no NEW registry and does not trip the targetExists cross-model branch.
        final String old = "knowledge__ren-rb__minilm-l6-v2-384__v1";
        final String neu = "knowledge__ren-rb__minilm-l6-v2-384__v2";
        final String ts = "2026-01-01 00:00:00+00";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT_C + "', '" + old + "')");
            su.createStatement().execute(chunkInsert(TENANT_C, old, "chunks_384", 384, "rbchunk"));
            // OLD telemetry row that will try to move to (ts,'collide',NEW)...
            su.createStatement().execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
                + "VALUES ('" + TENANT_C + "', '" + ts + "', 'collide', '" + old + "', 1, 1)");
            // ...but that PK already exists under NEW -> UPDATE collision mid-transaction.
            su.createStatement().execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
                + "VALUES ('" + TENANT_C + "', '" + ts + "', 'collide', '" + neu + "', 9, 9)");
        }

        assertThatThrownBy(() -> repo.renameCollection(TENANT_C, old, neu))
            .as("mid-transaction PK collision propagates").isInstanceOf(Exception.class);

        try (Connection su = pg.createConnection("")) {
            // Everything rolled back: OLD intact, NEW registry never created.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_C
                + "' AND name='" + old + "'")).as("OLD registry intact after rollback").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_collections WHERE tenant_id='" + TENANT_C
                + "' AND name='" + neu + "'")).as("NEW registry NOT created (rolled back)").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='" + TENANT_C
                + "' AND collection='" + old + "'")).as("chunk NOT re-homed (rolled back)").isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.chunks_384 WHERE tenant_id='" + TENANT_C
                + "' AND collection='" + neu + "'")).as("no chunk under NEW").isZero();
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.search_telemetry WHERE tenant_id='" + TENANT_C
                + "' AND collection='" + old + "'")).as("OLD telemetry intact").isEqualTo(1);
        }
    }

    // ── fixture ──────────────────────────────────────────────────────────────

    /** Seed one full collection (all re-homed lifecycle tables) for {@code tenant}. Superuser; bypasses RLS. */
    private static void seedFullCollection(Connection su, String tenant, String coll) throws Exception {
        var st = su.createStatement();
        st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '" + coll + "')");
        st.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', 'Doc 1', '" + coll + "')");
        st.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', 0, '" + chash("rnman1") + "')");
        // chunks: 2/1/1
        st.execute(chunkInsert(tenant, coll, "chunks_384", 384, "rn384a"));
        st.execute(chunkInsert(tenant, coll, "chunks_384", 384, "rn384b"));
        st.execute(chunkInsert(tenant, coll, "chunks_768", 768, "rn768a"));
        st.execute(chunkInsert(tenant, coll, "chunks_1024", 1024, "rn1024a"));
        // chash_index: 2
        st.execute("INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
            + "VALUES ('" + tenant + "', '" + chash("rnci1") + "', '" + coll + "', NOW())");
        st.execute("INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
            + "VALUES ('" + tenant + "', '" + chash("rnci2") + "', '" + coll + "', NOW())");
        // topics: 1 (explicit id)
        long topicId = Math.abs((long) (tenant + coll).hashCode());
        st.execute("INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) "
            + "VALUES (" + topicId + ", '" + tenant + "', 'topic-rn', '" + coll + "', 0, NOW(), 'pending')");
        // taxonomy_meta: 1 (fk-003-4 RESTRICT)
        st.execute("INSERT INTO nexus.taxonomy_meta (tenant_id, collection) VALUES ('" + tenant + "', '" + coll + "')");
        // topic_assignments: 2 (source_collection=coll)
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', " + topicId + ", 'projection', '" + coll + "', NOW())");
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', 'rn-doc-2', " + topicId + ", 'projection', '" + coll + "', NOW())");
        // centroids: one per dim
        st.execute("INSERT INTO nexus.taxonomy_centroids_384 (tenant_id, collection, topic_id, embedding) "
            + "VALUES ('" + tenant + "', '" + coll + "', " + topicId + ", " + vec(384) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids_768 (tenant_id, collection, topic_id, embedding) "
            + "VALUES ('" + tenant + "', '" + coll + "', " + topicId + ", " + vec(768) + "::vector)");
        st.execute("INSERT INTO nexus.taxonomy_centroids_1024 (tenant_id, collection, topic_id, embedding) "
            + "VALUES ('" + tenant + "', '" + coll + "', " + topicId + ", " + vec(1024) + "::vector)");
        // document_aspects: 2 — one doc-rooted, one DOC-LESS
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/a1.md', NOW(), 'v1', 'docling', 'rn-doc-1')");
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/a2.md', NOW(), 'v1', 'docling', NULL)");
        // document_highlights: 1
        st.execute("INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, highlights_md, ingested_at) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', '" + coll + "', 'hi', NOW())");
        // aspect_extraction_queue: 2 — one doc-rooted, one DOC-LESS
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/q1.md', 'pending', NOW(), 'rn-doc-1')");
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + coll + "', '/p/q2.md', 'pending', NOW(), NULL)");
        // search_telemetry: 2 (no FK, but re-homed)
        st.execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
            + "VALUES ('" + tenant + "', NOW(), 'qh1', '" + coll + "', 10, 5)");
        st.execute("INSERT INTO nexus.search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count) "
            + "VALUES ('" + tenant + "', NOW(), 'qh2', '" + coll + "', 8, 4)");
        // hook_failures: 1 (no FK, but re-homed)
        st.execute("INSERT INTO nexus.hook_failures (tenant_id, doc_id, collection, hook_name, error, occurred_at) "
            + "VALUES ('" + tenant + "', 'rn-doc-1', '" + coll + "', 'post_store', 'boom', NOW())");
        // relevance_log: 2 (no FK, but re-homed — RDR-164 §Approach Phase 3 third audit table)
        st.execute("INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, collection, action, session_id, timestamp) "
            + "VALUES ('" + tenant + "', 'q1', 'ch1', '" + coll + "', 'click', 's1', NOW())");
        st.execute("INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, collection, action, session_id, timestamp) "
            + "VALUES ('" + tenant + "', 'q2', 'ch2', '" + coll + "', 'skip', 's1', NOW())");
    }

    private static String chunkInsert(String tenant, String coll, String table, int dim, String seed) {
        return "INSERT INTO nexus." + table + " (tenant_id, collection, chash, chunk_text, embedding) "
            + "VALUES ('" + tenant + "', '" + coll + "', '" + chash(seed) + "', 'text', " + vec(dim) + "::vector)";
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
