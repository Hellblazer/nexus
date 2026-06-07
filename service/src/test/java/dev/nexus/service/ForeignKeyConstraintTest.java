package dev.nexus.service;

import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-b7v6i — Cross-store composite FK constraint tests.
 *
 * <p>Verifies that the fk-001-catalog-cross-store changeset enforces referential integrity
 * with correct tenant isolation. Key properties tested:
 *
 * <ol>
 *   <li>REFERENTIAL INTEGRITY: inserting a taxonomy/aspect row whose doc-ref has no matching
 *       catalog_documents entry is rejected by the FK (PSQLException).</li>
 *   <li>ON DELETE CASCADE: deleting a catalog_documents row removes dependent
 *       topic_assignments, document_aspects, and document_highlights rows.</li>
 *   <li>ON DELETE CASCADE for queue: deleting a catalog_documents row removes dependent
 *       aspect_extraction_queue rows (stale queue items for a deleted doc have no purpose;
 *       null-doc-id queue items are unaffected). CASCADE chosen over SET NULL because PG14
 *       SET NULL on a composite FK nullifies ALL FK columns including the NOT NULL tenant_id.</li>
 *   <li>TENANT-CORRECTNESS (headline): a composite FK prevents cross-tenant references.
 *       Tenant-A's topic_assignment/aspect/highlight CANNOT reference a catalog_documents
 *       row that belongs to tenant-B, even when that tumbler exists — the composite
 *       (tenant_id, doc_id) FK rejects it. This proves the FK bypasses RLS correctly
 *       (checks table owner, not tenant GUC) but still enforces tenant scope via the
 *       composite key.</li>
 *   <li>NULLABLE CONVERSION: document_aspects.doc_id and aspect_extraction_queue.doc_id
 *       accept NULL (valid FK — no reference = no violation).</li>
 *   <li>RLS NEGATIVE: RLS tenant isolation still holds after FK addition; tenant A rows
 *       remain invisible to tenant B via GUC.</li>
 * </ol>
 *
 * <p>All tests use the superuser connection for direct SQL inserts (bypasses RLS) so we
 * can control exact tenant_id values and test cross-tenant FK rejection precisely.
 * RLS tests use a restricted svc role with tenant GUC set.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ForeignKeyConstraintTest {

    private static final String TENANT_A  = "fk-tenant-a";
    private static final String TENANT_B  = "fk-tenant-b";
    private static final String SVC_ROLE  = "svc_fk_test";
    private static final String SVC_PASS  = "svc_fk_test_pass";

    // Tumbler values
    private static final String TUMBLER_A = "1.1";
    private static final String TUMBLER_B = "2.1";

    EmbeddedPostgres pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        // Phase 1: create roles (autoCommit=true; CREATE ROLE cannot run in txn)
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        // Phase 2: apply full master changelog (includes fk-001-catalog-cross-store)
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to all relevant tables
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of(
                    "catalog_documents", "catalog_owners",
                    "catalog_links", "catalog_document_chunks", "catalog_collections", "catalog_meta",
                    "topics", "taxonomy_meta", "topic_assignments", "topic_links",
                    "document_aspects", "document_highlights",
                    "aspect_extraction_queue", "aspect_promotion_log")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.catalog_links_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.document_aspects_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.document_highlights_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.aspect_extraction_queue_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // HikariCP as svc role (non-superuser, subject to RLS)
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.close();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SCHEMA VERIFICATION
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(1)
    void fkChangeset_appliesCleanly_allFkConstraintsPresent() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            // Verify all 4 FK constraints exist in pg_constraint
            List<String> expectedFks = List.of(
                "fk_ta_catalog_doc",
                "fk_doc_aspects_catalog_doc",
                "fk_doc_highlights_catalog_doc",
                "fk_aspect_queue_catalog_doc",
                "fk_catalog_chunks_catalog_doc"
            );
            for (String fkName : expectedFks) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT 1 FROM pg_constraint c " +
                    "JOIN pg_namespace n ON n.oid = c.connamespace " +
                    "WHERE c.contype = 'f' " +
                    "  AND c.conname = '" + fkName + "' " +
                    "  AND n.nspname = 'nexus'");
                assertThat(rs.next()).as("FK constraint " + fkName + " must exist").isTrue();
            }
        }
    }

    @Test @Order(2)
    void docAspectsDocId_isNullable() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT is_nullable FROM information_schema.columns " +
                "WHERE table_schema='nexus' AND table_name='document_aspects' AND column_name='doc_id'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("is_nullable")).isEqualTo("YES");
        }
    }

    @Test @Order(3)
    void aspectQueueDocId_isNullable() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT is_nullable FROM information_schema.columns " +
                "WHERE table_schema='nexus' AND table_name='aspect_extraction_queue' AND column_name='doc_id'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("is_nullable")).isEqualTo("YES");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — topic_assignments
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void topicAssignment_validCatalogDoc_succeeds() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, TUMBLER_A);
            insertTopic(su, TENANT_A, 100L, "test-topic", "col-a");
            // Insert a topic_assignment referencing the catalog document
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments " +
                "(tenant_id, doc_id, topic_id, assigned_by, assigned_at) VALUES " +
                "('" + TENANT_A + "', '" + TUMBLER_A + "', 100, 'hdbscan', NOW())");
            // Verify it was inserted
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.topic_assignments " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='" + TUMBLER_A + "'");
            rs.next();
            assertThat(rs.getInt(1)).isEqualTo(1);
        }
    }

    @Test @Order(11)
    void topicAssignment_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertTopic(su, TENANT_A, 101L, "another-topic", "col-a");
            // Attempt to insert topic_assignment with a doc_id not in catalog_documents
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.topic_assignments " +
                    "(tenant_id, doc_id, topic_id, assigned_by, assigned_at) VALUES " +
                    "('" + TENANT_A + "', 'nonexistent-tumbler', 101, 'hdbscan', NOW())")
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(12)
    void deleteCatalogDoc_cascadesToTopicAssignments() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "1.99");
            insertTopic(su, TENANT_A, 199L, "cascade-topic", "col-a");
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments " +
                "(tenant_id, doc_id, topic_id, assigned_by, assigned_at) VALUES " +
                "('" + TENANT_A + "', '1.99', 199, 'hdbscan', NOW())");

            // Verify assignment exists
            ResultSet before = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.topic_assignments " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='1.99'");
            before.next();
            assertThat(before.getInt(1)).isEqualTo(1);

            // Delete the catalog document
            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents " +
                "WHERE tenant_id='" + TENANT_A + "' AND tumbler='1.99'");

            // Assignment must be gone (ON DELETE CASCADE)
            ResultSet after = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.topic_assignments " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='1.99'");
            after.next();
            assertThat(after.getInt(1)).as("Cascade delete must remove topic_assignments").isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — document_aspects
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void aspect_nullDocId_isAccepted() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            // doc_id=NULL is valid for a FK — no catalog reference required
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects " +
                "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name) VALUES " +
                "('" + TENANT_A + "', 'knowledge__a', 'path/null-doc.pdf', NOW(), 'v1', 'docling')");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT doc_id FROM nexus.document_aspects " +
                "WHERE tenant_id='" + TENANT_A + "' AND source_path='path/null-doc.pdf'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("doc_id")).isNull();
        }
    }

    @Test @Order(21)
    void aspect_validDocId_succeeds() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "3.1");
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects " +
                "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) VALUES " +
                "('" + TENANT_A + "', 'knowledge__b', 'path/valid-doc.pdf', NOW(), 'v1', 'docling', '3.1')");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT doc_id FROM nexus.document_aspects " +
                "WHERE tenant_id='" + TENANT_A + "' AND source_path='path/valid-doc.pdf'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("doc_id")).isEqualTo("3.1");
        }
    }

    @Test @Order(22)
    void aspect_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_aspects " +
                    "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) VALUES " +
                    "('" + TENANT_A + "', 'knowledge__c', 'path/orphan.pdf', NOW(), 'v1', 'docling', 'no-such-tumbler')")
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(23)
    void deleteCatalogDoc_cascadesToAspects() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "3.99");
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects " +
                "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) VALUES " +
                "('" + TENANT_A + "', 'knowledge__d', 'path/cascade-aspect.pdf', NOW(), 'v1', 'docling', '3.99')");

            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents " +
                "WHERE tenant_id='" + TENANT_A + "' AND tumbler='3.99'");

            ResultSet after = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.document_aspects " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='3.99'");
            after.next();
            assertThat(after.getInt(1)).as("Cascade delete must remove document_aspects").isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — document_highlights
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void highlight_validDocId_succeeds() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "4.1");
            su.createStatement().execute(
                "INSERT INTO nexus.document_highlights " +
                "(tenant_id, doc_id, ingested_at) VALUES " +
                "('" + TENANT_A + "', '4.1', NOW())");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.document_highlights " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='4.1'");
            rs.next();
            assertThat(rs.getInt(1)).isEqualTo(1);
        }
    }

    @Test @Order(31)
    void highlight_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_highlights " +
                    "(tenant_id, doc_id, ingested_at) VALUES " +
                    "('" + TENANT_A + "', 'no-such-tumbler-hl', NOW())")
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(32)
    void deleteCatalogDoc_cascadesToHighlights() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "4.99");
            su.createStatement().execute(
                "INSERT INTO nexus.document_highlights " +
                "(tenant_id, doc_id, ingested_at) VALUES " +
                "('" + TENANT_A + "', '4.99', NOW())");

            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents " +
                "WHERE tenant_id='" + TENANT_A + "' AND tumbler='4.99'");

            ResultSet after = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.document_highlights " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='4.99'");
            after.next();
            assertThat(after.getInt(1)).as("Cascade delete must remove document_highlights").isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — aspect_extraction_queue
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void queue_nullDocId_isAccepted() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.aspect_extraction_queue " +
                "(tenant_id, collection, source_path, enqueued_at) VALUES " +
                "('" + TENANT_A + "', 'knowledge__q', 'path/queue-null.pdf', NOW())");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT doc_id FROM nexus.aspect_extraction_queue " +
                "WHERE tenant_id='" + TENANT_A + "' AND source_path='path/queue-null.pdf'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("doc_id")).isNull();
        }
    }

    @Test @Order(41)
    void queue_validDocId_succeeds() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "5.1");
            su.createStatement().execute(
                "INSERT INTO nexus.aspect_extraction_queue " +
                "(tenant_id, collection, source_path, doc_id, enqueued_at) VALUES " +
                "('" + TENANT_A + "', 'knowledge__q', 'path/queue-valid.pdf', '5.1', NOW())");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT doc_id FROM nexus.aspect_extraction_queue " +
                "WHERE tenant_id='" + TENANT_A + "' AND source_path='path/queue-valid.pdf'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("doc_id")).isEqualTo("5.1");
        }
    }

    @Test @Order(42)
    void queue_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.aspect_extraction_queue " +
                    "(tenant_id, collection, source_path, doc_id, enqueued_at) VALUES " +
                    "('" + TENANT_A + "', 'knowledge__q', 'path/queue-orphan.pdf', 'no-such-tumbler-q', NOW())")
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(43)
    void deleteCatalogDoc_cascadesToQueue() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "5.99");
            su.createStatement().execute(
                "INSERT INTO nexus.aspect_extraction_queue " +
                "(tenant_id, collection, source_path, doc_id, enqueued_at) VALUES " +
                "('" + TENANT_A + "', 'knowledge__q', 'path/queue-cascade.pdf', '5.99', NOW())");

            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents " +
                "WHERE tenant_id='" + TENANT_A + "' AND tumbler='5.99'");

            // Queue item must be deleted (ON DELETE CASCADE — stale queue for a deleted doc is moot)
            ResultSet after = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.aspect_extraction_queue " +
                "WHERE tenant_id='" + TENANT_A + "' AND source_path='path/queue-cascade.pdf'");
            after.next();
            assertThat(after.getInt(1))
                .as("Cascade delete must remove queue item for deleted catalog doc").isZero();
        }
    }

    @Test @Order(44)
    void queue_nullDocId_survivesDocDeletion() throws Exception {
        // A queue item with doc_id=NULL is not bound to any catalog doc;
        // deleting catalog docs must not affect it.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "6.1");
            su.createStatement().execute(
                "INSERT INTO nexus.aspect_extraction_queue " +
                "(tenant_id, collection, source_path, enqueued_at) VALUES " +
                "('" + TENANT_A + "', 'knowledge__qq', 'path/queue-unbound.pdf', NOW())");

            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents " +
                "WHERE tenant_id='" + TENANT_A + "' AND tumbler='6.1'");

            // Unbound queue item (doc_id=NULL) must survive
            ResultSet after = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.aspect_extraction_queue " +
                "WHERE tenant_id='" + TENANT_A + "' AND source_path='path/queue-unbound.pdf'");
            after.next();
            assertThat(after.getInt(1))
                .as("Unbound queue item (doc_id=NULL) must not be affected by doc deletion").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // TENANT-CORRECTNESS — the headline property
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * CRITICAL: proves that the composite FK (tenant_id, doc_id) → catalog_documents(tenant_id, tumbler)
     * prevents cross-tenant references WITHOUT relying on RLS.
     *
     * Scenario:
     *   - Tenant-A has catalog_documents row (tenant_id='fk-tenant-a', tumbler='1.1').
     *   - Tenant-B tries to insert topic_assignment (tenant_id='fk-tenant-b', doc_id='1.1').
     *   - The FK checks catalog_documents(tenant_id='fk-tenant-b', tumbler='1.1') — NOT FOUND.
     *   - Result: PSQLException (FK violation), not silent success.
     *
     * This test is distinct from RLS: it uses the superuser connection (bypasses RLS) to
     * prove the FK itself enforces tenant scope even when RLS is circumvented.
     */
    @Test @Order(50)
    void crossTenantTopicAssignment_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            // Tenant-A has a catalog document with tumbler TUMBLER_A
            insertCatalogDocument(su, TENANT_A, TUMBLER_A); // idempotent; may already exist from @Order(10)

            // Tenant-B inserts a topic (needed for topic_assignments FK)
            insertTopic(su, TENANT_B, 200L, "cross-tenant-topic", "col-b");

            // Tenant-B tries to reference Tenant-A's document tumbler
            // The composite FK (tenant_id='fk-tenant-b', doc_id='1.1') must check
            // catalog_documents(tenant_id='fk-tenant-b', tumbler='1.1') — which does NOT exist.
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.topic_assignments " +
                    "(tenant_id, doc_id, topic_id, assigned_by, assigned_at) VALUES " +
                    "('" + TENANT_B + "', '" + TUMBLER_A + "', 200, 'hdbscan', NOW())")
            );
            assertThat(ex.getMessage())
                .as("FK must reject cross-tenant reference even via superuser connection")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(51)
    void crossTenantAspect_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            // Tenant-A has a catalog document; Tenant-B tries to reference it
            insertCatalogDocument(su, TENANT_A, TUMBLER_A);

            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_aspects " +
                    "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) VALUES " +
                    "('" + TENANT_B + "', 'knowledge__x', 'path/cross-tenant.pdf', NOW(), 'v1', 'docling', '" + TUMBLER_A + "')")
            );
            assertThat(ex.getMessage())
                .as("FK must reject cross-tenant aspect reference")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(52)
    void crossTenantHighlight_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, TUMBLER_A);

            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.document_highlights " +
                    "(tenant_id, doc_id, ingested_at) VALUES " +
                    "('" + TENANT_B + "', '" + TUMBLER_A + "', NOW())")
            );
            assertThat(ex.getMessage())
                .as("FK must reject cross-tenant highlight reference")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(53)
    void crossTenantQueueItem_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, TUMBLER_A);

            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.aspect_extraction_queue " +
                    "(tenant_id, collection, source_path, doc_id, enqueued_at) VALUES " +
                    "('" + TENANT_B + "', 'knowledge__y', 'path/ct-queue.pdf', '" + TUMBLER_A + "', NOW())")
            );
            assertThat(ex.getMessage())
                .as("FK must reject cross-tenant queue reference")
                .containsIgnoringCase("foreign key");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — catalog_document_chunks (RDR-108 manifest)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void chunkManifest_validDocId_succeeds() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "chunk-doc-1");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks " +
                "(tenant_id, doc_id, position, chash) VALUES " +
                "('" + TENANT_A + "', 'chunk-doc-1', 0, 'abc123')");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='chunk-doc-1'");
            rs.next();
            assertThat(rs.getInt(1)).as("Chunk manifest row must be inserted").isEqualTo(1);
        }
    }

    @Test @Order(71)
    void chunkManifest_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks " +
                    "(tenant_id, doc_id, position, chash) VALUES " +
                    "('" + TENANT_A + "', 'nonexistent-chunk-doc', 0, 'deadbeef')")
            );
            assertThat(ex.getMessage())
                .as("FK must reject chunk row with no matching catalog_documents entry")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(72)
    void deleteCatalogDoc_cascadesToChunkManifest() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "chunk-cascade-doc");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks " +
                "(tenant_id, doc_id, position, chash) VALUES " +
                "('" + TENANT_A + "', 'chunk-cascade-doc', 0, 'hash0'), " +
                "('" + TENANT_A + "', 'chunk-cascade-doc', 1, 'hash1')");

            ResultSet before = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='chunk-cascade-doc'");
            before.next();
            assertThat(before.getInt(1)).isEqualTo(2);

            su.createStatement().execute(
                "DELETE FROM nexus.catalog_documents " +
                "WHERE tenant_id='" + TENANT_A + "' AND tumbler='chunk-cascade-doc'");

            ResultSet after = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id='" + TENANT_A + "' AND doc_id='chunk-cascade-doc'");
            after.next();
            assertThat(after.getInt(1))
                .as("ON DELETE CASCADE must remove chunk manifest rows")
                .isZero();
        }
    }

    @Test @Order(73)
    void crossTenantChunkManifest_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            // Seed catalog_documents for TENANT_A only
            insertCatalogDocument(su, TENANT_A, TUMBLER_A);
            // Insert chunk row for TENANT_B referencing TENANT_A's tumbler — must be rejected
            // (FK checks as table owner; without composite key this would silently succeed)
            Exception ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks " +
                    "(tenant_id, doc_id, position, chash) VALUES " +
                    "('" + TENANT_B + "', '" + TUMBLER_A + "', 0, 'crosshash')")
            );
            assertThat(ex.getMessage())
                .as("Composite FK must reject cross-tenant chunk manifest reference")
                .containsIgnoringCase("foreign key");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // RLS NEGATIVE — isolation still holds after FK addition
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Verifies that RLS tenant isolation is not weakened by the FK addition.
     * Tenant-A's catalog_documents rows are invisible to a svc_role connection
     * with GUC set to Tenant-B.
     */
    @Test @Order(60)
    void rlsIsolation_tenantA_invisibleToTenantB() throws Exception {
        // Insert a catalog doc for TENANT_A via superuser
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "rls-check-tumbler");
        }

        // Query via svc role with Tenant-B GUC — must see 0 rows
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_B + "', true)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_documents " +
                "WHERE tumbler='rls-check-tumbler'");
            rs.next();
            assertThat(rs.getInt(1))
                .as("Tenant-B must not see Tenant-A catalog_documents after FK addition")
                .isZero();
        }
    }

    @Test @Order(61)
    void rlsIsolation_topicAssignment_tenantA_invisibleToTenantB() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "rls-ta-tumbler");
            insertTopic(su, TENANT_A, 300L, "rls-topic", "col-rls");
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments " +
                "(tenant_id, doc_id, topic_id, assigned_by, assigned_at) VALUES " +
                "('" + TENANT_A + "', 'rls-ta-tumbler', 300, 'hdbscan', NOW())");
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_B + "', true)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.topic_assignments WHERE doc_id='rls-ta-tumbler'");
            rs.next();
            assertThat(rs.getInt(1))
                .as("Tenant-B must not see Tenant-A topic_assignments after FK addition")
                .isZero();
        }
    }

    @Test @Order(62)
    void rlsIsolation_documentAspects_tenantA_invisibleToTenantB() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, "rls-asp-tumbler");
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects " +
                "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) VALUES " +
                "('" + TENANT_A + "', 'knowledge__rls', 'path/rls-aspect.pdf', NOW(), 'v1', 'docling', 'rls-asp-tumbler')");
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_B + "', true)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.document_aspects WHERE doc_id='rls-asp-tumbler'");
            rs.next();
            assertThat(rs.getInt(1))
                .as("Tenant-B must not see Tenant-A document_aspects after FK addition")
                .isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Insert a minimal catalog_documents row. Uses ON CONFLICT DO NOTHING for idempotency
     * (tests at various @Order values may insert the same tumbler).
     */
    private static void insertCatalogDocument(Connection su, String tenantId, String tumbler)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "(tenant_id, tumbler, title) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    /**
     * Insert a topics row for use in topic_assignments FK tests.
     * Uses ON CONFLICT DO NOTHING (id is BIGSERIAL; here we supply explicit IDs to avoid
     * sequence issues across tests — tests use non-overlapping ids via @Order convention).
     */
    private static void insertTopic(Connection su, String tenantId, long id, String label, String collection)
            throws Exception {
        // Insert by id using the sequence; supply literal id via nextval override
        su.createStatement().execute(
            "INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) " +
            "VALUES (" + id + ", '" + tenantId + "', '" + label + "', '" + collection + "', 0, NOW(), 'pending') " +
            "ON CONFLICT (id) DO NOTHING");
    }
}
