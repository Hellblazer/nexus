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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-164 P4 (bead nexus-jcx6w) — verify the document-level fk-001 ON DELETE CASCADE
 * fires in the service path, and document what it does NOT cover.
 *
 * <p>fk-001 (fk-001-catalog-cross-store.xml) makes four child tables cascade-delete when
 * their parent {@code catalog_documents(tenant_id, tumbler)} row is HARD-deleted:
 * {@code document_aspects}, {@code document_highlights}, {@code aspect_extraction_queue},
 * {@code catalog_document_chunks}. {@code topic_assignments} has NO document-rooted FK
 * (only a lookup index — its {@code doc_id} is a chunk content-hash, not a tumbler), so it
 * survives a document delete; its collection-scoped cleanup is the taxonomy cascade, not
 * fk-001. This isolates the FK behaviour from the explicit per-table deletes in
 * {@link CatalogDeleteCollectionCascadeTest}.
 *
 * <p>Three semantics are pinned: (1) a HARD {@code DELETE FROM catalog_documents} cascades to
 * the four FK children (the path {@code deleteCollection} takes); (2) the service's
 * {@code deleteDocument} API is a SOFT tombstone ({@code UPDATE deleted_at}) — it does NOT
 * fire {@code ON DELETE CASCADE}, so children intentionally survive a tombstone; (3) the
 * composite {@code (tenant_id, doc_id)} FK isolates tenants — deleting one tenant's document
 * never cascades another tenant's identically-named document's children.
 *
 * <p>KNOWN OPEN GAP (RDR-164 P4, NOT closed): because {@code topic_assignments} has no
 * document-rooted FK, a per-document HARD purge would leave its assignments orphaned. Today
 * the only hard-delete path is {@code deleteCollection}, which cleans {@code topic_assignments}
 * explicitly by {@code source_collection} (P2) — so no orphan accumulates in practice. But a
 * future per-document hard-purge path (e.g. trash-empty) MUST clean assignments explicitly;
 * fk-001 will not. Tracked as a follow-on bead (see RDR-164 P4 finding).
 *
 * <p>SCOPE NOTE (§Approach P4 bullet 2 — "retire redundant service-mode client cleanup"):
 * confirmed there is nothing to retire at the per-document level. {@code _WriteOps.delete_document}
 * is the LOCAL-mode SQLite manifest cascade (kept; no FK in sqlite); {@code HttpCatalogClient.
 * delete_document} is a bare POST that relies on the soft-delete endpoint. No service-mode client
 * fan-out exists here to remove. Collection-level client-orchestration retirement is P5's scope.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogDocumentCascadeTest {

    private static final String TENANT = "doc-casc";
    private static final String TENANT_B = "doc-casc-b";
    private static final String COLL = "knowledge__doc-casc__minilm-l6-v2-384__v1";
    private static final String SVC_ROLE = "svc_doc_casc";
    private static final String SVC_PASS = "svc_doc_casc_pass";

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
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(10)
    void softDelete_tombstone_doesNotCascade() throws Exception {
        // The service's deleteDocument API is a soft tombstone (UPDATE deleted_at). It must
        // NOT fire fk-001 ON DELETE CASCADE — the doc-rooted children survive the tombstone.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocument(su, TENANT, "soft-doc-1");
        }
        int n = repo.deleteDocument(TENANT, "soft-doc-1");
        assertThat(n).as("soft delete tombstoned one row").isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT deleted_at IS NOT NULL FROM nexus.catalog_documents WHERE tenant_id='"
                + TENANT + "' AND tumbler='soft-doc-1'")).as("row tombstoned, not removed").isEqualTo(1);
            // Children survive the tombstone (no cascade on UPDATE deleted_at).
            assertChildCounts(su, TENANT, "soft-doc-1", 1, 1, 1, 1);
        }
    }

    @Test @Order(20)
    void hardDelete_catalogDocument_cascadesFourChildren_topicAssignmentSurvives() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocument(su, TENANT, "hard-doc-1");
            // topic_assignment for the doc — it has NO document-rooted FK, so it must survive.
            seedTopicAssignment(su, TENANT, "hard-doc-1");
        }
        // Sanity: children present before the hard delete.
        try (Connection su = pg.createConnection("")) {
            assertChildCounts(su, TENANT, "hard-doc-1", 1, 1, 1, 1);
        }

        // HARD delete the catalog_documents row (the path deleteCollection takes); fk-001
        // cascades to the four doc-rooted children. FK checks run as table owner (bypass RLS),
        // so a superuser DELETE exercises the same constraint the svc-role delete would.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_documents WHERE tenant_id='" + TENANT + "' AND tumbler='hard-doc-1'");
            assertThat(n).as("one catalog_documents row hard-deleted").isEqualTo(1);
        }

        try (Connection su = pg.createConnection("")) {
            // The four fk-001 CASCADE children are gone.
            assertChildCounts(su, TENANT, "hard-doc-1", 0, 0, 0, 0);
            // topic_assignments has NO document-rooted FK (fk-001 changeset 1 = index only) — survives.
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.topic_assignments WHERE tenant_id='" + TENANT
                + "' AND doc_id='hard-doc-1'"))
                .as("topic_assignments NOT cascaded by document delete (no doc-rooted FK)").isEqualTo(1);
        }
    }

    @Test @Order(30)
    void hardDelete_compositeFkIsolatesTenants() throws Exception {
        // fk-001's headline property: the FK is composite (tenant_id, doc_id) → catalog_documents
        // (tenant_id, tumbler). Two tenants can share a tumbler value; deleting tenant A's document
        // must cascade ONLY tenant A's children — tenant B's identically-named document and its
        // children are untouched (the composite match requires the same tenant_id).
        final String shared = "xt-doc";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocument(su, TENANT, shared);
            seedDocument(su, TENANT_B, shared);
        }
        try (Connection su = pg.createConnection("")) {
            assertChildCounts(su, TENANT, shared, 1, 1, 1, 1);
            assertChildCounts(su, TENANT_B, shared, 1, 1, 1, 1);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_documents WHERE tenant_id='" + TENANT + "' AND tumbler='" + shared + "'");
        }

        try (Connection su = pg.createConnection("")) {
            assertChildCounts(su, TENANT, shared, 0, 0, 0, 0);       // tenant A cascaded
            assertChildCounts(su, TENANT_B, shared, 1, 1, 1, 1);     // tenant B untouched
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Assert the four fk-001 child tables hold the expected row counts for {@code tenant}/{@code tumbler}. */
    private void assertChildCounts(Connection su, String tenant, String tumbler,
                                   int aspects, int highlights, int queue, int manifest) throws Exception {
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_aspects WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("document_aspects").isEqualTo(aspects);
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.document_highlights WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("document_highlights").isEqualTo(highlights);
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.aspect_extraction_queue WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("aspect_extraction_queue").isEqualTo(queue);
        assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks WHERE tenant_id='" + tenant
            + "' AND doc_id='" + tumbler + "'")).as("catalog_document_chunks manifest").isEqualTo(manifest);
    }

    /** Seed one document + its four fk-001 child rows (aspects/highlights/queue/manifest) for {@code tenant}. */
    private static void seedDocument(Connection su, String tenant, String tumbler) throws Exception {
        var st = su.createStatement();
        // Register the collection first (document_aspects/queue carry an fk-003 collection FK).
        st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '" + COLL + "') "
            + "ON CONFLICT DO NOTHING");
        st.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 'Doc', '" + COLL + "')");
        st.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 0, '" + chash("man" + tenant + tumbler) + "')");
        st.execute("INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '/p/a-" + tenant + "-" + tumbler + ".md', NOW(), 'v1', 'docling', '" + tumbler + "')");
        st.execute("INSERT INTO nexus.document_highlights (tenant_id, doc_id, collection, highlights_md, ingested_at) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', '" + COLL + "', 'hi', NOW())");
        st.execute("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, status, enqueued_at, doc_id) "
            + "VALUES ('" + tenant + "', '" + COLL + "', '/p/q-" + tenant + "-" + tumbler + ".md', 'pending', NOW(), '" + tumbler + "')");
    }

    /** Seed a topic + a topic_assignment keyed to {@code docId} for {@code tenant}. */
    private static void seedTopicAssignment(Connection su, String tenant, String docId) throws Exception {
        var st = su.createStatement();
        st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '" + COLL + "') "
            + "ON CONFLICT DO NOTHING");
        // Mask to 32 bits before widening so Math.abs cannot overflow on Integer.MIN_VALUE.
        long topicId = (tenant + docId).hashCode() & 0xFFFFFFFFL;
        st.execute("INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) "
            + "VALUES (" + topicId + ", '" + tenant + "', 'topic-dc', '" + COLL + "', 0, NOW(), 'pending')");
        st.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
            + "VALUES ('" + tenant + "', '" + docId + "', " + topicId + ", 'projection', '" + COLL + "', NOW())");
    }

    private static String chash(String seed) {
        return (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
    }

    private static int rows(Connection su, String sql) throws Exception {
        var rs = su.createStatement().executeQuery(sql);
        rs.next();
        // boolean expressions (deleted_at IS NOT NULL) come back as t/f; coerce to 1/0.
        Object v = rs.getObject(1);
        if (v instanceof Boolean b) return b ? 1 : 0;
        return rs.getInt(1);
    }
}
