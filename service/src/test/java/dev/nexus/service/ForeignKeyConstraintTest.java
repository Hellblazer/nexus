package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.*;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.postgresql.util.PSQLException;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
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

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Phase 1: create roles (autoCommit=true; CREATE ROLE cannot run in txn)
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        // Phase 3.5 (RDR-164 P1a): register the collections these fixtures reference so the
        // new fk-003 collection FKs on document_aspects / aspect_extraction_queue / topics
        // are satisfied. These tests exercise the fk-001 document-rooted FKs, not collection
        // registration, so the collections are pre-seeded here rather than per-insert.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String[][] seeds = {
                {TENANT_A, "knowledge__a"}, {TENANT_A, "knowledge__b"}, {TENANT_A, "knowledge__c"},
                {TENANT_A, "knowledge__d"}, {TENANT_A, "knowledge__q"}, {TENANT_A, "knowledge__qq"},
                {TENANT_A, "knowledge__rls"}, {TENANT_A, "col-a"}, {TENANT_A, "col-rls"},
                {TENANT_B, "knowledge__x"}, {TENANT_B, "knowledge__y"},
            };
            for (String[] s : seeds) {
                PgContainerHelper.insertCollection(ctx, s[0], s[1]);
            }
        }

        // HikariCP as svc role (non-superuser, subject to RLS)
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SCHEMA VERIFICATION
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(1)
    void fkChangeset_appliesCleanly_allFkConstraintsPresent() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // Verify the 4 cross-store FK constraints exist in pg_constraint.
            // topic_assignments is intentionally EXCLUDED (nexus-sa14p): its doc_id is a
            // chunk chash, not a document tumbler, so no fk_ta_catalog_doc is registered.
            List<String> expectedFks = List.of(
                "fk_doc_aspects_catalog_doc",
                "fk_doc_highlights_catalog_doc",
                "fk_aspect_queue_catalog_doc",
                "fk_catalog_chunks_catalog_doc"
            );
            // And assert fk_ta_catalog_doc does NOT exist.
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.foreignKey(ctx, "nexus", "fk_ta_catalog_doc"))
                .as("fk_ta_catalog_doc must NOT exist (nexus-sa14p)").isNull();
            for (String fkName : expectedFks) {
                assertThat(PgCatalogProbes.foreignKey(ctx, "nexus", fkName))
                    .as("FK constraint " + fkName + " must exist").isNotNull();
            }
        }
    }

    /**
     * nexus-msz9i: {@code PgVectorRepository#liveChunksPredicate} (and its jOOQ twin
     * {@code liveChunksCondition}) express chunk liveness in the DEAD-SET form
     * {@code NOT EXISTS(dead parent AND no live parent)}. That form is equivalent to the
     * older {@code NOT EXISTS(any manifest) OR EXISTS(live manifest)} form on every
     * REACHABLE input, but the two DISAGREE on one unreachable input: a
     * {@code catalog_document_chunks} row whose {@code doc_id} has no
     * {@code catalog_documents} row (old form => chunk DEAD, dead-set form => chunk LIVE).
     *
     * <p>What makes that input unreachable is precisely this constraint being ENFORCED and
     * VALIDATED — fk-001-5 DELETEs pre-existing orphans and then adds the FK with no
     * {@code NOT VALID}, so neither existing nor future rows can dangle. A merely-present
     * constraint is NOT enough: a {@code NOT VALID} FK would leave pre-existing orphan rows
     * in place and silently flip those chunks from filtered to visible.
     *
     * <p>{@code convalidated} is therefore a correctness dependency of the tombstone read
     * filter, not a schema nicety. If this assertion ever fails, revisit the predicate
     * before relaxing the constraint.
     */
    @Test @Order(1)
    void manifestFk_isValidated_liveChunksPredicateDependsOnIt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgCatalogProbes.Constraint rs = PgCatalogProbes.foreignKey(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "fk_catalog_chunks_catalog_doc");
            assertThat(rs)
                .as("fk_catalog_chunks_catalog_doc must exist (liveChunksPredicate depends on it)")
                .isNotNull();
            assertThat(rs.convalidated())
                .as("fk_catalog_chunks_catalog_doc must be VALIDATED — nexus-msz9i's dead-set "
                    + "liveChunksPredicate is only equivalent to the old form because dangling "
                    + "manifest rows are impossible; a NOT VALID FK reintroduces them")
                .isTrue();
        }
    }

    @Test @Order(2)
    void docAspectsDocId_isNotNullable() throws Exception {
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): document_aspects.doc_id
        // reverses fk-001-2's nullable conversion -- it is NOT NULL again. Assert
        // the constraint genuinely rejects a fresh NULL insert, not merely that
        // information_schema reports it (the pattern Hygiene001NotNullMigrationRlsTest
        // already uses for the same column). source_uri is supplied (also NOT NULL
        // now, and ordered ahead of doc_id in the table definition, so an omitted
        // source_uri would trip its own NOT NULL check first) so ONLY doc_id fires.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_A, "knowledge__a", "path/not-nullable-doc-id.pdf", OffsetDateTime.now(), "v1",
                        "docling", "file:///path/not-nullable-doc-id.pdf")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("document_aspects.doc_id must be NOT NULL (hygiene-001 step 1)")
                .containsIgnoringCase("null value in column \"doc_id\"");
        }
    }

    @Test @Order(3)
    void aspectQueueDocId_isNotNullable() throws Exception {
        // hygiene-001 step 2: aspect_extraction_queue.doc_id reverses fk-001-4's
        // nullable conversion -- it is NOT NULL again.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT)
                    .values(TENANT_A, "knowledge__q", "path/not-nullable-queue.pdf", OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("aspect_extraction_queue.doc_id must be NOT NULL (hygiene-001 step 2)")
                .containsIgnoringCase("null value in column \"doc_id\"");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — topic_assignments
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void topicAssignment_chashDocId_succeeds_noCatalogFk() throws Exception {
        // nexus-sa14p: doc_id is a chunk chash, not a catalog tumbler, and there is no
        // fk_ta_catalog_doc. A chash doc_id with no matching catalog_documents row imports.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            insertTopic(ctx, TENANT_A, 100L, "test-topic", "col-a");
            String chash = hexChash("fk-topicAssignment-chashDocId"); // 64-hex chunk chash, not a tumbler
            // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires
            // a matching nexus.chunks row for this INSERT to succeed at all.
            byte[] chashBytes = HexFormat.of().parseHex(chash);
            seedChunk(ctx, TENANT_A, "col-a", chashBytes);
            // Genuine hex-decoded bytes (RDR-194 P3d): doc_id is bytea now (P3c) -- a bare
            // string literal would store the ASCII bytes of the hex STRING via
            // Postgres's bytea "escape format" input, not the 32-byte digest
            // decode() produces, so it would never match seedChunk's chunks row.
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                .values(TENANT_A, chashBytes, 100L, "hdbscan", "col-a", OffsetDateTime.now())
                .execute();
            int count = ctx.selectCount().from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT_A)).and(TOPIC_ASSIGNMENTS.DOC_ID.eq(chashBytes))
                .fetchOne(0, int.class);
            assertThat(count).isEqualTo(1);
        }
    }

    @Test @Order(11)
    void topicAssignment_topicIdFk_stillEnforced() throws Exception {
        // The topic_id -> topics(id) FK IS correct and remains enforced (only the
        // catalog doc_id FK was removed). An assignment to a non-existent topic rejects.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                        TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                    .values(TENANT_A, HexFormat.of().parseHex(hexChash("fk-topicAssignment-topicIdFk")), 999999L,
                        "hdbscan", "col-a", OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(12)
    void deleteCatalogDoc_doesNotAffectTopicAssignments() throws Exception {
        // nexus-sa14p: with no catalog FK, deleting a catalog document does NOT cascade
        // to topic_assignments (assignments are chunk-keyed and independent of the catalog).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "1.99");
            insertTopic(ctx, TENANT_A, 199L, "no-cascade-topic", "col-a");
            byte[] chashBytes = HexFormat.of().parseHex(
                hexChash("fk-deleteCatalogDoc-doesNotAffectTopicAssignments"));
            // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires
            // a matching nexus.chunks row for this INSERT to succeed at all. The
            // chunk is untouched by the catalog_documents DELETE below, so it
            // remains a valid FK parent throughout -- consistent with what this
            // test proves (the assignment survives independently of the catalog).
            seedChunk(ctx, TENANT_A, "col-a", chashBytes);
            // Genuine hex-decoded bytes (RDR-194 P3d): see the sibling test's identical note.
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                .values(TENANT_A, chashBytes, 199L, "hdbscan", "col-a", OffsetDateTime.now())
                .execute();

            ctx.deleteFrom(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.99"))
                .execute();

            // Assignment must SURVIVE (no cascade — chash doc_id is independent of catalog).
            int count = ctx.selectCount().from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT_A)).and(TOPIC_ASSIGNMENTS.DOC_ID.eq(chashBytes))
                .fetchOne(0, int.class);
            assertThat(count).as("assignment must survive catalog-doc delete").isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — document_aspects
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void aspect_nullDocId_isRejected() throws Exception {
        // hygiene-001 step 1: doc_id=NULL is no longer valid -- the column is
        // NOT NULL again, reversing fk-001-2's nullable conversion. source_uri
        // is supplied (also NOT NULL now, ordered ahead of doc_id in the table
        // definition) so ONLY doc_id's NOT NULL check fires.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_A, "knowledge__a", "path/null-doc.pdf", OffsetDateTime.now(), "v1", "docling",
                        "file:///path/null-doc.pdf")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("document_aspects.doc_id must be NOT NULL (hygiene-001 step 1)")
                .containsIgnoringCase("null value in column \"doc_id\"");
        }
    }

    @Test @Order(21)
    void aspect_validDocId_succeeds() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "3.1");
            // hygiene-001 step 1: source_uri is NOT NULL now too, alongside doc_id.
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_A, "knowledge__b", "path/valid-doc.pdf", OffsetDateTime.now(), "v1", "docling", "3.1",
                    "file:///path/valid-doc.pdf")
                .execute();
            String docId = ctx.select(DOCUMENT_ASPECTS.DOC_ID).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT_A))
                .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("path/valid-doc.pdf"))
                .fetchOne(DOCUMENT_ASPECTS.DOC_ID);
            assertThat(docId).isEqualTo("3.1");
        }
    }

    @Test @Order(22)
    void aspect_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // hygiene-001 step 1: source_uri is NOT NULL now -- supply it so the
            // NOT NULL check doesn't fire ahead of the FK check under test.
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_A, "knowledge__c", "path/orphan.pdf", OffsetDateTime.now(), "v1", "docling",
                        "no-such-tumbler", "file:///path/orphan.pdf")
                    .execute()
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(23)
    void deleteCatalogDoc_cascadesToAspects() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "3.99");
            // hygiene-001 step 1: source_uri is NOT NULL now, alongside doc_id.
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_A, "knowledge__d", "path/cascade-aspect.pdf", OffsetDateTime.now(), "v1", "docling",
                    "3.99", "file:///path/cascade-aspect.pdf")
                .execute();

            ctx.deleteFrom(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("3.99"))
                .execute();

            int count = ctx.selectCount().from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT_A)).and(DOCUMENT_ASPECTS.DOC_ID.eq("3.99"))
                .fetchOne(0, int.class);
            assertThat(count).as("Cascade delete must remove document_aspects").isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — document_highlights
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void highlight_validDocId_succeeds() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "4.1");
            // hygiene-001 step 3: source_uri and collection are NOT NULL now;
            // 'knowledge__b' is pre-registered for TENANT_A in startAll() Phase 3.5.
            ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(TENANT_A, "4.1", "file:///4.1", "knowledge__b", OffsetDateTime.now())
                .execute();
            int count = ctx.selectCount().from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT_A)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("4.1"))
                .fetchOne(0, int.class);
            assertThat(count).isEqualTo(1);
        }
    }

    @Test @Order(31)
    void highlight_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // hygiene-001 step 3: source_uri/collection are NOT NULL now -- supply
            // them so that check doesn't fire ahead of the doc-id FK check under test.
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                        DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                    .values(TENANT_A, "no-such-tumbler-hl", "file:///no-such-tumbler-hl", "knowledge__c",
                        OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(32)
    void deleteCatalogDoc_cascadesToHighlights() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "4.99");
            // hygiene-001 step 3: source_uri/collection are NOT NULL now.
            ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(TENANT_A, "4.99", "file:///4.99", "knowledge__d", OffsetDateTime.now())
                .execute();

            ctx.deleteFrom(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("4.99"))
                .execute();

            int count = ctx.selectCount().from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT_A)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("4.99"))
                .fetchOne(0, int.class);
            assertThat(count).as("Cascade delete must remove document_highlights").isZero();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // REFERENTIAL INTEGRITY — aspect_extraction_queue
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void queue_nullDocId_isRejected() throws Exception {
        // hygiene-001 step 2: aspect_extraction_queue.doc_id is NOT NULL again --
        // reverses fk-001-4's nullable conversion.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT)
                    .values(TENANT_A, "knowledge__q", "path/queue-null.pdf", OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("aspect_extraction_queue.doc_id must be NOT NULL (hygiene-001 step 2)")
                .containsIgnoringCase("null value in column \"doc_id\"");
        }
    }

    @Test @Order(41)
    void queue_validDocId_succeeds() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "5.1");
            ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT)
                .values(TENANT_A, "knowledge__q", "path/queue-valid.pdf", "5.1", OffsetDateTime.now())
                .execute();
            String docId = ctx.select(ASPECT_EXTRACTION_QUEUE.DOC_ID).from(ASPECT_EXTRACTION_QUEUE)
                .where(ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(TENANT_A))
                .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq("path/queue-valid.pdf"))
                .fetchOne(ASPECT_EXTRACTION_QUEUE.DOC_ID);
            assertThat(docId).isEqualTo("5.1");
        }
    }

    @Test @Order(42)
    void queue_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.DOC_ID, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT)
                    .values(TENANT_A, "knowledge__q", "path/queue-orphan.pdf", "no-such-tumbler-q",
                        OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage()).containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(43)
    void deleteCatalogDoc_cascadesToQueue() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "5.99");
            ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.DOC_ID, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT)
                .values(TENANT_A, "knowledge__q", "path/queue-cascade.pdf", "5.99", OffsetDateTime.now())
                .execute();

            ctx.deleteFrom(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("5.99"))
                .execute();

            // Queue item must be deleted (ON DELETE CASCADE — stale queue for a deleted doc is moot)
            int count = ctx.selectCount().from(ASPECT_EXTRACTION_QUEUE)
                .where(ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(TENANT_A))
                .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq("path/queue-cascade.pdf"))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("Cascade delete must remove queue item for deleted catalog doc").isZero();
        }
    }

    // queue_nullDocId_survivesDocDeletion DELETED (hygiene-001 step 2,
    // nexus-tk070.p6a follow-on): its subject -- a queue row unbound to any
    // catalog doc (doc_id=NULL) surviving an unrelated doc deletion -- is no
    // longer representable now that aspect_extraction_queue.doc_id is NOT
    // NULL; an inverted "insert with NULL doc_id is rejected" assertion would
    // only duplicate queue_nullDocId_isRejected above.

    // ══════════════════════════════════════════════════════════════════════════
    // TENANT-CORRECTNESS — the headline property
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * CRITICAL: proves that the composite FK (tenant_id, doc_id) → catalog_documents(tenant_id, tumbler)
     * prevents cross-tenant references WITHOUT relying on RLS.
     *
     * Proven below for document_aspects (@Order(51)). NOT applicable to topic_assignments:
     * its doc_id is a chunk chash with no catalog FK (nexus-sa14p), so cross-tenant
     * catalog references do not apply there — tenant isolation for assignments is RLS-only.
     */
    @Test @Order(51)
    void crossTenantAspect_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Tenant-A has a catalog document; Tenant-B tries to reference it
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, TUMBLER_A);

            // hygiene-001 step 1: source_uri is NOT NULL now -- supply it so that
            // check doesn't fire ahead of the composite FK check under test.
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_B, "knowledge__x", "path/cross-tenant.pdf", OffsetDateTime.now(), "v1", "docling",
                        TUMBLER_A, "file:///path/cross-tenant.pdf")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("FK must reject cross-tenant aspect reference")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(52)
    void crossTenantHighlight_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, TUMBLER_A);

            // hygiene-001 step 3: source_uri/collection are NOT NULL now --
            // 'knowledge__x' is pre-registered for TENANT_B in Phase 3.5.
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                        DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                    .values(TENANT_B, TUMBLER_A, "file:///cross-tenant-hl", "knowledge__x", OffsetDateTime.now())
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("FK must reject cross-tenant highlight reference")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(53)
    void crossTenantQueueItem_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, TUMBLER_A);

            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                        ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                        ASPECT_EXTRACTION_QUEUE.DOC_ID, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT)
                    .values(TENANT_B, "knowledge__y", "path/ct-queue.pdf", TUMBLER_A, OffsetDateTime.now())
                    .execute()
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "chunk-doc-1");
            PgContainerHelper.insertCollection(ctx, TENANT_A, "fk-chunk-coll");
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
            // a matching nexus.chunks row for this CONTROL insert to succeed.
            byte[] chash = chashAscii("abc123abc123abc123abc123abc12300");
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                .values(TENANT_A, "fk-chunk-coll", chash, "text", vector(384))
                .onConflictDoNothing()
                .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .values(TENANT_A, "chunk-doc-1", 0, chash, "fk-chunk-coll")
                .execute();
            int count = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("chunk-doc-1"))
                .fetchOne(0, int.class);
            assertThat(count).as("Chunk manifest row must be inserted").isEqualTo(1);
        }
    }

    @Test @Order(71)
    void chunkManifest_orphanDocId_rejectsWithFKViolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "fk-chunk-coll");
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT_A, "nonexistent-chunk-doc", 0, chashAscii("deadbeefdeadbeefdeadbeefdeadbeef"),
                        "fk-chunk-coll")
                    .execute()
            );
            assertThat(ex.getMessage())
                .as("FK must reject chunk row with no matching catalog_documents entry")
                .containsIgnoringCase("foreign key");
        }
    }

    @Test @Order(72)
    void deleteCatalogDoc_cascadesToChunkManifest() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "chunk-cascade-doc");
            PgContainerHelper.insertCollection(ctx, TENANT_A, "fk-chunk-coll");
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
            // a matching nexus.chunks row for each of these two manifest inserts.
            byte[] chash0 = chashAscii("hash0000000000000000000000000000");
            byte[] chash1 = chashAscii("hash1111111111111111111111111111");
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                .values(TENANT_A, "fk-chunk-coll", chash0, "text0", vector(384))
                .values(TENANT_A, "fk-chunk-coll", chash1, "text1", vector(384))
                .onConflictDoNothing()
                .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .values(TENANT_A, "chunk-cascade-doc", 0, chash0, "fk-chunk-coll")
                .values(TENANT_A, "chunk-cascade-doc", 1, chash1, "fk-chunk-coll")
                .execute();

            int before = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("chunk-cascade-doc"))
                .fetchOne(0, int.class);
            assertThat(before).isEqualTo(2);

            ctx.deleteFrom(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("chunk-cascade-doc"))
                .execute();

            int after = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("chunk-cascade-doc"))
                .fetchOne(0, int.class);
            assertThat(after)
                .as("ON DELETE CASCADE must remove chunk manifest rows")
                .isZero();
        }
    }

    @Test @Order(73)
    void crossTenantChunkManifest_isRejectedByCompositeFk() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Seed catalog_documents for TENANT_A only
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, TUMBLER_A);
            PgContainerHelper.insertCollection(ctx, TENANT_B, "fk-chunk-coll");
            // Insert chunk row for TENANT_B referencing TENANT_A's tumbler — must be rejected
            // (FK checks as table owner; without composite key this would silently succeed)
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT_B, TUMBLER_A, 0, chashAscii("crosshashcrosshashcrosshash00000"), "fk-chunk-coll")
                    .execute()
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, "rls-check-tumbler");
        }

        // Query via svc role with Tenant-B GUC — must see 0 rows.
        // isLocal=true (SET LOCAL) only survives to the guarded SELECT if this
        // connection is in an explicit transaction — the pool hands out
        // autoCommit=true connections, so an implicit-commit boundary between
        // setTenant() and the SELECT would silently discard the GUC and this
        // test would exercise RLS default-deny instead of the claimed
        // cross-tenant mismatch (nexus-gcbp4).
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(false);
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_B, true);
            int count = ctx.selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TUMBLER.eq("rls-check-tumbler"))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("Tenant-B must not see Tenant-A catalog_documents after FK addition")
                .isZero();

            // Positive control (nexus-492qf): flip the GUC, in the SAME transaction,
            // to the row's rightful owner and confirm it becomes visible — proves the
            // negative assertion above is driven by tenant mismatch, not by the GUC
            // having silently gone missing (which would ALSO read back 0 rows under
            // FORCE RLS default-deny).
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, true);
            int ownerCount = ctx.selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TUMBLER.eq("rls-check-tumbler"))
                .fetchOne(0, int.class);
            assertThat(ownerCount)
                .as("Tenant-A (the rightful owner) must see its own catalog_documents row")
                .isPositive();

            svc.rollback();
        }
    }

    @Test @Order(61)
    void rlsIsolation_topicAssignment_tenantA_invisibleToTenantB() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "rls-ta-tumbler");
            insertTopic(ctx, TENANT_A, 300L, "rls-topic", "col-rls");
            // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires
            // a matching nexus.chunks row for this INSERT to succeed at all.
            byte[] chashBytes = HexFormat.of().parseHex(hexChash("rls-ta-tumbler"));
            seedChunk(ctx, TENANT_A, "col-rls", chashBytes);
            // Genuine hex-decoded bytes (RDR-194 P3d): see topicAssignment_chashDocId_
            // succeeds_noCatalogFk's identical note.
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                .values(TENANT_A, chashBytes, 300L, "hdbscan", "col-rls", OffsetDateTime.now())
                .execute();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(false);
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            byte[] chash = HexFormat.of().parseHex(hexChash("rls-ta-tumbler"));
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_B, true);
            int count = ctx.selectCount().from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.DOC_ID.eq(chash))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("Tenant-B must not see Tenant-A topic_assignments after FK addition")
                .isZero();

            // Positive control (nexus-492qf): flip the GUC, in the SAME transaction,
            // to the row's rightful owner and confirm it becomes visible — proves the
            // negative assertion above is driven by tenant mismatch, not by the GUC
            // having silently gone missing.
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, true);
            int ownerCount = ctx.selectCount().from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.DOC_ID.eq(chash))
                .fetchOne(0, int.class);
            assertThat(ownerCount)
                .as("Tenant-A (the rightful owner) must see its own topic_assignments row")
                .isPositive();

            svc.rollback();
        }
    }

    @Test @Order(62)
    void rlsIsolation_documentAspects_tenantA_invisibleToTenantB() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "rls-asp-tumbler");
            // hygiene-001 step 1: source_uri is NOT NULL now, alongside doc_id.
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_A, "knowledge__rls", "path/rls-aspect.pdf", OffsetDateTime.now(), "v1", "docling",
                    "rls-asp-tumbler", "file:///path/rls-aspect.pdf")
                .execute();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(false);
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_B, true);
            int count = ctx.selectCount().from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.DOC_ID.eq("rls-asp-tumbler"))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("Tenant-B must not see Tenant-A document_aspects after FK addition")
                .isZero();

            // Positive control (nexus-492qf): flip the GUC, in the SAME transaction,
            // to the row's rightful owner and confirm it becomes visible — proves the
            // negative assertion above is driven by tenant mismatch, not by the GUC
            // having silently gone missing.
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, true);
            int ownerCount = ctx.selectCount().from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.DOC_ID.eq("rls-asp-tumbler"))
                .fetchOne(0, int.class);
            assertThat(ownerCount)
                .as("Tenant-A (the rightful owner) must see its own document_aspects row")
                .isPositive();

            svc.rollback();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Insert a minimal nexus.chunks row at dim 384 (RDR-194 P3d, nexus-tk070.p3d):
     * every topic_assignments row now requires a matching (tenant_id,
     * source_collection, doc_id) -> chunks(tenant_id, collection, chash) parent via
     * topic_assignments_chunk_fk. Also registers the collection since
     * chunks_collection_fk requires it -- safe to call even when the caller already
     * registered it. Every call site in this file uses dim 384.
     */
    private static void seedChunk(DSLContext ctx, String tenantId, String collection, byte[] chashBytes) {
        PgContainerHelper.insertCollection(ctx, tenantId, collection);
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
            .values(tenantId, collection, chashBytes, "fk-test chunk", vector(384))
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Insert a topics row for use in topic_assignments FK tests.
     * Uses ON CONFLICT DO NOTHING (id is BIGSERIAL; here we supply explicit IDs to avoid
     * sequence issues across tests — tests use non-overlapping ids via @Order convention).
     */
    private static void insertTopic(DSLContext ctx, String tenantId, long id, String label, String collection) {
        // RDR-164 P1a: register the topic's collection (topics_collection_fk).
        PgContainerHelper.insertCollection(ctx, tenantId, collection);
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(id, tenantId, label, collection, 0, OffsetDateTime.now(), "pending")
            .onConflictDoNothing()
            .execute();
    }

    /** A pgvector value with every one of {@code dim} components equal to {@code 0.1}. */
    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    /** Store a chash-shaped string as its own ASCII bytes -- matches the pre-conversion
     *  raw-SQL behavior of a bare string literal into a {@code bytea} column via
     *  PostgreSQL's escape-format input (as opposed to {@link #hexChash}'s genuine
     *  hex-decoded form, used where the original SQL called {@code decode(..., 'hex')}). */
    private static byte[] chashAscii(String literal) {
        return literal.getBytes(java.nio.charset.StandardCharsets.US_ASCII);
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c). The pre-existing inline literals here predate
     *  RDR-180's full-digest flip and were only 32 hex chars (a legacy half-digest
     *  shape); topic_assignments.doc_id has no FK to catalog_documents (nexus-sa14p),
     *  so this is independent of any tumbler value used elsewhere in the same test. */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
