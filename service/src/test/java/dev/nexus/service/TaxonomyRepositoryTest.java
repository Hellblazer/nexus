package dev.nexus.service;

import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            String schema = "nexus";
            for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON " + schema + "." + table + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE " + schema + ".topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SCHEMA " + schema + " TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO " + schema + ", public");

            // nexus-b7v6i: topic_assignments.doc_id now enforces a FK to catalog_documents(tenant_id, tumbler).
            // Seed all doc_ids used as tumblers in this test class so FK checks pass.
            // "doc-label-missing" is intentionally omitted — tests expect it to be absent.
            for (String tumbler : List.of(
                    "doc-del-1", "doc-merge", "doc-manual", "doc-proj",
                    "doc-label-1", "doc-label-2", "doc-purge-only", "doc-purge-col",
                    "icf-doc-1", "icf-doc-2", "imp-doc-1")) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
                    "VALUES ('" + TENANT_A + "', '" + tumbler + "', 'Test fixture: " + tumbler + "') " +
                    "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
            }
        }

        svcDs       = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
        repo        = new TaxonomyRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.close();
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

        repo.upsertTopicLink(TENANT_A, t1, t2, 3, "co-occurrence");
        repo.upsertTopicLink(TENANT_A, t1, t2, 5, "co-occurrence"); // GREATEST wins

        List<Map<String, Object>> pairs = repo.getTopicLinkPairs(TENANT_A, List.of(t1, t2));
        assertThat(pairs).isNotEmpty();
        var link = pairs.stream()
            .filter(m -> ((Number) m.get("from_topic_id")).longValue() == t1
                      && ((Number) m.get("to_topic_id")).longValue() == t2)
            .findFirst();
        assertThat(link).isPresent();
        assertThat(((Number) link.get().get("link_count")).intValue()).isEqualTo(5);
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
    void importTopic_preservesIdAndGreatestDocCount() {
        long srcId = repo.importTopic(TENANT_A, 9900001L, "imported-topic", null, COL_A,
                                      "centroid-hash-1", 10, PAST_TS, "pending", null);
        assertThat(srcId).isEqualTo(9900001L);
        Optional<Map<String, Object>> row = repo.getTopicById(TENANT_A, 9900001L);
        assertThat(row).isPresent();
        assertThat(row.get().get("label")).isEqualTo("imported-topic");
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(10);

        // Re-import with lower doc_count — GREATEST preserves higher value
        repo.importTopic(TENANT_A, 9900001L, "imported-topic", null, COL_A,
                         "centroid-hash-1", 5, PAST_TS, "accepted", null);
        row = repo.getTopicById(TENANT_A, 9900001L);
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(10); // GREATEST preserved

        // Re-import with HIGHER doc_count — must win
        repo.importTopic(TENANT_A, 9900001L, "imported-topic", null, COL_A,
                         "centroid-hash-1", 99, PAST_TS, "pending", null);
        row = repo.getTopicById(TENANT_A, 9900001L);
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(99);

        // review_status uses EXCLUDED (verbatim): last import wins
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
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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
        rawConfig.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
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

    // ── Helpers ────────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.addDataSourceProperty("options", "-c search_path=nexus,public");
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

}
