package dev.nexus.service;

import dev.nexus.service.db.CatalogIdentityConflictException;
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

    /** Deterministic 64-lowercase-hex chash from a readable seed (RDR-194 P3c:
     *  topic_assignments.doc_id is bytea now, requiring real hex). */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** RDR-194 P3d (nexus-tk070.p3d): seed a real nexus.chunks row (and its
     *  nexus.catalog_collections parent) so a topic_assignments write for
     *  (tenant, collection, chashHex) satisfies the composite
     *  topic_assignments_chunk_fk FOREIGN KEY (tenant_id, source_collection,
     *  doc_id) REFERENCES nexus.chunks (tenant_id, collection, chash). This
     *  class's tests are pure taxonomy-repository-logic tests, independent of
     *  real content — the chunk row here is a MINIMAL FK-satisfying stub
     *  (one non-null embedding column, per chunks' own exactly_one_embedding
     *  CHECK), not a content fixture. Idempotent (ON CONFLICT DO NOTHING on
     *  both inserts) so callers can seed the same (tenant, collection,
     *  chashHex) tuple more than once across a test's several assignments. */
    private void seedChunk(String tenant, String collection, String chashHex) {
        StringBuilder zeroVec = new StringBuilder("[");
        for (int i = 0; i < 384; i++) {
            if (i > 0) zeroVec.append(',');
            zeroVec.append('0');
        }
        zeroVec.append(']');
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + tenant + "', '" + collection + "') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                + "VALUES ('" + tenant + "', '" + collection + "', decode('" + chashHex + "', 'hex'), "
                + "'seed', '" + zeroVec + "'::vector) ON CONFLICT DO NOTHING");
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

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
                // UPDATE mirrors taxonomy-005 (production grants nexus_svc UPDATE on
                // this one sequence so the fidelity import can setval past imported
                // ids — g37fr FINDING 2); SELECT for the GREATEST(last_value, ...).
                "GRANT USAGE, SELECT, UPDATE ON SEQUENCE " + schema + ".topics_id_seq TO " + SVC_ROLE);
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
                    "disc-doc-1", "disc-doc-2",
                    // nexus-71988 assignMany fixtures
                    "am-doc-1", "am-doc-2", "am-doc-dup", "am-doc-proj")) {
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
        seedChunk(TENANT_A, COL_A, hexChash("doc-del-1"));
        repo.assignTopic(TENANT_A, hexChash("doc-del-1"), topicId, "manual", null, COL_A, null);

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
        seedChunk(TENANT_A, COL_A, hexChash("doc-merge"));
        repo.assignTopic(TENANT_A, hexChash("doc-merge"), src, "projection", 0.8, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("doc-merge"), tgt, "projection", 0.9, COL_A, null);

        Optional<String> col = repo.mergeTopics(TENANT_A, src, tgt);
        assertThat(col).isPresent().contains(COL_A);

        // src must be gone
        assertThat(repo.getTopicById(TENANT_A, src)).isEmpty();

        // tgt should still have the doc, with max similarity preserved (0.9)
        List<String> docIds = repo.getTopicDocIds(TENANT_A, tgt, 0);
        assertThat(docIds).contains(hexChash("doc-merge"));
    }

    // ── Assignments ────────────────────────────────────────────────────────────

    @Test @Order(12)
    void assignTopic_nonProjection_insertOrIgnore() {
        long topicId = repo.insertTopic(TENANT_A, "assign-manual-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("doc-manual"));
        repo.assignTopic(TENANT_A, hexChash("doc-manual"), topicId, "manual", null, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("doc-manual"), topicId, "manual", null, COL_A, null); // idempotent

        List<String> docs = repo.getTopicDocIds(TENANT_A, topicId, 0);
        assertThat(docs).containsExactly(hexChash("doc-manual"));
    }

    @Test @Order(13)
    void assignTopic_projection_greatestSimilarity() {
        long topicId = repo.insertTopic(TENANT_A, "assign-proj-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("doc-proj"));
        repo.assignTopic(TENANT_A, hexChash("doc-proj"), topicId, "projection", 0.5, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("doc-proj"), topicId, "projection", 0.8, COL_A, null); // higher wins
        repo.assignTopic(TENANT_A, hexChash("doc-proj"), topicId, "projection", 0.3, COL_A, null); // lower ignored

        List<String> docs = repo.getTopicDocIds(TENANT_A, topicId, 0);
        assertThat(docs).containsExactly(hexChash("doc-proj"));

        // Verify the max sim row is what we get via chunkGroundedIn
        Optional<Double> sim = repo.chunkGroundedIn(TENANT_A, hexChash("doc-proj"), COL_A);
        assertThat(sim).isPresent();
        assertThat(sim.get()).isEqualTo(0.8, offset(0.001));
    }

    @Test @Order(14)
    void getAssignmentsForDocs_andByLabel() {
        long topicId = repo.insertTopic(TENANT_A, "label-search-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("doc-label-1"));
        seedChunk(TENANT_A, COL_A, hexChash("doc-label-2"));
        repo.assignTopic(TENANT_A, hexChash("doc-label-1"), topicId, "manual", null, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("doc-label-2"), topicId, "manual", null, COL_A, null);

        List<Map<String, Object>> assignments = repo.getAssignmentsForDocs(
            TENANT_A, List.of(hexChash("doc-label-1"), hexChash("doc-label-2"), hexChash("doc-label-missing")));
        assertThat(assignments).hasSizeGreaterThanOrEqualTo(2);

        List<String> byLabel = repo.getDocIdsForLabel(TENANT_A, "label-search-topic");
        assertThat(byLabel).containsExactlyInAnyOrder(hexChash("doc-label-1"), hexChash("doc-label-2"));
    }

    @Test @Order(15)
    void purgeAssignmentsForDoc_removesEmptyTopics() {
        long topicId = repo.insertTopic(TENANT_A, "purge-only-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("doc-purge-only"));
        repo.assignTopic(TENANT_A, hexChash("doc-purge-only"), topicId, "manual", null, COL_A, null);

        int removed = repo.purgeAssignmentsForDoc(TENANT_A, COL_A, hexChash("doc-purge-only"));
        assertThat(removed).isEqualTo(1);

        // Empty topic must be pruned
        assertThat(repo.getTopicById(TENANT_A, topicId)).isEmpty();
    }

    /**
     * RDR-194 P3c (nexus-tk070.p3c), found by {@code tests/test_t2.py::test_delete}
     * going red: {@code T2Database.delete}'s memory-to-taxonomy cascade
     * ({@code src/nexus/db/t2/__init__.py:556}) calls this method UNCONDITIONALLY
     * on every memory delete with the memory entry's own TITLE (e.g.
     * {@code "doomed.md"}), never a chash, on the defensive theory that a
     * topic_assignments row might reference it — a theory D1's own finding makes
     * structurally impossible (every live writer emits a 64-hex chunk chash; no
     * row can EVER have a non-canonical doc_id). Pre-P3c this was a harmless
     * TEXT no-op; post-P3c a naive {@code docIdBytes(title)} would throw and
     * abort the caller's ENTIRE memory delete. Falsifiable: removing the shape
     * guard in {@link TaxonomyRepository#purgeAssignmentsForDoc} makes this throw
     * {@code IllegalArgumentException} instead of returning 0.
     */
    @Test @Order(151)
    void purgeAssignmentsForDoc_nonHexTitle_isNoOpNotException() {
        // Empty-topics sweep must still run for a non-matching title (matches
        // this method's pre-P3c behavior: unconditional, not gated on removed > 0).
        long staleTopicId = repo.insertTopic(TENANT_A, "purge-nonhex-stale", null, COL_A, 0, null, null);

        int removed = repo.purgeAssignmentsForDoc(TENANT_A, COL_A, "doomed.md");
        assertThat(removed).as("a non-hex title structurally cannot match any doc_id row").isEqualTo(0);
        assertThat(repo.getTopicById(TENANT_A, staleTopicId))
            .as("the empty-topics sweep still runs for a non-matching title")
            .isEmpty();
    }

    // ── Collection ops ─────────────────────────────────────────────────────────

    @Test @Order(16)
    void purgeCollection_removesAllRows() {
        String tempCol = "knowledge__purge-temp";
        long id = repo.insertTopic(TENANT_A, "purge-col-topic", null, tempCol, 0, null, null);
        seedChunk(TENANT_A, tempCol, hexChash("doc-purge-col"));
        repo.assignTopic(TENANT_A, hexChash("doc-purge-col"), id, "manual", null, tempCol, null);
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

    @Test @Order(191)
    void linkDrift_mirrorsRefreshProjectionLinksPredicate() {
        // nexus-ypori. The audit and the materializer must agree on what
        // "linkable" means. They did not: the client-side check required the
        // co-occurring partner to BE projection while refreshProjectionLinks
        // requires it to be NON-projection, so it reported 50 unlinkable
        // topics as drift and suppressed the 2 real ones. These three shapes
        // are the whole contract.
        long drifted   = repo.insertTopic(TENANT_A, "drift-proj", null, COL_A, 0, null, null);
        long partner   = repo.insertTopic(TENANT_A, "drift-centroid", null, COL_A, 0, null, null);
        long bothProj1 = repo.insertTopic(TENANT_A, "bothproj-1", null, COL_A, 0, null, null);
        long bothProj2 = repo.insertTopic(TENANT_A, "bothproj-2", null, COL_A, 0, null, null);
        long lonely    = repo.insertTopic(TENANT_A, "lonely-proj", null, COL_A, 0, null, null);

        // (1) LINKABLE: projection + non-projection on one doc, no link row.
        seedChunk(TENANT_A, COL_A, hexChash("doc-drift"));
        repo.assignTopic(TENANT_A, hexChash("doc-drift"), drifted, "projection", 0.9, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("doc-drift"), partner, "centroid",   0.8, COL_A, null);

        // (2) NOT linkable: two projection assignments produce no link at all.
        seedChunk(TENANT_A, COL_A, hexChash("doc-bothproj"));
        repo.assignTopic(TENANT_A, hexChash("doc-bothproj"), bothProj1, "projection", 0.9, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("doc-bothproj"), bothProj2, "projection", 0.8, COL_A, null);

        // (3) NOT linkable: a lone assignment has nothing to pair with.
        seedChunk(TENANT_A, COL_A, hexChash("doc-lonely"));
        repo.assignTopic(TENANT_A, hexChash("doc-lonely"), lonely, "projection", 0.7, COL_A, null);

        var report = repo.linkDrift(TENANT_A, 50);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) report.get("rows");
        List<Long> ids = rows.stream()
            .map(m -> ((Number) m.get("topic_id")).longValue()).toList();

        assertThat(ids).contains(drifted);
        assertThat(ids).doesNotContain(bothProj1, bothProj2, lonely);

        // and once the link exists, the drift clears
        // nexus-cefa1.6: link_types is jsonb now — every literal below is a
        // valid JSON array (previously a bare unquoted string, tolerated
        // only because the column was untyped TEXT).
        repo.upsertTopicLink(TENANT_A, drifted, partner, 1, "[\"projection\"]");
        var after = repo.linkDrift(TENANT_A, 50);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> afterRows = (List<Map<String, Object>>) after.get("rows");
        assertThat(afterRows.stream()
            .map(m -> ((Number) m.get("topic_id")).longValue()).toList())
            .doesNotContain(drifted);
    }

    @Test @Order(192)
    void linkDrift_countIsExactWhileRowsAreCapped() {
        // drift_count must not be len(rows): an operator who caps the payload
        // still needs to know the true blast radius.
        long partner = repo.insertTopic(TENANT_A, "cap-partner", null, COL_A, 0, null, null);
        for (int i = 0; i < 4; i++) {
            long t = repo.insertTopic(TENANT_A, "cap-" + i, null, COL_A, 0, null, null);
            seedChunk(TENANT_A, COL_A, hexChash("doc-cap-" + i));
            repo.assignTopic(TENANT_A, hexChash("doc-cap-" + i), t, "projection", 0.9, COL_A, null);
            repo.assignTopic(TENANT_A, hexChash("doc-cap-" + i), partner, "centroid", 0.8, COL_A, null);
        }
        var report = repo.linkDrift(TENANT_A, 2);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) report.get("rows");
        assertThat(rows).hasSize(2);
        assertThat(((Number) report.get("drift_count")).intValue()).isGreaterThanOrEqualTo(4);
    }

    @Test @Order(19)
    void upsertAndGetTopicLinks() {
        long t1 = repo.insertTopic(TENANT_A, "link-topic-1", null, COL_A, 0, null, null);
        long t2 = repo.insertTopic(TENANT_A, "link-topic-2", null, COL_A, 0, null, null);

        // upsertTopicLink is the LIVE-COMPUTE path: EXCLUDED (overwrite), NOT
        // GREATEST. A decremented recompute must lower the stored count (RDR-152
        // nexus-1di3r.4). Contrast importTopicLink (ETL) below, which keeps GREATEST.
        repo.upsertTopicLink(TENANT_A, t1, t2, 5, "[\"co-occurrence\"]");
        repo.upsertTopicLink(TENANT_A, t1, t2, 3, "[\"co-occurrence\"]"); // EXCLUDED overwrites -> 3

        List<Map<String, Object>> pairs = repo.getTopicLinkPairs(TENANT_A, List.of(t1, t2));
        assertThat(pairs).isNotEmpty();
        var link = pairs.stream()
            .filter(m -> ((Number) m.get("from_topic_id")).longValue() == t1
                      && ((Number) m.get("to_topic_id")).longValue() == t2)
            .findFirst();
        assertThat(link).isPresent();
        assertThat(((Number) link.get().get("link_count")).intValue()).isEqualTo(3);
    }

    /**
     * nexus-cefa1.6: link_types is jsonb now (taxonomy-008-link-types-jsonb.xml)
     * — dedicated coverage for the documented behaviour change, mirroring
     * PlanRepositoryTest.savePlan_planJson_jsonbCanonicalizesWhitespaceAndKeyOrder
     * (nexus-cefa1.5). link_types holds a JSON ARRAY, not an object: PostgreSQL's
     * jsonb canonicalization only reorders OBJECT keys (shorter-first, ties
     * broken bytewise) — array ELEMENT ORDER is preserved verbatim. Whitespace
     * still normalizes to the canonical form (a space after every comma)
     * regardless of what was submitted. {@code getTopicLinkPairs} never
     * projects link_types, so this reads the raw column back via JDBC.
     */
    @Test @Order(193)
    void upsertTopicLink_linkTypesJsonbCanonicalizesWhitespace_arrayOrderPreserved() throws Exception {
        long t1 = repo.insertTopic(TENANT_A, "canon-link-t1", null, COL_A, 0, null, null);
        long t2 = repo.insertTopic(TENANT_A, "canon-link-t2", null, COL_A, 0, null, null);

        // Non-canonical spacing (no space after the comma); "zeta" written before
        // "alpha" — arrays are not reordered the way object keys are.
        repo.upsertTopicLink(TENANT_A, t1, t2, 1, "[\"zeta\",\"alpha\"]");

        try (java.sql.Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT link_types::text AS lt FROM nexus.topic_links "
                + "WHERE tenant_id = '" + TENANT_A + "' "
                + "AND from_topic_id = " + t1 + " AND to_topic_id = " + t2);
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("lt"))
                .as("jsonb array canonical text inserts a space after each comma but "
                    + "preserves element order — zeta stays before alpha")
                .isEqualTo("[\"zeta\", \"alpha\"]");
        }
    }

    /**
     * nexus-cefa1.6: {@code refreshProjectionLinks}'s string-merge algorithm
     * (splice "projection" into an existing link_types JSON array via crude
     * bracket-replace string surgery) is UNCHANGED by the jsonb conversion —
     * only the plumbing that gets a Java String in and a JSONB out changed
     * (JSONB.data() unwrap on read, jsonbRequired wrap on write). This proves
     * the merge still lands both types when a cooccurrence link already
     * exists and a projection assignment later makes the same pair eligible
     * for refreshProjectionLinks.
     */
    @Test @Order(194)
    void refreshProjectionLinks_mergesIntoExistingLinkTypes_preservingBothTypes() throws Exception {
        long t1 = repo.insertTopic(TENANT_A, "merge-link-t1", null, COL_A, 0, null, null);
        long t2 = repo.insertTopic(TENANT_A, "merge-link-t2", null, COL_A, 0, null, null);

        // Seed an existing cooccurrence-only link (as generateCooccurrenceLinks would).
        repo.upsertTopicLink(TENANT_A, t1, t2, 1, "[\"cooccurrence\"]");

        // Make (t1, t2) eligible for refreshProjectionLinks: t1 gets a projection
        // assignment on a doc t2 also holds via a non-projection assigned_by.
        seedChunk(TENANT_A, COL_A, hexChash("merge-doc"));
        repo.assignTopic(TENANT_A, hexChash("merge-doc"), t1, "projection", 0.9, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("merge-doc"), t2, "hdbscan", 0.8, COL_A, null);

        repo.refreshProjectionLinks(TENANT_A);

        try (java.sql.Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT link_types::text AS lt FROM nexus.topic_links "
                + "WHERE tenant_id = '" + TENANT_A + "' "
                + "AND from_topic_id = " + Math.min(t1, t2) + " AND to_topic_id = " + Math.max(t1, t2));
            assertThat(rs.next()).isTrue();
            String lt = rs.getString("lt");
            assertThat(lt).as("original cooccurrence type must survive the merge").contains("cooccurrence");
            assertThat(lt).as("projection type must be spliced in by the merge").contains("projection");
        }
    }

    // ── ICF ────────────────────────────────────────────────────────────────────

    @Test @Order(20)
    void icf_sourceCountAndRows() {
        String srcColA = "src__col-a-icf";
        String srcColB = "src__col-b-icf";
        long topic = repo.insertTopic(TENANT_A, "icf-test-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, srcColA, hexChash("icf-doc-1"));
        seedChunk(TENANT_A, srcColB, hexChash("icf-doc-2"));
        repo.assignTopic(TENANT_A, hexChash("icf-doc-1"), topic, "projection", 0.8, srcColA, null);
        repo.assignTopic(TENANT_A, hexChash("icf-doc-2"), topic, "projection", 0.7, srcColB, null);

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

    @Test @Order(215)
    void importTopic_advancesIdSequence_noCollisionOnNextSerialInsert() {
        // g37fr FINDING 2 (RDR-155 P4b, engine v0.1.53): fidelity import
        // preserves the source id verbatim but must ALSO advance the topics
        // BIGSERIAL sequence past it — otherwise the next live serial INSERT
        // (persist_rebuild) collides 409 on a shared store. Standard PG
        // import discipline: setval(pg_get_serial_sequence, GREATEST(...))
        // inside the import transaction.
        long liveId = repo.insertTopic(TENANT_A, "seq-live-probe", null, COL_A, 0, null, null);
        long importedId = liveId + 1; // exactly the sequence's next value pre-fix
        repo.importTopic(TENANT_A, importedId, "seq-imported", null, COL_A,
                         "centroid-seq", 0, PAST_TS, "pending", null);
        long nextId = repo.insertTopic(TENANT_A, "seq-after-import", null, COL_A, 0, null, null);
        assertThat(nextId)
            .as("serial insert after fidelity import must not collide with the imported id")
            .isGreaterThan(importedId);
    }

    @Test @Order(216)
    void importBatch_topics_advancesIdSequence() {
        // Batch twin of the sequence-advance discipline (same 409 class).
        long liveId = repo.insertTopic(TENANT_A, "seq-batch-probe", null, COL_A, 0, null, null);
        long importedId = liveId + 1;
        repo.importBatch(TENANT_A, "topic", List.of(Map.of(
            "id", importedId, "label", "seq-batch-imported", "collection", COL_A,
            "created_at", PAST_TS, "doc_count", 0, "review_status", "pending")));
        long nextId = repo.insertTopic(TENANT_A, "seq-batch-after", null, COL_A, 0, null, null);
        assertThat(nextId)
            .as("serial insert after batch fidelity import must not collide")
            .isGreaterThan(importedId);
    }

    // ── nexus-q2ign: topics ON CONFLICT arbiter completeness ────────────────────

    @Test @Order(217)
    void importTopic_refusesRootLabelConflictAtDifferentId() {
        // Sequential double-write repro of the unhandled 23505: two fidelity
        // imports at DIFFERENT ids both claiming the same (collection, label)
        // ROOT topic identity. ON CONFLICT (TOPICS.ID) cannot see this — it is
        // a genuinely different key — so pre-fix this raised a raw PSQLException
        // (23505 on idx_topics_root_tenant_collection_label) instead of the
        // typed refusal.
        final String col = "knowledge__q2ign_dup";
        repo.importTopic(TENANT_A, 9910001L, "q2ign-dup-label", null, col,
                         null, 0, PAST_TS, "pending", null);

        assertThatThrownBy(() -> repo.importTopic(TENANT_A, 9910002L, "q2ign-dup-label", null, col,
                                                   null, 0, PAST_TS, "pending", null))
            .as("a second id claiming the same live root-topic identity must be REFUSED, "
                + "not silently merged and not a raw 23505")
            .isInstanceOf(CatalogIdentityConflictException.class)
            .satisfies(e -> assertThat(((CatalogIdentityConflictException) e).constraint())
                .isEqualTo("idx_topics_root_tenant_collection_label"));

        // The refused write must not have landed — exactly one row for this identity.
        assertThat(repo.getAllTopics(TENANT_A, col)).hasSize(1);
        assertThat(repo.getTopicById(TENANT_A, 9910002L)).isEmpty();
    }

    @Test @Order(218)
    void importTopic_sameIdReimport_sameLabelIsNotRefused() {
        // The idempotent ETL re-run case (already covered functionally by
        // importTopic_preservesId_docCountNotEtlMerged above) must stay
        // unaffected by the new guard: re-importing the SAME id is a
        // convergent no-op regardless of the root-label key.
        final String col = "knowledge__q2ign_same_id";
        repo.importTopic(TENANT_A, 9910010L, "q2ign-same-id", null, col,
                         null, 0, PAST_TS, "pending", null);
        assertThatCode(() -> repo.importTopic(TENANT_A, 9910010L, "q2ign-same-id", null, col,
                                              "new-centroid", 0, PAST_TS, "accepted", null))
            .doesNotThrowAnyException();
        assertThat(repo.getTopicById(TENANT_A, 9910010L).get().get("centroid_hash"))
            .isEqualTo("new-centroid");
    }

    @Test @Order(219)
    void importTopic_childTopicNotGovernedByRootLabelKey() {
        // parent_id NOT NULL is outside idx_topics_root_tenant_collection_label's
        // predicate entirely — two children may legitimately share a label under
        // the same parent (SQLite never enforced uniqueness there either).
        final String col = "knowledge__q2ign_child";
        long parent = repo.importTopic(TENANT_A, 9910020L, "q2ign-parent", null, col,
                                       null, 0, PAST_TS, "pending", null);
        repo.importTopic(TENANT_A, 9910021L, "q2ign-child-dup", parent, col,
                         null, 0, PAST_TS, "pending", null);
        assertThatCode(() -> repo.importTopic(TENANT_A, 9910022L, "q2ign-child-dup", parent, col,
                                              null, 0, PAST_TS, "pending", null))
            .as("child topics (parent_id NOT NULL) carry no root-label identity to guard")
            .doesNotThrowAnyException();
        assertThat(repo.getChildTopics(TENANT_A, parent)).hasSize(2);
    }

    @Test @Order(220)
    void importTopicsBatch_refusesRootLabelConflictAtDifferentId() {
        // Batch twin of Order(217) — the multi-row ON CONFLICT (TOPICS.ID)
        // form is not exempt from the key its arbiter omits.
        final String col = "knowledge__q2ign_batch_dup";
        repo.importBatch(TENANT_A, "topic", List.of(m(
            "id", 9910030L, "label", "q2ign-batch-dup", "collection", col,
            "created_at", PAST_TS, "doc_count", 0, "review_status", "pending")));

        assertThatThrownBy(() -> repo.importBatch(TENANT_A, "topic", List.of(m(
                "id", 9910031L, "label", "q2ign-batch-dup", "collection", col,
                "created_at", PAST_TS, "doc_count", 0, "review_status", "pending"))))
            .as("batch import must refuse the same identity conflict as the single-row path")
            .isInstanceOf(CatalogIdentityConflictException.class);

        assertThat(repo.getAllTopics(TENANT_A, col)).hasSize(1);
    }

    @Test @Order(22)
    void importAssignment_fidelityAndIdempotent() {
        long topicId = repo.importTopic(TENANT_A, 9900002L, "assign-import-topic", null, COL_A,
                                        null, 0, PAST_TS, "pending", null);
        seedChunk(TENANT_A, COL_A, hexChash("imp-doc-1"));
        repo.importAssignment(TENANT_A, hexChash("imp-doc-1"), topicId, "projection", 0.7, PAST_TS, COL_A);

        List<String> docs = repo.getTopicDocIds(TENANT_A, topicId, 0);
        assertThat(docs).contains(hexChash("imp-doc-1"));

        // Re-import with same data — idempotent (GREATEST similarity)
        repo.importAssignment(TENANT_A, hexChash("imp-doc-1"), topicId, "projection", 0.7, PAST_TS, COL_A);
        assertThat(repo.getTopicDocIds(TENANT_A, topicId, 0)).containsExactly(hexChash("imp-doc-1"));
    }

    @Test @Order(23)
    void importTopicLink_fidelityAndGreatestLinkCount() {
        long t1 = repo.importTopic(TENANT_A, 9900003L, "link-import-t1", null, COL_A,
                                   null, 0, PAST_TS, "pending", null);
        long t2 = repo.importTopic(TENANT_A, 9900004L, "link-import-t2", null, COL_A,
                                   null, 0, PAST_TS, "pending", null);

        repo.importTopicLink(TENANT_A, t1, t2, 7, "[\"co-occur\"]");
        repo.importTopicLink(TENANT_A, t1, t2, 3, "[\"co-occur\"]"); // GREATEST 7 preserved

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
        seedChunk(TENANT_A, COL_OS, hexChash("os-doc-manual"));
        seedChunk(TENANT_A, COL_OS, hexChash("os-doc-hdbscan"));
        repo.assignTopic(TENANT_A, hexChash("os-doc-manual"), t1, "manual", null, COL_OS, null);
        repo.assignTopic(TENANT_A, hexChash("os-doc-hdbscan"), t1, "hdbscan", null, COL_OS, null);

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
        assertThat(manual.get(0).get("doc_id")).isEqualTo(hexChash("os-doc-manual"));
        assertThat(((Number) manual.get(0).get("topic_id")).longValue()).isEqualTo(t1);
    }

    @SuppressWarnings("unchecked")
    @Test @Order(29)
    void persistRebuildTopics_replaceSemanticsClearsOldInsertsNewAppliesManual() {
        // Seed an "old" topic + assignment that the rebuild must clear.
        long oldId = repo.insertTopic(TENANT_A, "rb-old", null, COL_RB, 1, PAST_TS, null);
        seedChunk(TENANT_A, COL_RB, hexChash("rb-doc-1"));
        seedChunk(TENANT_A, COL_RB, hexChash("rb-doc-2"));
        seedChunk(TENANT_A, COL_RB, hexChash("rb-doc-manual"));
        repo.assignTopic(TENANT_A, hexChash("rb-doc-1"), oldId, "hdbscan", null, COL_RB, null);

        var specs = List.of(
            m("label", "rb-new-0", "doc_count", 2, "terms", "[\"x\"]",
              "review_status", "pending", "assigned_by", "hdbscan",
              "doc_ids", List.of(hexChash("rb-doc-1"), hexChash("rb-doc-2"))),
            m("label", "rb-new-1", "doc_count", 0, "terms", "[\"y\"]",
              "review_status", "pending", "assigned_by", "hdbscan",
              "doc_ids", List.of()));
        // Transfer the manual doc to spec index 1.
        Map<String, Object> manualTransfers = m(hexChash("rb-doc-manual"), 1);

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
        assertThat(manual.get(0).get("doc_id")).isEqualTo(hexChash("rb-doc-manual"));
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
        seedChunk(TENANT_A, COL_DISC, hexChash("disc-doc-1"));
        seedChunk(TENANT_A, COL_DISC, hexChash("disc-doc-2"));
        var specs = List.of(
            m("label", "disc-0", "doc_count", 2, "terms", "[\"p\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of(hexChash("disc-doc-1"), hexChash("disc-doc-2"))),
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
            .containsExactlyInAnyOrder(hexChash("disc-doc-1"), hexChash("disc-doc-2"));
    }

    @Test @Order(315)
    void persistDiscoveredTopics_concurrentSameCollection_oneWinsOtherSkips() throws Exception {
        // nexus-n2ls1 regression: the existing-topics guard is a plain SELECT
        // COUNT — pre-fix, two concurrent persists for the same collection both
        // counted 0, both inserted the same root label, and the loser hit the
        // taxonomy-004 partial unique index (23505 → HTTP 409, observed live
        // 2026-07-07). The per-collection pg_advisory_xact_lock serializes them:
        // the loser waits, then guard-skips cleanly. Barrier-start both threads
        // for maximal overlap; assert NO exception, exactly one winner, and
        // exactly the winner's rows in the DB.
        final String col = "docs__disc_race__bge-base-en-v15-768__v1";
        seedChunk(TENANT_A, col, hexChash("race-doc-1"));
        var specs = List.of(
            m("label", "race-topic-a", "doc_count", 1, "terms", "[\"r\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of(hexChash("race-doc-1"))),
            m("label", "race-topic-b", "doc_count", 0, "terms", "[\"s\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of()));

        var barrier = new java.util.concurrent.CyclicBarrier(2);
        var results = new java.util.concurrent.ConcurrentHashMap<String, List<Long>>();
        var failures = new java.util.concurrent.CopyOnWriteArrayList<Throwable>();
        Runnable persist = () -> {
            try {
                barrier.await(10, java.util.concurrent.TimeUnit.SECONDS);
                results.put(Thread.currentThread().getName(),
                            repo.persistDiscoveredTopics(TENANT_A, col, specs));
            } catch (Throwable t) {
                failures.add(t);
            }
        };
        Thread t1 = new Thread(persist, "race-1");
        Thread t2 = new Thread(persist, "race-2");
        t1.start(); t2.start();
        t1.join(30_000); t2.join(30_000);

        assertThat(failures)
            .withFailMessage("concurrent persist_discovered raised: %s", failures)
            .isEmpty();
        var sizes = results.values().stream().map(List::size).sorted().toList();
        // One thread inserted both specs; the other guard-skipped after waiting
        // on the advisory lock.
        assertThat(sizes).containsExactly(0, 2);
        assertThat(repo.getAllTopics(TENANT_A, col)).hasSize(2);
    }

    @Test @Order(316)
    void persistDiscoveredTopics_inBatchDuplicateLabelReusesTopicId() {
        // nexus-n2ls1 defense-in-depth: the nexus client dedups labels before
        // POSTing, but the server must not 23505→409 when a raw client sends
        // two specs with the same label. The ON CONFLICT DO NOTHING belt skips
        // the second insert and reuses the first topic's id, keeping topic_ids
        // aligned with specs order; assignments union onto the shared topic.
        final String col = "docs__disc_duplabel__bge-base-en-v15-768__v1";
        seedChunk(TENANT_A, col, hexChash("dup-doc-1"));
        seedChunk(TENANT_A, col, hexChash("dup-doc-2"));
        var specs = List.of(
            m("label", "dup-topic", "doc_count", 1, "terms", "[\"t\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of(hexChash("dup-doc-1"))),
            m("label", "dup-topic", "doc_count", 1, "terms", "[\"u\"]",
              "assigned_by", "hdbscan", "doc_ids", List.of(hexChash("dup-doc-2"))));

        List<Long> ids = repo.persistDiscoveredTopics(TENANT_A, col, specs);

        assertThat(ids).hasSize(2);
        assertThat(ids.get(0)).isEqualTo(ids.get(1));
        var topics = repo.getAllTopics(TENANT_A, col);
        assertThat(topics).hasSize(1);
        // First spec wins the row — the losing spec's terms are deliberately
        // dropped (documented behavior, matches the client-side dedup).
        assertThat(topics.get(0).get("terms")).isEqualTo("[\"t\"]");
        assertThat(repo.getTopicDocIds(TENANT_A, ids.get(0), 0))
            .containsExactlyInAnyOrder(hexChash("dup-doc-1"), hexChash("dup-doc-2"));
    }

    @Test @Order(317)
    void persistRebuildTopics_inBatchDuplicateLabelReusesTopicId() {
        // nexus-n2ls1 critique M2: rebuild inserts root topics behind the same
        // taxonomy-004 partial unique index; a raw client sending duplicate
        // labels in one rebuild plan must merge (first wins, doc_ids union),
        // not 23505 → 409.
        final String col = "docs__rb_duplabel__bge-base-en-v15-768__v1";
        seedChunk(TENANT_A, col, hexChash("rb-dup-doc-1"));
        seedChunk(TENANT_A, col, hexChash("rb-dup-doc-2"));
        var specs = List.of(
            m("label", "rb-dup", "doc_count", 1, "terms", "[\"a\"]",
              "review_status", "pending", "assigned_by", "hdbscan",
              "doc_ids", List.of(hexChash("rb-dup-doc-1"))),
            m("label", "rb-dup", "doc_count", 1, "terms", "[\"b\"]",
              "review_status", "pending", "assigned_by", "hdbscan",
              "doc_ids", List.of(hexChash("rb-dup-doc-2"))));

        List<Long> ids = repo.persistRebuildTopics(TENANT_A, col, specs, Map.of());

        assertThat(ids).hasSize(2);
        assertThat(ids.get(0)).isEqualTo(ids.get(1));
        var topics = repo.getAllTopics(TENANT_A, col);
        assertThat(topics).hasSize(1);
        assertThat(topics.get(0).get("terms")).isEqualTo("[\"a\"]");
        assertThat(repo.getTopicDocIds(TENANT_A, ids.get(0), 0))
            .containsExactlyInAnyOrder(hexChash("rb-dup-doc-1"), hexChash("rb-dup-doc-2"));
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
        seedChunk(TENANT_A, col, hexChash("pd-doc-1"));
        seedChunk(TENANT_A, col, hexChash("pd-doc-2"));
        repo.assignTopic(TENANT_A, hexChash("pd-doc-1"), t, "manual", null, col, null);
        repo.assignTopic(TENANT_A, hexChash("pd-doc-2"), t, "manual", null, col, null);
        // AFTER INSERT trigger set the live count.
        assertThat(((Number) repo.getTopicById(TENANT_A, t).get().get("doc_count")).intValue())
            .isEqualTo(2);

        // Purge one doc's assignment; topic survives (still has pd-doc-2).
        repo.purgeAssignmentsForDoc(TENANT_A, col, hexChash("pd-doc-1"));

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
        seedChunk(TENANT_A, col, hexChash("es-doc-1"));
        seedChunk(TENANT_A, col, hexChash("es-doc-2"));
        seedChunk(TENANT_A, col, hexChash("es-doc-3"));
        repo.assignTopic(TENANT_A, hexChash("es-doc-1"), t, "manual", null, col, null);
        repo.assignTopic(TENANT_A, hexChash("es-doc-2"), t, "manual", null, col, null);
        repo.assignTopic(TENANT_A, hexChash("es-doc-3"), t, "manual", null, col, null);
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
        // topic_assignments_chunk_fk is keyed on the ASSIGNMENT's own tenant_id
        // (TENANT_A here), not the referenced topic's tenant — orthogonal to
        // what this test proves about the doc_count trigger's tenant scoping.
        seedChunk(TENANT_A, col, hexChash("xt-doc-a"));
        repo.assignTopic(TENANT_A, hexChash("xt-doc-a"), bTopicId, "manual", null, col, null);

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
        seedChunk(TENANT_A, col, hexChash("dr-doc-1"));
        seedChunk(TENANT_A, col, hexChash("dr-doc-2"));
        seedChunk(TENANT_A, col, hexChash("dr-doc-3"));
        var specs = List.of(
            m("label", "disc-recount", "doc_count", 999, "terms", "[\"p\"]",
              "assigned_by", "hdbscan",
              "doc_ids", List.of(hexChash("dr-doc-1"), hexChash("dr-doc-2"), hexChash("dr-doc-3"))));
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
        for (int i = 0; i < 50; i++) {
            String d = hexChash("bl-doc-" + i);
            docIds.add(d);
            seedChunk(TENANT_A, col, d);
        }
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

    @Test @Order(45)
    void rootTopicUniqueness_dupRejected_childAndOtherTenantAllowed() {
        // nexus-slcn7: partial unique index on (tenant_id, collection, label)
        // WHERE parent_id IS NULL forbids duplicate ROOT topics, while children
        // and other tenants may reuse the label.
        final String col = "knowledge__uniq";
        long root = repo.insertTopic(TENANT_A, "uniq-label", null, col, 0, PAST_TS, null);
        assertThat(root).isPositive();

        // Duplicate ROOT (same tenant, collection, label) → unique-index violation.
        assertThatThrownBy(() ->
            repo.insertTopic(TENANT_A, "uniq-label", null, col, 0, PAST_TS, null))
            .isInstanceOf(org.jooq.exception.DataAccessException.class);

        // A CHILD topic (parent_id set) with the same label is allowed.
        long child = repo.insertTopic(TENANT_A, "uniq-label", root, col, 0, PAST_TS, null);
        assertThat(child).isPositive();

        // A different tenant may reuse the label.
        long bRoot = repo.insertTopic(TENANT_B, "uniq-label", null, col, 0, PAST_TS, null);
        assertThat(bRoot).isPositive();
    }

    // ── importBatch: ONE multi-row INSERT per kind (nexus-1usso) ────────────────
    // Plan-audit correction on nexus-1usso: importBatch HAD an endpoint but still
    // looped per-row .execute() inside its single tenant transaction (N round-trips).
    // These tests exercise the multi-row conversion for all four kinds plus the
    // intra-batch dedupe a single ON CONFLICT DO UPDATE statement requires.

    @Test @Order(46)
    void importBatch_topic_multiRow_insertsAll_andExcludedMergeOnReimport() {
        long id0 = 9900200L;
        long id1 = 9900201L;
        int n = repo.importBatch(TENANT_A, "topic", List.of(
            m("id", id0, "label", "batch-t0", "collection", "knowledge__batch_topic",
              "centroid_hash", "ch0", "doc_count", 5, "created_at", PAST_TS,
              "review_status", "pending", "terms", "[\"a\"]"),
            m("id", id1, "label", "batch-t1", "collection", "knowledge__batch_topic",
              "centroid_hash", "ch1", "doc_count", 9, "created_at", PAST_TS,
              "review_status", "pending", "terms", "[\"b\"]")));
        assertThat(n).isEqualTo(2);
        assertThat(repo.getTopicById(TENANT_A, id0)).isPresent();
        assertThat(repo.getTopicById(TENANT_A, id1)).isPresent();
        assertThat(((Number) repo.getTopicById(TENANT_A, id0).get().get("doc_count")).intValue()).isEqualTo(5);

        // Re-import (one-row batch) with different review_status/centroid_hash/terms —
        // EXCLUDED merge applies exactly as the single-row importTopic path. doc_count
        // is trigger-maintained and NOT an ETL merge participant — seed of 5 survives.
        repo.importBatch(TENANT_A, "topic", List.of(
            m("id", id0, "label", "batch-t0", "collection", "knowledge__batch_topic",
              "centroid_hash", "ch0-v2", "doc_count", 999, "created_at", PAST_TS,
              "review_status", "accepted", "terms", "[\"a\",\"z\"]")));
        var row = repo.getTopicById(TENANT_A, id0).get();
        assertThat(row.get("review_status")).isEqualTo("accepted");
        assertThat(((Number) row.get("doc_count")).intValue()).isEqualTo(5);
    }

    @Test @Order(47)
    void importBatch_assignment_multiRow_neverDowngradesProjection_greatestSimilarity() {
        long t0 = repo.importTopic(TENANT_A, 9900210L, "batch-assign-t0", null, "knowledge__batch_assign",
                                   null, 0, PAST_TS, "pending", null);
        long t1 = repo.importTopic(TENANT_A, 9900211L, "batch-assign-t1", null, "knowledge__batch_assign",
                                   null, 0, PAST_TS, "pending", null);
        seedChunk(TENANT_A, "knowledge__batch_assign", hexChash("batch-a-doc-1"));
        seedChunk(TENANT_A, "knowledge__batch_assign", hexChash("batch-a-doc-2"));

        int n = repo.importBatch(TENANT_A, "assignment", List.of(
            m("doc_id", hexChash("batch-a-doc-1"), "topic_id", t0, "assigned_by", "projection",
              "similarity", 0.5, "assigned_at", PAST_TS, "source_collection", "knowledge__batch_assign"),
            m("doc_id", hexChash("batch-a-doc-2"), "topic_id", t1, "assigned_by", "manual",
              "similarity", null, "assigned_at", PAST_TS, "source_collection", "knowledge__batch_assign")));
        assertThat(n).isEqualTo(2);
        assertThat(repo.getTopicDocIds(TENANT_A, t0, 0)).contains(hexChash("batch-a-doc-1"));
        assertThat(repo.getTopicDocIds(TENANT_A, t1, 0)).contains(hexChash("batch-a-doc-2"));

        // Re-import same (doc_id, topic_id) with assigned_by='hdbscan' + lower similarity —
        // never downgrade projection, GREATEST similarity.
        repo.importBatch(TENANT_A, "assignment", List.of(
            m("doc_id", hexChash("batch-a-doc-1"), "topic_id", t0, "assigned_by", "hdbscan",
              "similarity", 0.2, "assigned_at", PAST_TS, "source_collection", "knowledge__batch_assign")));
        // chunkGroundedIn only matches assigned_by='projection' rows — if the CASE
        // logic had downgraded assigned_by to 'hdbscan' this would come back empty.
        // GREATEST(0.5, 0.2) also confirms similarity was not clobbered downward.
        assertThat(repo.chunkGroundedIn(TENANT_A, hexChash("batch-a-doc-1"), "knowledge__batch_assign"))
            .contains(0.5);
    }

    @Test @Order(48)
    void importBatch_link_multiRow_greatestLinkCount() {
        long t0 = repo.importTopic(TENANT_A, 9900220L, "batch-link-t0", null, "knowledge__batch_link",
                                   null, 0, PAST_TS, "pending", null);
        long t1 = repo.importTopic(TENANT_A, 9900221L, "batch-link-t1", null, "knowledge__batch_link",
                                   null, 0, PAST_TS, "pending", null);
        long t2 = repo.importTopic(TENANT_A, 9900222L, "batch-link-t2", null, "knowledge__batch_link",
                                   null, 0, PAST_TS, "pending", null);

        int n = repo.importBatch(TENANT_A, "link", List.of(
            m("from_topic_id", t0, "to_topic_id", t1, "link_count", 7, "link_types", "[\"co-occur\"]"),
            m("from_topic_id", t0, "to_topic_id", t2, "link_count", 4, "link_types", "[\"co-occur\"]")));
        assertThat(n).isEqualTo(2);

        var pairs = repo.getTopicLinkPairs(TENANT_A, List.of(t0, t1, t2));
        assertThat(pairs).hasSize(2);

        // Re-import with a LOWER link_count for (t0,t1) — GREATEST(7,3) keeps 7.
        repo.importBatch(TENANT_A, "link", List.of(
            m("from_topic_id", t0, "to_topic_id", t1, "link_count", 3, "link_types", "[\"co-occur\"]")));
        var updated = repo.getTopicLinkPairs(TENANT_A, List.of(t0, t1, t2)).stream()
            .filter(p -> ((Number) p.get("from_topic_id")).longValue() == t0
                      && ((Number) p.get("to_topic_id")).longValue() == t1)
            .findFirst();
        assertThat(updated).isPresent();
        assertThat(((Number) updated.get().get("link_count")).intValue()).isEqualTo(7);
    }

    @Test @Order(49)
    void importBatch_meta_multiRow_distinctCollections_greatestCounters() {
        String colX = "knowledge__batch_meta_x";
        String colY = "knowledge__batch_meta_y";
        int n = repo.importBatch(TENANT_A, "meta", List.of(
            m("collection", colX, "last_discover_doc_count", 50, "last_discover_at", PAST_TS),
            m("collection", colY, "last_discover_doc_count", 12, "last_discover_at", PAST_TS)));
        assertThat(n).isEqualTo(2);
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, colX)).contains(50);
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, colY)).contains(12);

        // Re-import colX with a LOWER count — GREATEST(50,20) keeps 50.
        repo.importBatch(TENANT_A, "meta", List.of(
            m("collection", colX, "last_discover_doc_count", 20, "last_discover_at", PAST_TS)));
        assertThat(repo.getLastDiscoverDocCount(TENANT_A, colX)).contains(50);
    }

    @Test @Order(50)
    void importBatch_topic_intraBatchDuplicate_lastWins_noError() {
        // A single multi-row INSERT ... ON CONFLICT cannot touch the same row
        // twice (PG: "cannot affect row a second time") — the repo must dedupe
        // within the batch, last occurrence winning.
        long id = 9900230L;
        int n = repo.importBatch(TENANT_A, "topic", List.of(
            m("id", id, "label", "dup-a", "collection", "knowledge__batch_dup",
              "centroid_hash", "ch-a", "doc_count", 1, "created_at", PAST_TS,
              "review_status", "pending", "terms", "[\"a\"]"),
            m("id", id, "label", "dup-b", "collection", "knowledge__batch_dup",
              "centroid_hash", "ch-b", "doc_count", 2, "created_at", PAST_TS,
              "review_status", "accepted", "terms", "[\"b\"]")));
        assertThat(n).isEqualTo(2); // rows submitted (contract unchanged), not rows landed
        var row = repo.getTopicById(TENANT_A, id);
        assertThat(row).isPresent();
        assertThat(row.get().get("label")).isEqualTo("dup-b");
        assertThat(((Number) row.get().get("doc_count")).intValue()).isEqualTo(2);
    }

    @Test @Order(51)
    void importBatch_emptyAndNull_returnZero() {
        assertThat(repo.importBatch(TENANT_A, "topic", List.of())).isZero();
        assertThat(repo.importBatch(TENANT_A, "topic", null)).isZero();
    }

    @Test @Order(52)
    void importBatch_unknownKind_throws() {
        assertThatThrownBy(() -> repo.importBatch(TENANT_A, "bogus-kind", List.of(m("id", 1L))))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── assignMany (nexus-71988) ────────────────────────────────────────────────

    @Test @Order(60)
    void assignMany_mixedCentroidAndProjection_matchesSequentialAssignTopic() {
        long tBatch = repo.insertTopic(TENANT_A, "am-batch-topic", null, COL_A, 0, null, null);
        long tSeq   = repo.insertTopic(TENANT_A, "am-seq-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("am-doc-1"));
        seedChunk(TENANT_A, COL_A, hexChash("am-doc-2"));

        int persisted = repo.assignMany(TENANT_A, List.of(
            m("doc_id", hexChash("am-doc-1"), "topic_id", tBatch, "assigned_by", "centroid",
              "source_collection", COL_A),
            m("doc_id", hexChash("am-doc-2"), "topic_id", tBatch, "assigned_by", "projection",
              "similarity", 0.7, "source_collection", COL_A)));
        assertThat(persisted).isEqualTo(2);

        // Equivalent sequence of single-row assignTopic calls to a sibling topic.
        repo.assignTopic(TENANT_A, hexChash("am-doc-1"), tSeq, "centroid", null, COL_A, null);
        repo.assignTopic(TENANT_A, hexChash("am-doc-2"), tSeq, "projection", 0.7, COL_A, null);

        assertThat(repo.getTopicDocIds(TENANT_A, tBatch, 0))
            .containsExactlyInAnyOrder(hexChash("am-doc-1"), hexChash("am-doc-2"));
        assertThat(repo.getTopicDocIds(TENANT_A, tSeq, 0))
            .containsExactlyInAnyOrder(hexChash("am-doc-1"), hexChash("am-doc-2"));

        Optional<Double> sim = repo.chunkGroundedIn(TENANT_A, hexChash("am-doc-2"), COL_A);
        assertThat(sim).isPresent();
        assertThat(sim.get()).isEqualTo(0.7, offset(0.001));
    }

    @Test @Order(61)
    void assignMany_duplicateNonProjectionRowsInBatch_dupSafe() {
        long t = repo.insertTopic(TENANT_A, "am-dup-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("am-doc-dup"));
        // Two identical (doc_id, topic_id) non-projection rows in ONE call: the
        // second hits ON CONFLICT DO NOTHING (separate INSERT statements) — no error.
        int persisted = repo.assignMany(TENANT_A, List.of(
            m("doc_id", hexChash("am-doc-dup"), "topic_id", t, "assigned_by", "centroid",
              "source_collection", COL_A),
            m("doc_id", hexChash("am-doc-dup"), "topic_id", t, "assigned_by", "centroid",
              "source_collection", COL_A)));
        assertThat(persisted).isEqualTo(2);
        assertThat(repo.getTopicDocIds(TENANT_A, t, 0)).containsExactly(hexChash("am-doc-dup"));
    }

    @Test @Order(62)
    void assignMany_projectionBestSimilarityWins_withinBatch() {
        long t = repo.insertTopic(TENANT_A, "am-proj-topic", null, COL_A, 0, null, null);
        seedChunk(TENANT_A, COL_A, hexChash("am-doc-proj"));
        repo.assignMany(TENANT_A, List.of(
            m("doc_id", hexChash("am-doc-proj"), "topic_id", t, "assigned_by", "projection",
              "similarity", 0.4, "source_collection", COL_A),
            m("doc_id", hexChash("am-doc-proj"), "topic_id", t, "assigned_by", "projection",
              "similarity", 0.9, "source_collection", COL_A),
            m("doc_id", hexChash("am-doc-proj"), "topic_id", t, "assigned_by", "projection",
              "similarity", 0.6, "source_collection", COL_A)));

        Optional<Double> sim = repo.chunkGroundedIn(TENANT_A, hexChash("am-doc-proj"), COL_A);
        assertThat(sim).isPresent();
        assertThat(sim.get()).isEqualTo(0.9, offset(0.001));
    }

    @Test @Order(63)
    void assignMany_emptyList_noOp() {
        assertThat(repo.assignMany(TENANT_A, List.of())).isZero();
        assertThat(repo.assignMany(TENANT_A, null)).isZero();
    }

    // ── Hub staleness (nexus-onjvy) ────────────────────────────────────────────

    /**
     * Seed one hub: a topic with projection assignments from two source collections
     * (DF=2), assigned at {@code assignedAt}. Returns the topic id.
     */
    private long seedHub(String tenant, String label, String assignedAt,
                         String sourceA, String sourceB) {
        long topicId = repo.insertTopic(tenant, label, null, COL_A, 0, null, null);
        seedChunk(tenant, sourceA, hexChash(label + "-doc-a"));
        seedChunk(tenant, sourceB, hexChash(label + "-doc-b"));
        repo.assignTopic(tenant, hexChash(label + "-doc-a"), topicId, "projection", 0.5, sourceA, assignedAt);
        repo.assignTopic(tenant, hexChash(label + "-doc-b"), topicId, "projection", 0.5, sourceB, assignedAt);
        return topicId;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> hubRow(List<Map<String, Object>> hubs, long topicId) {
        return hubs.stream()
            .filter(h -> Long.valueOf(topicId).equals(((Number) h.get("topic_id")).longValue()))
            .findFirst().orElseThrow(() ->
                new AssertionError("topic " + topicId + " did not surface as a hub — the "
                    + "staleness assertions below would be vacuous"));
    }

    @Test @Order(64)
    void detectHubs_staleWhenAssignmentsNewerThanLatestDiscover() {
        // nexus-onjvy gap 3: warn_stale was accepted and SILENTLY DROPPED over HTTP.
        // RDR-077 C-2 semantics: compare against MAX(last_discover_at) across ALL
        // contributing source collections, not a single row.
        final String tenant = "tax-hub-stale-" + System.nanoTime();
        final String srcA = "code__hub_a", srcB = "code__hub_b";
        long topicId = seedHub(tenant, "hub-stale", "2026-04-10T13:04:00Z", srcA, srcB);
        // Both collections discovered BEFORE the hub's latest assignment.
        repo.recordDiscoverCount(tenant, srcA, 100, "2026-04-09T00:00:00Z");
        repo.recordDiscoverCount(tenant, srcB, 100, "2026-04-09T00:00:00Z");

        var hub = hubRow(repo.detectHubsData(tenant, 2), topicId);

        assertThat(hub.get("is_stale")).as("assignments postdate every discover").isEqualTo(true);
        assertThat(hub.get("max_last_discover_at")).isEqualTo("2026-04-09T00:00:00Z");
        assertThat(hub.get("never_discovered_count")).isEqualTo(0);
    }

    @Test @Order(65)
    void detectHubs_notStaleWhenAnyContributingCollectionDiscoveredLater() {
        // THE C-2 CORRECTNESS POINT: MAX aggregates over ALL contributing collections.
        // Updating ONE of the two to postdate the hub's latest assignment clears
        // staleness — a single-row lookup would have kept it stale.
        final String tenant = "tax-hub-fresh-" + System.nanoTime();
        final String srcA = "code__hub_a", srcB = "code__hub_b";
        long topicId = seedHub(tenant, "hub-fresh", "2026-04-10T13:04:00Z", srcA, srcB);
        repo.recordDiscoverCount(tenant, srcA, 100, "2026-04-11T00:00:00Z");
        repo.recordDiscoverCount(tenant, srcB, 100, "2026-04-09T00:00:00Z");

        var hub = hubRow(repo.detectHubsData(tenant, 2), topicId);

        assertThat(hub.get("is_stale")).as("the MAX postdates the assignments").isEqualTo(false);
        assertThat(hub.get("max_last_discover_at"))
            .as("MAX picks the later of the two, not the first row seen")
            .isEqualTo("2026-04-11T00:00:00Z");
        assertThat(hub.get("never_discovered_count")).isEqualTo(0);
    }

    @Test @Order(66)
    void detectHubs_neverDiscoveredCollectionCountsAndForcesStale() {
        // A contributing collection with NO taxonomy_meta row at all is "never
        // discovered" and makes the hub stale regardless of the other's timestamp.
        final String tenant = "tax-hub-never-" + System.nanoTime();
        final String srcA = "code__hub_a", srcB = "code__hub_b";
        long topicId = seedHub(tenant, "hub-never", "2026-04-10T13:04:00Z", srcA, srcB);
        repo.recordDiscoverCount(tenant, srcA, 100, "2026-04-30T00:00:00Z");  // well after
        // srcB deliberately never recorded.

        var hub = hubRow(repo.detectHubsData(tenant, 2), topicId);

        assertThat(hub.get("never_discovered_count")).isEqualTo(1);
        assertThat(hub.get("is_stale"))
            .as("a never-discovered contributor is stale even though MAX postdates")
            .isEqualTo(true);
    }

    // ── Assignment detail projection (nexus-onjvy) ─────────────────────────────

    @Test @Order(67)
    void getAssignmentDetails_returnsTheQualityColumnsForDocsCannotRead() {
        // nexus-onjvy gap 1: similarity / assigned_at / source_collection were WRITTEN
        // by assignTopic and projected by no route — getAssignmentsForDocs selects
        // doc_id + topic_id only, which is asserted here so the two stay distinct.
        final String tenant = "tax-detail-" + System.nanoTime();
        long topicId = repo.insertTopic(tenant, "detail-topic", null, COL_A, 0, null, null);
        seedChunk(tenant, "code__detail_src", hexChash("detail-doc"));
        repo.assignTopic(tenant, hexChash("detail-doc"), topicId, "projection",
            0.8712345, "code__detail_src", "2026-04-14T10:00:00Z");

        var details = repo.getAssignmentDetails(tenant, List.of(hexChash("detail-doc")));

        assertThat(details).hasSize(1);
        var row = details.get(0);
        assertThat(row.get("doc_id")).isEqualTo(hexChash("detail-doc"));
        assertThat(((Number) row.get("topic_id")).longValue()).isEqualTo(topicId);
        assertThat(row.get("assigned_by")).isEqualTo("projection");
        assertThat(((Number) row.get("similarity")).doubleValue()).isEqualTo(0.8712345);
        assertThat(row.get("source_collection")).isEqualTo("code__detail_src");
        assertThat(row.get("assigned_at")).isEqualTo("2026-04-14T10:00:00Z");

        // The cheap map read is deliberately NOT widened: callers destructure it as
        // {doc_id: topic_id}, so widening would change a return type they depend on.
        var map = repo.getAssignmentsForDocs(tenant, List.of(hexChash("detail-doc")));
        assertThat(map).hasSize(1);
        assertThat(map.get(0).keySet())
            .as("for_docs stays a two-key projection")
            .containsExactlyInAnyOrder("doc_id", "topic_id");
    }

    @Test @Order(68)
    void getAssignmentDetails_isTenantScopedAndEmptyForUnknownDocs() {
        final String tenant = "tax-detail-rls-" + System.nanoTime();
        final String other  = "tax-detail-other-" + System.nanoTime();
        long mine = repo.insertTopic(tenant, "mine-topic", null, COL_A, 0, null, null);
        long theirs = repo.insertTopic(other, "their-topic", null, COL_A, 0, null, null);
        seedChunk(tenant, "code__mine", hexChash("shared-doc-id"));
        seedChunk(other, "code__theirs", hexChash("shared-doc-id"));
        repo.assignTopic(tenant, hexChash("shared-doc-id"), mine, "projection", 0.1, "code__mine", null);
        repo.assignTopic(other, hexChash("shared-doc-id"), theirs, "projection", 0.9, "code__theirs", null);

        var out = repo.getAssignmentDetails(tenant, List.of(hexChash("shared-doc-id")));

        assertThat(out).as("the other tenant's row for the same doc_id must not leak")
            .hasSize(1);
        assertThat(out.get(0).get("source_collection")).isEqualTo("code__mine");
        assertThat(repo.getAssignmentDetails(tenant, List.of(hexChash("no-such-doc")))).isEmpty();
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
