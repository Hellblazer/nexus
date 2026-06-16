package dev.nexus.service;

import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.assertj.core.api.Assertions.*;
import static org.assertj.core.data.Offset.offset;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-gmiaf.14 — TaxonomyRepository integration tests.
 *
 * <p>Hermetic embedded Postgres. Applies the full Liquibase master changelog.
 * Asserts:
 * <ol>
 *   <li>topics CRUD: insert / getById / updateLabel / renameTopic / markReviewed</li>
 *   <li>topics: getRootTopics / getChildTopics / getAllTopics / getUnreviewed</li>
 *   <li>topics: resolveLabel exact and collection-scoped</li>
 *   <li>topics: getDistinctCollections returns all known collections</li>
 *   <li>topics: deleteTopic returns collection, assignments cascade via FK</li>
 *   <li>topics: mergeTopics preserves MAX(similarity) on conflict</li>
 *   <li>assignments: assignTopic INSERT OR IGNORE for non-projection</li>
 *   <li>assignments: assignTopic projection GREATEST(similarity) on conflict</li>
 *   <li>assignments: getTopicDocIds / getAssignmentsForDocs / getDocIdsForLabel</li>
 *   <li>assignments: purgeAssignmentsForDoc removes empty topics</li>
 *   <li>collection ops: purgeCollection / renameCollection</li>
 *   <li>meta: recordDiscoverCount / getLastDiscoverDocCount</li>
 *   <li>links: upsertTopicLink GREATEST on conflict / getTopicLinkPairs</li>
 *   <li>ICF: countDistinctSourceCollections / computeIcfRows</li>
 *   <li>analytics: topTopicsForCollection / chunkGroundedIn / getProjectionCountsByCollection</li>
 *   <li>ETL import: importTopic preserves id + GREATEST doc_count + EXCLUDED review_status</li>
 *   <li>ETL import: importTopic idempotent re-run does not double-insert</li>
 *   <li>ETL import: importAssignment / importTopicLink / importTaxonomyMeta fidelity</li>
 *   <li>RLS isolation: tenant A cannot see tenant B rows</li>
 *   <li>RLS WITH CHECK: raw INSERT with wrong tenant_id is rejected</li>
 *   <li>fail-closed: unset GUC returns zero rows</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TaxonomyRepositoryTest {

    private static final String TENANT_A = "tax-tenant-a";
    private static final String TENANT_B = "tax-tenant-b";
    private static final String SVC_ROLE = "svc_tax_test";
    private static final String SVC_PASS = "svc_tax_test_pass";

    private static final String PAST_TS  = "2024-03-15T08:00:00Z";
    private static final String COL_A    = "knowledge__a";
    private static final String COL_B    = "knowledge__b";
    // RDR-152 nexus-1di3r Phase 3 — distinct collections to avoid cross-test leakage.
    private static final String COL_OS   = "knowledge__os";
    private static final String COL_RB   = "knowledge__rb";
    private static final String COL_DISC = "knowledge__disc";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
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

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String schema = "nexus";
            for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON " + schema + "." + table + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE " + schema + ".topics_id_seq TO " + SVC_ROLE);
            // Grant SELECT on catalog_documents to the DML role for general catalog
            // query coverage in mixed tests. (nexus-sa14p: importAssignment no longer
            // reads catalog_documents — fk_ta_catalog_doc was removed — so this is not
            // strictly required for assignment imports, but is harmless and mirrors the
            // prod nexus_svc grant set.)
            su.createStatement().execute(
                "GRANT SELECT ON " + schema + ".catalog_documents TO " + SVC_ROLE);
            // RDR-156 P0.2: assignTopic/importAssignment now auto-stub catalog_collections;
            // the svc role needs INSERT (and SELECT for the ON CONFLICT check).
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON " + schema + ".catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SCHEMA " + schema + " TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO " + schema + ", public");

            // nexus-b7v6i: topic_assignments.doc_id now enforces a FK to catalog_documents(tenant_id, tumbler).
            // Seed all doc_ids used as tumblers in this test class so FK checks pass.
            // "doc-label-missing" is intentionally omitted — tests expect it to be absent.
            for (String tumbler : List.of(
                    "doc-del-1", "doc-merge", "doc-manual", "doc-proj",
                    "doc-label-1", "doc-label-2", "doc-purge-only", "doc-purge-col",
                    "icf-doc-1", "icf-doc-2", "imp-doc-1",
                    // RDR-152 nexus-1di3r Phase 3 fixtures
                    "os-doc-manual", "os-doc-hdbscan",
                    "rb-doc-1", "rb-doc-2", "rb-doc-manual",
                    "disc-doc-1", "disc-doc-2")) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
                    "VALUES ('" + TENANT_A + "', '" + tumbler + "', 'Test fixture: " + tumbler + "') " +
                    "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
            }
            // RDR-156 P0.2: topic_assignments.source_collection now enforces a FK to
            // catalog_collections(tenant_id, name).  Seed stub rows for all test collections.
            for (String col : List.of(COL_A, COL_B)) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + TENANT_A + "', '" + col + "') " +
                    "ON CONFLICT (tenant_id, name) DO NOTHING");
            }
        }

        svcDs       = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
        repo        = new TaxonomyRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ── Topics CRUD ────────────────────────────────────────────────────────────

    @Test @Order(1)
    void insertAndGetById_roundTrip() {
        long id = repo.insertTopic(TENANT_A, "machine-learning", null, COL_A, 0, null, "ML,AI");
        assertThat(id).isPositive();
        Optional<Map<String, Object>> row = repo.getTopicById(TENANT_A, id);
        assertThat(row).isPresent();
        assertThat(row.get().get("label")).isEqualTo("machine-learning");
        assertThat(row.get().get("collection")).isEqualTo(COL_A);
        assertThat(row.get().get("review_status")).isEqualTo("pending");
    }

    @Test @Order(2)
    void updateLabel_changesLabelOnly() {
        long id = repo.insertTopic(TENANT_A, "orig-label", null, COL_A, 0, null, null);
        repo.updateTopicLabel(TENANT_A, id, "new-label");
        Optional<Map<String, Object>> row = repo.getTopicById(TENANT_A, id);
        assertThat(row).isPresent();
        assertThat(row.get().get("label")).isEqualTo("new-label");
        assertThat(row.get().get("review_status")).isEqualTo("pending");
    }

    @Test @Order(3)
    void renameTopic_setsAccepted() {
        long id = repo.insertTopic(TENANT_A, "draft-topic", null, COL_A, 0, null, null);
        repo.renameTopic(TENANT_A, id, "final-label");
        Optional<Map<String, Object>> row = repo.getTopicById(TENANT_A, id);
        assertThat(row).isPresent();
        assertThat(row.get().get("label")).isEqualTo("final-label");
        assertThat(row.get().get("review_status")).isEqualTo("accepted");
    }

    @Test @Order(4)
    void markTopicReviewed_updatesStatus() {
        long id = repo.insertTopic(TENANT_A, "reviewed-topic", null, COL_A, 0, null, null);
        repo.markTopicReviewed(TENANT_A, id, "accepted");
        Optional<Map<String, Object>> row = repo.getTopicById(TENANT_A, id);
        assertThat(row.get().get("review_status")).isEqualTo("accepted");
    }

    @Test @Order(5)
    void rootAndChildTopics_tree() {
        long root = repo.insertTopic(TENANT_A, "parent-topic", null, COL_A, 5, null, null);
        long child1 = repo.insertTopic(TENANT_A, "child-1", root, COL_A, 3, null, null);
        long child2 = repo.insertTopic(TENANT_A, "child-2", root, COL_A, 2, null, null);

        List<Map<String, Object>> roots = repo.getRootTopics(TENANT_A);
        assertThat(roots).extracting(m -> m.get("id")).contains(root);
        // children should not appear as roots
        assertThat(roots).extracting(m -> m.get("id")).doesNotContain(child1, child2);

        List<Map<String, Object>> children = repo.getChildTopics(TENANT_A, root);
        assertThat(children).extracting(m -> m.get("id")).containsExactlyInAnyOrder(child1, child2);
    }

    @Test @Order(6)
    void getAllTopics_collectionFilter() {
        repo.insertTopic(TENANT_A, "colb-topic", null, COL_B, 1, null, null);
        List<Map<String, Object>> all  = repo.getAllTopics(TENANT_A, null);
        List<Map<String, Object>> colb = repo.getAllTopics(TENANT_A, COL_B);
        assertThat(colb).allSatisfy(m -> assertThat(m.get("collection")).isEqualTo(COL_B));
        assertThat(all.size()).isGreaterThanOrEqualTo(colb.size());
    }

    @Test @Order(7)
    void getUnreviewed_filtersPendingOnly() {
        long pending  = repo.insertTopic(TENANT_A, "unrev-pending", null, COL_A, 0, null, null);
        long accepted = repo.insertTopic(TENANT_A, "unrev-accepted", null, COL_A, 0, null, null);
        repo.markTopicReviewed(TENANT_A, accepted, "accepted");

        List<Map<String, Object>> unrev = repo.getUnreviewedTopics(TENANT_A, null, 200);
        var ids = unrev.stream().map(m -> m.get("id")).toList();
        assertThat(ids).contains(pending);
        assertThat(ids).doesNotContain(accepted);
    }

    @Test @Order(8)
    void resolveLabel_exactAndCollectionScoped() {
        String label = "unique-label-xyz-" + System.nanoTime();
        long id = repo.insertTopic(TENANT_A, label, null, COL_A, 0, null, null);
        Optional<Long> resolved = repo.resolveLabel(TENANT_A, label, null);
        assertThat(resolved).isPresent().contains(id);

        Optional<Long> scopedHit  = repo.resolveLabel(TENANT_A, label, COL_A);
        Optional<Long> scopedMiss = repo.resolveLabel(TENANT_A, label, COL_B);
        assertThat(scopedHit).isPresent().contains(id);
        assertThat(scopedMiss).isEmpty();
    }

    @Test @Order(9)
    void getDistinctCollections_includesBothCols() {
        List<String> cols = repo.getDistinctCollections(TENANT_A);
        assertThat(cols).contains(COL_A, COL_B);
    }

    // ── Delete / merge ─────────────────────────────────────────────────────────

    @Test @Order(10)
    void deleteTopic_returnsCollectionAndCascades() {
        long topicId = repo.insertTopic(TENANT_A, "doomed-topic", null, COL_A, 0, null, null);
        repo.assignTopic(TENANT_A, "doc-del-1", topicId, "manual", null, null, null);

        Optional<String> col = repo.deleteTopic(TENANT_A, topicId);
        assertThat(col).isPresent().contains(COL_A);

        // Topic gone
        assertThat(repo.getTopicById(TENANT_A, topicId)).isEmpty();
        // Assignments cascaded
        assertThat(repo.getTopicDocIds(TENANT_A, topicId, 0)).isEmpty();
    }

    @Test @Order(11)
    void mergeTopics_preservesMaxSimilarity() {
        long src = repo.insertTopic(TENANT_A, "src-topic-merge", null, COL_A, 0, null, null);
        long tgt = repo.insertTopic(TENANT_A, "tgt-topic-merge", null, COL_A, 0, null, null);

        // src has similarity 0.8, tgt already has 0.9 for same doc
        repo.assignTopic(TENANT_A, "doc-merge", src, "projection", 0.8, COL_A, null);
        repo.assignTopic(TENANT_A, "doc-merge", tgt, "projection", 0.9, COL_A, null);

        Optional<String> col = repo.mergeTopics(TENANT_A, src, tgt);
        assertThat(col).isPresent().contains(COL_A);

        // src must be gone
        assertThat(repo.getTopicById(TENANT_A, src)).isEmpty();

        // tgt should still have the doc, with max similarity preserved (0.9)
        List<String> docIds = repo.getTopicDocIds(TENANT_A, tgt, 0);
        assertThat(docIds).contains("doc-merge");
    }

    // ── Assignments ────────────────────────────────────────────────────────────

    @Test @Order(12)
    void assignTopic_nonProjection_insertOrIgnore() {
        long topicId = repo.insertTopic(TENANT_A, "assign-manual-topic", null, COL_A, 0, null, null);
        repo.assignTopic(TENANT_A, "doc-manual", topicId, "manual", null, null, null);
        repo.assignTopic(TENANT_A, "doc-manual", topicId, "manual", null, null, null); // idempotent

        List<String> docs = repo.getTopicDocIds(TENANT_A, topicId, 0);
        assertThat(docs).containsExactly("doc-manual");
    }

    @Test @Order(13)
    void assignTopic_projection_greatestSimilarity() {
        long topicId = repo.insertTopic(TENANT_A, "assign-proj-topic", null, COL_A, 0, null, null);
        repo.assignTopic(TENANT_A, "doc-proj", topicId, "projection", 0.5, COL_A, null);
        repo.assignTopic(TENANT_A, "doc-proj", topicId, "projection", 0.8, COL_A, null); // higher wins
        repo.assignTopic(TENANT_A, "doc-proj", topicId, "projection", 0.3, COL_A, null); // lower ignored

        List<String> docs = repo.getTopicDocIds(TENANT_A, topicId, 0);
        assertThat(docs).containsExactly("doc-proj");

        // Verify the max sim row is what we get via chunkGroundedIn
        Optional<Double> sim = repo.chunkGroundedIn(TENANT_A, "doc-proj", COL_A);
        assertThat(sim).isPresent();
        assertThat(sim.get()).isEqualTo(0.8, offset(0.001));
    }

    @Test @Order(14)
    void getAssignmentsForDocs_andByLabel() {
        long topicId = repo.insertTopic(TENANT_A, "label-search-topic", null, COL_A, 0, null, null);
        repo.assignTopic(TENANT_A, "doc-label-1", topicId, "manual", null, null, null);
        repo.assignTopic(TENANT_A, "doc-label-2", topicId, "manual", null, null, null);

        List<Map<String, Object>> assignments = repo.getAssignmentsForDocs(
            TENANT_A, List.of("doc-label-1", "doc-label-2", "doc-label-missing"));
        assertThat(assignments).hasSizeGreaterThanOrEqualTo(2);

        List<String> byLabel = repo.getDocIdsForLabel(TENANT_A, "label-search-topic");
        assertThat(byLabel).containsExactlyInAnyOrder("doc-label-1", "doc-label-2");
    }

    @Test @Order(15)
    void purgeAssignmentsForDoc_removesEmptyTopics() {
        long topicId = repo.insertTopic(TENANT_A, "purge-only-topic", null, COL_A, 0, null, null);
        repo.assignTopic(TENANT_A, "doc-purge-only", topicId, "manual", null, null, null);

        int removed = repo.purgeAssignmentsForDoc(TENANT_A, COL_A, "doc-purge-only");
        assertThat(removed).isEqualTo(1);

        // Empty topic must be pruned
        assertThat(repo.getTopicById(TENANT_A, topicId)).isEmpty();
    }

    // ── Collection ops ─────────────────────────────────────────────────────────

    @Test @Order(16)
    void purgeCollection_removesAllRows() {
        String tempCol = "knowledge__purge-temp";
        long id = repo.insertTopic(TENANT_A, "purge-col-topic", null, tempCol, 0, null, null);
        repo.assignTopic(TENANT_A, "doc-purge-col", id, "manual", null, tempCol, null);
        repo.recordDiscoverCount(TENANT_A, tempCol, 5, null);

        Map<String, Integer> counts = repo.purgeCollection(TENANT_A, tempCol);
        assertThat(counts.get("topics")).isGreaterThan(0);

        assertThat(repo.getAllTopics(TENANT_A, tempCol)).isEmpty();
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, tempCol)).isEmpty();
    }

    @Test @Order(17)
    void renameCollection_updatesAllRows() {
        String oldCol = "knowledge__rename-old-" + System.nanoTime();
        String newCol = "knowledge__rename-new-" + System.nanoTime();
        repo.insertTopic(TENANT_A, "rename-topic", null, oldCol, 1, null, null);
        repo.recordDiscoverCount(TENANT_A, oldCol, 1, null);

        repo.renameCollection(TENANT_A, oldCol, newCol);
        assertThat(repo.getAllTopics(TENANT_A, oldCol)).isEmpty();
        assertThat(repo.getAllTopics(TENANT_A, newCol)).isNotEmpty();
    }

    // ── Meta ───────────────────────────────────────────────────────────────────

    @Test @Order(18)
    void recordAndGetDiscoverCount() {
        repo.recordDiscoverCount(TENANT_A, COL_A, 42, PAST_TS);
        Optional<Integer> count = repo.getLastDiscoverDocCount(TENANT_A, COL_A);
        assertThat(count).isPresent();
        assertThat(count.get()).isEqualTo(42);

        // Idempotent re-record with higher count: GREATEST wins
        repo.recordDiscoverCount(TENANT_A, COL_A, 100, null);
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, COL_A)).contains(100);
    }

    // ── Links ──────────────────────────────────────────────────────────────────

    @Test @Order(19)
    void upsertAndGetTopicLinks() {
        long t1 = repo.insertTopic(TENANT_A, "link-topic-1", null, COL_A, 0, null, null);
        long t2 = repo.insertTopic(TENANT_A, "link-topic-2", null, COL_A, 0, null, null);

        // upsertTopicLink is the LIVE-COMPUTE path: EXCLUDED (overwrite), NOT
        // GREATEST. A decremented recompute must lower the stored count (RDR-152
        // nexus-1di3r.4). Contrast importTopicLink (ETL) below, which keeps GREATEST.
        repo.upsertTopicLink(TENANT_A, t1, t2, 5, "co-occurrence");
        repo.upsertTopicLink(TENANT_A, t1, t2, 3, "co-occurrence"); // EXCLUDED overwrites -> 3

        List<Map<String, Object>> pairs = repo.getTopicLinkPairs(TENANT_A, List.of(t1, t2));
        assertThat(pairs).isNotEmpty();
        var link = pairs.stream()
            .filter(m -> ((Number) m.get("from_topic_id")).longValue() == t1
                      && ((Number) m.get("to_topic_id")).longValue() == t2)
            .findFirst();
        assertThat(link).isPresent();
        assertThat(((Number) link.get().get("link_count")).intValue()).isEqualTo(3);
    }

    // ── ICF ────────────────────────────────────────────────────────────────────

    @Test @Order(20)
    void icf_sourceCountAndRows() {
        String srcColA = "src__col-a-icf";
        String srcColB = "src__col-b-icf";
        long topic = repo.insertTopic(TENANT_A, "icf-test-topic", null, COL_A, 0, null, null);
        repo.assignTopic(TENANT_A, "icf-doc-1", topic, "projection", 0.8, srcColA, null);
        repo.assignTopic(TENANT_A, "icf-doc-2", topic, "projection", 0.7, srcColB, null);

        int n = repo.countDistinctSourceCollections(TENANT_A);
        assertThat(n).isGreaterThanOrEqualTo(2);

        List<Map<String, Object>> rows = repo.computeIcfRows(TENANT_A, n);
        assertThat(rows).isNotEmpty();
        // Every row must have icf_raw > 0 (N/DF where DF > 0)
        rows.forEach(r -> assertThat(((Number) r.get("icf_raw")).doubleValue()).isGreaterThan(0.0));
    }

    // ── ETL import ─────────────────────────────────────────────────────────────

    @Test @Order(21)
    void importTopic_preservesId_docCountNotEtlMerged() {
        // RDR-154 P0 (nexus-i7ivk): doc_count is trigger-maintained and is no
        // longer an ETL ON CONFLICT merge participant. The INSERT branch seeds
        // the column; re-imports MUST NOT touch it (neither GREATEST nor verbatim).
        long srcId = repo.importTopic(TENANT_A, 9900001L, "imported-topic", null, COL_A,
                                      "centroid-hash-1", 10, PAST_TS, "pending", null);
        assertThat(srcId).isEqualTo(9900001L);
        Optional<Map<String, Object>> row = repo.getTopicById(TENANT_A, 9900001L);
        assertThat(row).isPresent();
        assertThat(row.get().get("label")).isEqualTo("imported-topic");
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(10); // seed

        // Re-import with LOWER doc_count — ETL no longer writes doc_count; seed preserved.
        repo.importTopic(TENANT_A, 9900001L, "imported-topic", null, COL_A,
                         "centroid-hash-1", 5, PAST_TS, "accepted", null);
        row = repo.getTopicById(TENANT_A, 9900001L);
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(10);

        // Re-import with HIGHER doc_count — still NOT written by the ETL upsert.
        repo.importTopic(TENANT_A, 9900001L, "imported-topic", null, COL_A,
                         "centroid-hash-1", 99, PAST_TS, "pending", null);
        row = repo.getTopicById(TENANT_A, 9900001L);
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(10);

        // review_status STILL uses EXCLUDED (verbatim): last import wins.
        assertThat(row.get().get("review_status")).isEqualTo("pending");
    }

    @Test @Order(22)
    void importAssignment_fidelityAndIdempotent() {
        long topicId = repo.importTopic(TENANT_A, 9900002L, "assign-import-topic", null, COL_A,
                                        null, 0, PAST_TS, "pending", null);
        repo.importAssignment(TENANT_A, "imp-doc-1", topicId, "projection", 0.7, PAST_TS, COL_A);

        List<String> docs = repo.getTopicDocIds(TENANT_A, topicId, 0);
        assertThat(docs).contains("imp-doc-1");

        // Re-import with same data — idempotent (GREATEST similarity)
        repo.importAssignment(TENANT_A, "imp-doc-1", topicId, "projection", 0.7, PAST_TS, COL_A);
        assertThat(repo.getTopicDocIds(TENANT_A, topicId, 0)).containsExactly("imp-doc-1");
    }

    @Test @Order(23)
    void importTopicLink_fidelityAndGreatestLinkCount() {
        long t1 = repo.importTopic(TENANT_A, 9900003L, "link-import-t1", null, COL_A,
                                   null, 0, PAST_TS, "pending", null);
        long t2 = repo.importTopic(TENANT_A, 9900004L, "link-import-t2", null, COL_A,
                                   null, 0, PAST_TS, "pending", null);

        repo.importTopicLink(TENANT_A, t1, t2, 7, "co-occur");
        repo.importTopicLink(TENANT_A, t1, t2, 3, "co-occur"); // GREATEST 7 preserved

        List<Map<String, Object>> pairs = repo.getTopicLinkPairs(TENANT_A, List.of(t1, t2));
        var link = pairs.stream()
            .filter(m -> ((Number) m.get("from_topic_id")).longValue() == t1)
            .findFirst();
        assertThat(link).isPresent();
        assertThat(((Number) link.get().get("link_count")).intValue()).isEqualTo(7);
    }

    @Test @Order(24)
    void importTaxonomyMeta_greatestDiscoverCount() {
        String col = "knowledge__meta-import";
        repo.importTaxonomyMeta(TENANT_A, col, 50, PAST_TS);
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, col)).contains(50);

        // Re-import with lower count — GREATEST 50 preserved
        repo.importTaxonomyMeta(TENANT_A, col, 20, PAST_TS);
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, col)).contains(50);

        // Re-import with higher count
        repo.importTaxonomyMeta(TENANT_A, col, 80, PAST_TS);
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, col)).contains(80);
    }

    // ── RLS isolation ──────────────────────────────────────────────────────────

    @Test @Order(25)
    void rls_tenantA_cannotReadTenantB() {
        long idA = repo.insertTopic(TENANT_A, "rls-a-exclusive", null, COL_A, 0, null, null);
        long idB = repo.insertTopic(TENANT_B, "rls-b-exclusive", null, COL_A, 0, null, null);

        List<Map<String, Object>> topicsA = repo.getAllTopics(TENANT_A, null);
        List<Map<String, Object>> topicsB = repo.getAllTopics(TENANT_B, null);

        var idsA = topicsA.stream().map(m -> m.get("id")).toList();
        var idsB = topicsB.stream().map(m -> m.get("id")).toList();

        assertThat(idsA).contains(idA);
        assertThat(idsA).doesNotContain(idB);
        assertThat(idsB).contains(idB);
        assertThat(idsB).doesNotContain(idA);
    }

    @Test @Order(26)
    void rls_withCheck_rejectsWrongTenant() throws Exception {
        // Direct INSERT with tenant_id != GUC → WITH CHECK violation
        // The GUC is 'injector-tenant' but the row has tenant_id='other-tenant' → rejected
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Grant INSERT so the svc role can attempt the INSERT (it will be rejected by RLS)
            su.createStatement().execute(
                "GRANT INSERT ON nexus.topics TO " + SVC_ROLE);
        }

        com.zaxxer.hikari.HikariDataSource svcDsForCheck = buildSvcDataSource();
        try {
            try (Connection c = svcDsForCheck.getConnection()) {
                c.setAutoCommit(false);
                // Stamp GUC as 'injector-tenant'
                c.createStatement().execute("SELECT set_config('nexus.tenant', 'injector-tenant', true)");
                // Attempt INSERT with a different tenant_id → WITH CHECK rejects
                var e = assertThrows(PSQLException.class,
                    () -> c.createStatement().execute(
                        "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) " +
                        "VALUES ('other-tenant', 'evil', 'col-x', 0, NOW(), 'pending')"));
                // RLS WITH CHECK violation → new row violates row-level security policy
                assertThat(e.getMessage()).containsIgnoringCase("row-level security");
                c.rollback();
            }
        } finally {
            svcDsForCheck.close();
        }
    }

    @Test @Order(27)
    void failClosed_unsetGucReturnsZeroRows() throws Exception {
        // Insert a row via the svc role (GUC set), then query without GUC — must return 0
        long id = repo.insertTopic(TENANT_A, "fail-closed-check", null, COL_A, 0, null, null);
        assertThat(repo.getTopicById(TENANT_A, id)).isPresent();

        // Connect directly with svc role, no GUC stamp → RLS sees NULL tenant → 0 rows
        var rawConfig = new com.zaxxer.hikari.HikariConfig();
        rawConfig.setJdbcUrl(pg.getJdbcUrl());
        rawConfig.setUsername(SVC_ROLE);
        rawConfig.setPassword(SVC_PASS);
        rawConfig.setMaximumPoolSize(1);
        rawConfig.addDataSourceProperty("options", "-c search_path=nexus,public");
        com.zaxxer.hikari.HikariDataSource rawDs = new com.zaxxer.hikari.HikariDataSource(rawConfig);
        try (Connection c = rawDs.getConnection()) {
            c.setAutoCommit(true);
            var rs = c.createStatement().executeQuery(
                "SELECT id FROM nexus.topics WHERE label = 'fail-closed-check'");
            assertThat(rs.next()).as("unset GUC must return 0 rows (fail-closed)").isFalse();
        } finally {
            rawDs.close();
        }
    }

    // ── RDR-152 nexus-1di3r Phase 3: chroma-free taxonomy persist/read ─────────

    @SuppressWarnings("unchecked")
    @Test @Order(28)
    void readRebuildOldState_returnsTopicMapAndManualAssignmentsShape() {
        long t1 = repo.insertTopic(TENANT_A, "os-topic-1", null, COL_OS, 0, PAST_TS, "[\"a\"]");
        long t2 = repo.insertTopic(TENANT_A, "os-topic-2", null, COL_OS, 0, PAST_TS, "[\"b\"]");
        repo.markTopicReviewed(TENANT_A, t2, "accepted");
        // One manual assignment (must surface) + one hdbscan (must NOT surface).
        repo.assignTopic(TENANT_A, "os-doc-manual", t1, "manual", null, null, null);
        repo.assignTopic(TENANT_A, "os-doc-hdbscan", t1, "hdbscan", null, null, null);

        Map<String, Object> state = repo.readRebuildOldState(TENANT_A, COL_OS);

        assertThat(state).containsOnlyKeys("old_topic_map", "manual_assignments");

        var oldTopicMap = (List<Map<String, Object>>) state.get("old_topic_map");
        assertThat(oldTopicMap).hasSize(2);
        assertThat(oldTopicMap.get(0)).containsOnlyKeys("id", "label", "review_status");
        assertThat(oldTopicMap).anySatisfy(m -> {
            assertThat(m.get("id")).isEqualTo(t2);
            assertThat(m.get("label")).isEqualTo("os-topic-2");
            assertThat(m.get("review_status")).isEqualTo("accepted");
        });

        var manual = (List<Map<String, Object>>) state.get("manual_assignments");
        assertThat(manual).hasSize(1);
        assertThat(manual.get(0)).containsOnlyKeys("doc_id", "topic_id");
        assertThat(manual.get(0).get("doc_id")).isEqualTo("os-doc-manual");
        assertThat(((Number) manual.get(0).get("topic_id")).longValue()).isEqualTo(t1);
    }

    @SuppressWarnings("unchecked")
    @Test @Order(29)
    void persistRebuildTopics_replaceSemanticsClearsOldInsertsNewAppliesManual() {
        // Seed an "old" topic + assignment that the rebuild must clear.
        long oldId = repo.insertTopic(TENANT_A, "rb-old", null, COL_RB, 1, PAST_TS, null);
        repo.assignTopic(TENANT_A, "rb-doc-1", oldId, "hdbscan", null, null, null);

        var specs = List.of(
            m("label", "rb-new-0", "doc_count", 2, "terms", "[\"x\"]",
              "review_status", "pending", "assigned_by", "hdbscan",
              "doc_ids", List.of("rb-doc-1", "rb-doc-2")),
            m("label", "rb-new-1", "doc_count", 0, "terms", "[\"y\"]",
              "review_status", "pending", "assigned_by", "hdbscan",
              "doc_ids", List.of()));
        // Transfer the manual doc to spec index 1.
        Map<String, Object> manualTransfers = m("rb-doc-manual", 1);

        List<Long> ids = repo.persistRebuildTopics(TENANT_A, COL_RB, specs, manualTransfers);

        assertThat(ids).hasSize(2);
        // Old topic gone; exactly the two new topics remain for this collection.
        var topics = repo.getAllTopics(TENANT_A, COL_RB);
        assertThat(topics).hasSize(2);
        assertThat(topics).noneSatisfy(m -> assertThat(m.get("id")).isEqualTo(oldId));
        assertThat(topics).extracting(m -> m.get("label"))
            .containsExactlyInAnyOrder("rb-new-0", "rb-new-1");

        // Manual transfer applied to topic_ids[1], assigned_by='manual'.
        var manual = (List<Map<String, Object>>)
            repo.readRebuildOldState(TENANT_A, COL_RB).get("manual_assignments");
        assertThat(manual).hasSize(1);
        assertThat(manual.get(0).get("doc_id")).isEqualTo("rb-doc-manual");
        assertThat(((Number) manual.get(0).get("topic_id")).longValue()).isEqualTo(ids.get(1));
    }

    @Test @Order(30)
    void persistRebuildTopics_emptySpecsStillClearsOldRows() {
        long oldId = repo.insertTopic(TENANT_A, "rb-stale", null, COL_RB, 1, PAST_TS, null);
        assertThat(repo.getTopicById(TENANT_A, oldId)).isPresent();

        List<Long> ids = repo.persistRebuildTopics(
            TENANT_A, COL_RB, List.of(), Map.of());

        assertThat(ids).isEmpty();
        assertThat(repo.getAllTopics(TENANT_A, COL_RB)).isEmpty();
    }

    @Test @Order(31)
    void persistDiscoveredTopics_insertsTopicsAndAssignmentsReturnsAlignedIds() {
        var specs = List.of(
            m("label", "disc-0", "doc_count", 2, "terms", "[\"p\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of("disc-doc-1", "disc-doc-2")),
            m("label", "disc-1", "doc_count", 0, "terms", "[\"q\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of()));

        List<Long> ids = repo.persistDiscoveredTopics(TENANT_A, COL_DISC, specs);

        assertThat(ids).hasSize(2);
        var topics = repo.getAllTopics(TENANT_A, COL_DISC);
        assertThat(topics).extracting(m -> m.get("label"))
            .containsExactlyInAnyOrder("disc-0", "disc-1");
        // review_status defaults to 'pending' for discovered topics.
        assertThat(topics).allSatisfy(m -> assertThat(m.get("review_status")).isEqualTo("pending"));
        assertThat(repo.getTopicDocIds(TENANT_A, ids.get(0), 0))
            .containsExactlyInAnyOrder("disc-doc-1", "disc-doc-2");
    }

    @Test @Order(32)
    void persistDiscoveredTopics_existingTopicsGuardReturnsNoOp() {
        // COL_DISC already holds the 2 topics from Order(31); add 1 pre-existing here = 3.
        repo.insertTopic(TENANT_A, "disc-pre-existing", null, COL_DISC, 0, PAST_TS, null);
        assertThat(repo.getAllTopics(TENANT_A, COL_DISC)).hasSize(3);

        var specs = List.of(
            m("label", "disc-should-not-insert", "doc_count", 0, "terms", "[\"z\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of()));
        List<Long> ids = repo.persistDiscoveredTopics(TENANT_A, COL_DISC, specs);

        assertThat(ids).isEmpty();
        // Guard fired: still exactly the 3 pre-existing rows, none inserted.
        assertThat(repo.getAllTopics(TENANT_A, COL_DISC)).hasSize(3);
    }

    // ── RDR-154 P0 (nexus-i7ivk): doc_count trigger as SOLE writer ──────────────

    @Test @Order(40)
    void docCountTrigger_purgeDeleteLeavesCountCorrect() {
        // The cascade/purge-delete hole the trigger closes: deleting some of a
        // topic's assignments must recompute doc_count on the surviving row.
        final String col = "knowledge__dctrg_purge";
        long t = repo.insertTopic(TENANT_A, "purge-recount", null, col, 0, PAST_TS, null);
        repo.assignTopic(TENANT_A, "pd-doc-1", t, "manual", null, col, null);
        repo.assignTopic(TENANT_A, "pd-doc-2", t, "manual", null, col, null);
        // AFTER INSERT trigger set the live count.
        assertThat(((Number) repo.getTopicById(TENANT_A, t).get().get("doc_count")).intValue())
            .isEqualTo(2);

        // Purge one doc's assignment; topic survives (still has pd-doc-2).
        repo.purgeAssignmentsForDoc(TENANT_A, col, "pd-doc-1");

        // AFTER DELETE trigger recomputed: exactly 1 remains.
        assertThat(((Number) repo.getTopicById(TENANT_A, t).get().get("doc_count")).intValue())
            .isEqualTo(1);
    }

    @Test @Order(41)
    void docCountTrigger_etlUpsertDoesNotStompTriggerValue() {
        // After the trigger has computed a live count, an ETL importTopic upsert
        // on the same row MUST NOT overwrite doc_count (RDR-154 Decision 1).
        final String col = "knowledge__dctrg_etl";
        long t = repo.insertTopic(TENANT_A, "etl-nostomp", null, col, 0, PAST_TS, null);
        repo.assignTopic(TENANT_A, "es-doc-1", t, "manual", null, col, null);
        repo.assignTopic(TENANT_A, "es-doc-2", t, "manual", null, col, null);
        repo.assignTopic(TENANT_A, "es-doc-3", t, "manual", null, col, null);
        assertThat(((Number) repo.getTopicById(TENANT_A, t).get().get("doc_count")).intValue())
            .isEqualTo(3);

        // ETL re-import the same id with a wildly different doc_count seed.
        repo.importTopic(TENANT_A, t, "etl-nostomp", null, col,
                         null, 99, PAST_TS, "accepted", null);

        // Trigger value survives; the 99 was dropped from the ON CONFLICT merge.
        assertThat(((Number) repo.getTopicById(TENANT_A, t).get().get("doc_count")).intValue())
            .isEqualTo(3);
    }

    @Test @Order(42)
    void docCountTrigger_crossTenantIsolation() {
        // An assignment INSERT in tenant A that references a topic OWNED BY
        // tenant B (the FK check bypasses RLS, so the row can be inserted) MUST
        // NOT mutate tenant B's topics.doc_count.
        //
        // NOTE on what this proves: topics PK is `id` alone (globally unique), so
        // a same-id topic cannot exist under two tenants — meaning INVOKER vs
        // DEFINER is NOT behaviorally distinguishable here. The isolation this
        // test exercises is the trigger's explicit `t.tenant_id = a.tenant_id`
        // predicate (defense-in-depth), not RLS. The enforceable guard for the
        // SECURITY INVOKER property itself is the prosecdef=false assertion in
        // TaxonomySchemaLiquibaseTest.docCountTrigger_functionsTriggersAndComment.
        final long bTopicId = 9900500L;
        final String col = "knowledge__dctrg_xtenant";
        repo.importTopic(TENANT_B, bTopicId, "b-topic", null, col,
                         null, 7, PAST_TS, "pending", null);
        assertThat(((Number) repo.getTopicById(TENANT_B, bTopicId).get().get("doc_count")).intValue())
            .isEqualTo(7);

        // Tenant A inserts an assignment pointing at tenant B's topic id.
        repo.assignTopic(TENANT_A, "xt-doc-a", bTopicId, "manual", null, col, null);

        // Tenant B's row is untouched (trigger scoped to the session tenant).
        assertThat(((Number) repo.getTopicById(TENANT_B, bTopicId).get().get("doc_count")).intValue())
            .isEqualTo(7);
        // And tenant A owns no such topic id.
        assertThat(repo.getTopicById(TENANT_A, bTopicId)).isEmpty();
    }

    @Test @Order(43)
    void docCountTrigger_discoveryAssignmentInsertOverridesSeed() {
        // persistDiscoveredTopics seeds a per-spec doc_count then inserts the
        // assignments. The AFTER INSERT trigger must recompute doc_count from the
        // actual doc_ids, overriding any (here deliberately wrong) seed.
        final String col = "knowledge__dctrg_disc";
        var specs = List.of(
            m("label", "disc-recount", "doc_count", 999, "terms", "[\"p\"]",
              "assigned_by", "hdbscan",
              "doc_ids", List.of("dr-doc-1", "dr-doc-2", "dr-doc-3")));
        List<Long> ids = repo.persistDiscoveredTopics(TENANT_A, col, specs);
        assertThat(ids).hasSize(1);

        // Trigger recomputed the live count (3), not the bogus 999 seed.
        assertThat(((Number) repo.getTopicById(TENANT_A, ids.get(0)).get().get("doc_count")).intValue())
            .isEqualTo(3);
    }

    @Test @Order(44)
    void batchedAssignmentInsert_largeTopic_exactCountAndAllDocs() {
        // nexus-eh89h: the per-topic assignments are now inserted in one multi-row
        // statement. Exercise a large doc set to guard the VALUES builder and
        // confirm the doc_count trigger computes the exact live count.
        final String col = "knowledge__batch_large";
        List<String> docIds = new java.util.ArrayList<>();
        for (int i = 0; i < 50; i++) docIds.add("bl-doc-" + i);
        var specs = List.of(
            m("label", "batch-large", "doc_count", 0, "terms", "[\"p\"]",
              "assigned_by", "hdbscan", "doc_ids", docIds));
        List<Long> ids = repo.persistDiscoveredTopics(TENANT_A, col, specs);
        assertThat(ids).hasSize(1);

        assertThat(((Number) repo.getTopicById(TENANT_A, ids.get(0)).get().get("doc_count")).intValue())
            .as("trigger computes exact count over the batched multi-row insert")
            .isEqualTo(50);
        assertThat(repo.getTopicDocIds(TENANT_A, ids.get(0), 0))
            .as("all 50 assignments present")
            .hasSize(50);
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    /** Build a {@code Map<String,Object>} from alternating key/value varargs (mixed value types). */
    private static Map<String, Object> m(Object... kv) {
        var map = new java.util.LinkedHashMap<String, Object>();
        for (int i = 0; i < kv.length; i += 2) map.put((String) kv[i], kv[i + 1]);
        return map;
    }

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.addDataSourceProperty("options", "-c search_path=nexus,public");
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

}
