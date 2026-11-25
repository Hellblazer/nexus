// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.CentroidRecord;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;

/**
 * RDR-229 P3.1 stage 2b, taxonomy family (nexus-p2m1x.17): every production writer of {@code topics},
 * {@code topic_assignments}, {@code taxonomy_meta} and {@code taxonomy_centroids} in {@link TaxonomyRepository} and
 * {@link TaxonomyCentroidRepository} sets the registry id beside the name.
 *
 * <p>Each of the four tables carries a NOT VALID CHECK (the name and its id are both set or both unset; the centroids
 * also {@code collection_id IS NOT NULL}), evaluated on every row version a write produces. So an INSERT must name the
 * id, and an UPDATE that names no collection column ({@code updateTopicLabel}, a doc-count recount, the conflict arm
 * of an import) must set it too, or a row the stamp has not reached is refused with 23514. The unstamped state is built
 * the way an upgraded install has it: the table's CHECKs are rolled back with the changeset's own {@code <rollback>},
 * the ids set NULL, and the changeset's forward text adds them again NOT VALID.
 *
 * <p>The ids are read back with typed jOOQ over a superuser connection.
 *
 * <p>Not here: the doc-count recount the assignment triggers run on {@code topics} (SQL functions, the SQL family's
 * census rows); no case below leaves an unstamped topic for a trigger to recount.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P31TaxonomyDualWriteTest {

    private static final String SVC_ROLE = "svc_p31_taxonomy";
    private static final String SVC_PASS = "svc_p31_taxonomy_pass";
    private static final String CHANGELOG = "db/changelog/vectors-068-collection-id-expand.xml";
    private static final String TENANT = "p31-taxonomy";
    private static final String TS = "2026-01-01T00:00:00Z";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private TaxonomyRepository repo;
    private TaxonomyCentroidRepository centroids;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
        centroids = new TaxonomyCentroidRepository(tenantScope);
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ---- fixture -----------------------------------------------------------------------------------------------

    /** A registered collection (non-conformant names take the 768-wide model) holding the chunks {@code docIds} point at. */
    private String collection(String tag, String... chunkSeeds) throws Exception {
        String c = "knowledge__p31tax_" + tag + seq.incrementAndGet();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, c);
            for (String seed : chunkSeeds) {
                PgContainerHelper.insertChunks(ctx, TENANT, c, List.of(hex(seed)), List.of("seed " + seed),
                    List.of(new float[768]), List.of(Map.of()));
            }
        }
        return c;
    }

    private static String hex(String seed) {
        try {
            return HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static byte[] bytes(String seed) {
        return HexFormat.of().parseHex(hex(seed));
    }

    private Long registryId(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    private Long topicCid(long id) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(TOPICS.COLLECTION_ID).from(TOPICS)
                .where(TOPICS.ID.eq(id)).fetchOne(TOPICS.COLLECTION_ID);
        }
    }

    private Long assignCid(String seed, long topicId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID).from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT)).and(TOPIC_ASSIGNMENTS.DOC_ID.eq(bytes(seed)))
                .and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId)).fetchOne(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID);
        }
    }

    private Long metaCid(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(TAXONOMY_META.COLLECTION_ID).from(TAXONOMY_META)
                .where(TAXONOMY_META.TENANT_ID.eq(TENANT)).and(TAXONOMY_META.COLLECTION.eq(collection))
                .fetchOne(TAXONOMY_META.COLLECTION_ID);
        }
    }

    private Long centroidCid(String collection, long topicId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(TAXONOMY_CENTROIDS.COLLECTION_ID).from(TAXONOMY_CENTROIDS)
                .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.COLLECTION.eq(collection))
                .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(topicId)).fetchOne(TAXONOMY_CENTROIDS.COLLECTION_ID);
        }
    }

    /** Rolls the changeset's CHECKs back, runs {@code nullIt} as the superuser, and adds the CHECKs again NOT VALID. */
    private void unstamp(String changeset, Consumer<DSLContext> nullIt) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.replayChangesetRollback(su, CHANGELOG, changeset);
            nullIt.accept(DSL.using(su, SQLDialect.POSTGRES));
            PgContainerHelper.replayChangeset(su, CHANGELOG, changeset);
        }
    }

    private void unstampTopics(String collection) throws Exception {
        unstamp("vectors-068-9", d -> d.update(TOPICS).setNull(TOPICS.COLLECTION_ID)
            .where(TOPICS.TENANT_ID.eq(TENANT)).and(TOPICS.COLLECTION.eq(collection)).execute());
    }

    private void unstampAssignments(String collection) throws Exception {
        unstamp("vectors-068-7", d -> d.update(TOPIC_ASSIGNMENTS).setNull(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT)).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection)).execute());
    }

    private void unstampMeta(String collection) throws Exception {
        unstamp("vectors-068-10", d -> d.update(TAXONOMY_META).setNull(TAXONOMY_META.COLLECTION_ID)
            .where(TAXONOMY_META.TENANT_ID.eq(TENANT)).and(TAXONOMY_META.COLLECTION.eq(collection)).execute());
    }

    private long topic(String collection) {
        return repo.insertTopic(TENANT, "t" + seq.incrementAndGet(), null, collection, 0, null, null);
    }

    // ---- topics ------------------------------------------------------------------------------------------------

    @Test
    void insertTopicNamesTheRegistryId() throws Exception {
        String c = collection("it");
        long id = topic(c);
        assertThat(topicCid(id)).isNotNull().isEqualTo(registryId(c));
    }

    @Test
    void theLabelAndReviewUpdatesSetTheIdOfAnUnstampedTopic_theyNameNoCollectionColumn() throws Exception {
        String c = collection("up");
        long a = topic(c);
        long b = topic(c);
        long d = topic(c);
        unstampTopics(c);
        assertThat(topicCid(a)).as("fixture: unstamped").isNull();

        repo.updateTopicLabel(TENANT, a, "relabelled-" + seq.incrementAndGet());
        repo.renameTopic(TENANT, b, "renamed-" + seq.incrementAndGet());
        repo.markTopicReviewed(TENANT, d, "accepted");

        assertThat(List.of(topicCid(a), topicCid(b), topicCid(d))).containsOnly(registryId(c));
    }

    @Test
    void renameCollectionGivesEveryRewrittenRowTheTargetsId() throws Exception {
        String from = collection("rf", "rn-doc");
        String to = collection("rt");
        long t = topic(from);
        repo.assignTopic(TENANT, hex("rn-doc"), t, "hdbscan", null, from, null);
        repo.recordDiscoverCount(TENANT, from, 3, TS);
        try (Connection su = pg.createConnection("")) {
            // the chunk follows its collection (the name path moves them in one transaction)
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, to, List.of(hex("rn-doc")),
                List.of("seed"), List.of(new float[768]), List.of(Map.of()));
        }

        Map<String, Integer> counts = repo.renameCollection(TENANT, from, to);

        assertThat(counts).containsEntry("topics", 1).containsEntry("assignments", 1).containsEntry("meta", 1);
        Long toId = registryId(to);
        assertThat(toId).isNotNull().isNotEqualTo(registryId(from));
        assertThat(topicCid(t)).isEqualTo(toId);
        assertThat(assignCid("rn-doc", t)).isEqualTo(toId);
        assertThat(metaCid(to)).isEqualTo(toId);
    }

    // ---- assignments -------------------------------------------------------------------------------------------

    @Test
    void assignTopicNamesTheSourceCollectionsId_projectionAndTheOthers() throws Exception {
        String home = collection("ah", "as-1");
        String src = collection("as", "as-2");
        long t = topic(home);

        repo.assignTopic(TENANT, hex("as-1"), t, "hdbscan", null, home, null);
        repo.assignTopic(TENANT, hex("as-2"), t, "projection", 0.5, src, null);

        assertThat(assignCid("as-1", t)).isEqualTo(registryId(home));
        assertThat(assignCid("as-2", t)).isEqualTo(registryId(src));
    }

    @Test
    void theProjectionConflictArmFollowsTheWinningSourceCollection() throws Exception {
        String a = collection("pa", "pc-doc");
        String b = collection("pb", "pc-doc");
        long t = topic(a);
        repo.assignTopic(TENANT, hex("pc-doc"), t, "projection", 0.2, a, null);
        unstampAssignments(a);
        assertThat(assignCid("pc-doc", t)).as("fixture: unstamped").isNull();

        // a higher similarity from another source wins the row: name, model and id move together
        repo.assignTopic(TENANT, hex("pc-doc"), t, "projection", 0.9, b, null);
        assertThat(assignCid("pc-doc", t)).isEqualTo(registryId(b));

        // a lower similarity loses: the row keeps its source, and an unstamped row is stamped by the losing write too
        unstampAssignments(b);
        repo.assignTopic(TENANT, hex("pc-doc"), t, "projection", 0.1, a, null);
        assertThat(assignCid("pc-doc", t)).as("the winner is still b").isEqualTo(registryId(b));
    }

    @Test
    void assignManyNamesEachRowsSourceId() throws Exception {
        String a = collection("ma", "am-1");
        String b = collection("mb", "am-2");
        long t = topic(a);
        List<Map<String, Object>> rows = List.of(
            Map.of("doc_id", hex("am-1"), "topic_id", t, "assigned_by", "hdbscan", "source_collection", a),
            Map.of("doc_id", hex("am-2"), "topic_id", t, "assigned_by", "hdbscan", "source_collection", b));

        assertThat(repo.assignMany(TENANT, rows)).isEqualTo(2);

        assertThat(assignCid("am-1", t)).isEqualTo(registryId(a));
        assertThat(assignCid("am-2", t)).isEqualTo(registryId(b));
    }

    @Test
    void mergeTopicsMovesTheAssignmentsKeepingTheirSourceIds() throws Exception {
        String c = collection("mg", "mg-1", "mg-2");
        long source = topic(c);
        long target = topic(c);
        repo.assignTopic(TENANT, hex("mg-1"), source, "hdbscan", null, c, null);
        repo.assignTopic(TENANT, hex("mg-2"), source, "hdbscan", null, c, null);
        // an unstamped row of the target that the move conflicts with
        repo.assignTopic(TENANT, hex("mg-2"), target, "hdbscan", null, c, null);
        unstampAssignments(c);

        assertThat(repo.mergeTopics(TENANT, source, target)).contains(c);

        assertThat(assignCid("mg-1", target)).as("moved row").isEqualTo(registryId(c));
        assertThat(assignCid("mg-2", target)).as("conflict arm, unstamped target row").isEqualTo(registryId(c));
    }

    // ---- taxonomy_meta -----------------------------------------------------------------------------------------

    @Test
    void recordDiscoverCountNamesTheIdOnInsertAndOnTheConflictArm() throws Exception {
        String c = collection("dm");
        repo.recordDiscoverCount(TENANT, c, 5, TS);
        assertThat(metaCid(c)).isEqualTo(registryId(c));
        unstampMeta(c);
        assertThat(metaCid(c)).as("fixture: unstamped").isNull();

        repo.recordDiscoverCount(TENANT, c, 9, TS);

        assertThat(metaCid(c)).isEqualTo(registryId(c));
    }

    // ---- persist paths -----------------------------------------------------------------------------------------

    private static Map<String, Object> spec(String label, String... docs) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("label", label);
        m.put("terms", "a,b");
        m.put("terms_json", "[\"a\"]");
        m.put("created_at", TS);
        m.put("doc_ids", java.util.Arrays.stream(docs).map(Rdr229P31TaxonomyDualWriteTest::hex).toList());
        return m;
    }

    @Test
    void persistDiscoveredTopicsStampsTopicsAndAssignments() throws Exception {
        String c = collection("pd", "pd-1", "pd-2");

        List<Long> ids = repo.persistDiscoveredTopics(TENANT, c, List.of(spec("pd-a", "pd-1"), spec("pd-b", "pd-2")));

        assertThat(ids).hasSize(2);
        assertThat(topicCid(ids.get(0))).isEqualTo(registryId(c));
        assertThat(topicCid(ids.get(1))).isEqualTo(registryId(c));
        assertThat(assignCid("pd-1", ids.get(0))).isEqualTo(registryId(c));
        assertThat(assignCid("pd-2", ids.get(1))).isEqualTo(registryId(c));
    }

    @Test
    void persistRebuildTopicsStampsTopicsAssignmentsAndTheManualTransfers() throws Exception {
        String c = collection("pr", "pr-1", "pr-2");
        List<Long> ids = repo.persistDiscoveredTopics(TENANT, c, List.of(spec("pr-old", "pr-1")));
        unstampAssignments(c);

        List<Long> rebuilt = repo.persistRebuildTopics(TENANT, c, List.of(spec("pr-a", "pr-1"), spec("pr-b")),
            Map.of(hex("pr-2"), 1));

        assertThat(rebuilt).hasSize(2).doesNotContainAnyElementsOf(ids);
        assertThat(topicCid(rebuilt.get(0))).isEqualTo(registryId(c));
        assertThat(topicCid(rebuilt.get(1))).isEqualTo(registryId(c));
        assertThat(assignCid("pr-1", rebuilt.get(0))).isEqualTo(registryId(c));
        assertThat(assignCid("pr-2", rebuilt.get(1))).as("the manual transfer").isEqualTo(registryId(c));
    }

    @Test
    void persistSplitStampsTheChildrenAndTheirAssignments() throws Exception {
        String c = collection("ps", "ps-1", "ps-2");
        long parent = topic(c);

        List<Long> kids = repo.persistSplit(TENANT, parent, c, List.of(spec("ps-a", "ps-1"), spec("ps-b", "ps-2")));

        assertThat(kids).hasSize(2);
        for (int i = 0; i < 2; i++) {
            assertThat(topicCid(kids.get(i))).isEqualTo(registryId(c));
        }
        assertThat(assignCid("ps-1", kids.get(0))).isEqualTo(registryId(c));
        assertThat(assignCid("ps-2", kids.get(1))).isEqualTo(registryId(c));
    }

    // ---- fidelity imports --------------------------------------------------------------------------------------

    @Test
    void importTopicStampsOnInsertAndOnTheConflictArm() throws Exception {
        String c = collection("ti");
        long id = 700_000L + seq.incrementAndGet();
        repo.importTopic(TENANT, id, "imp-" + id, null, c, null, 0, TS, "pending", null);
        assertThat(topicCid(id)).isEqualTo(registryId(c));
        unstampTopics(c);

        repo.importTopic(TENANT, id, "imp-" + id, null, c, null, 0, TS, "accepted", null);

        assertThat(topicCid(id)).as("the DO UPDATE arm of an unstamped row").isEqualTo(registryId(c));
    }

    @Test
    void importAssignmentAndImportMetaStampOnInsertAndOnTheConflictArm() throws Exception {
        String c = collection("ia", "ia-1");
        String other = collection("ib", "ia-1");
        long t = topic(c);
        repo.importAssignment(TENANT, hex("ia-1"), t, "hdbscan", 0.3, TS, c);
        assertThat(assignCid("ia-1", t)).isEqualTo(registryId(c));

        // the conflict arm re-points the row at the incoming source: name, model and id together
        repo.importAssignment(TENANT, hex("ia-1"), t, "projection", 0.4, TS, other);
        assertThat(assignCid("ia-1", t)).isEqualTo(registryId(other));

        repo.importTaxonomyMeta(TENANT, c, 4, TS);
        assertThat(metaCid(c)).isEqualTo(registryId(c));
        unstampMeta(c);
        repo.importTaxonomyMeta(TENANT, c, 8, TS);
        assertThat(metaCid(c)).isEqualTo(registryId(c));
    }

    @Test
    void theBatchImportsStampEveryRowWhateverItsCollection() throws Exception {
        String a = collection("ba", "bi-1");
        String b = collection("bb", "bi-2");
        long t1 = 710_000L + seq.incrementAndGet();
        long t2 = 710_000L + seq.incrementAndGet();
        repo.importBatch(TENANT, "topic", List.of(
            Map.of("id", t1, "label", "bt-" + t1, "collection", a, "created_at", TS, "review_status", "pending"),
            Map.of("id", t2, "label", "bt-" + t2, "collection", b, "created_at", TS, "review_status", "pending")));
        assertThat(topicCid(t1)).isEqualTo(registryId(a));
        assertThat(topicCid(t2)).isEqualTo(registryId(b));

        repo.importBatch(TENANT, "assignment", List.of(
            Map.of("doc_id", hex("bi-1"), "topic_id", t1, "assigned_by", "hdbscan", "source_collection", a),
            Map.of("doc_id", hex("bi-2"), "topic_id", t2, "assigned_by", "hdbscan", "source_collection", b)));
        assertThat(assignCid("bi-1", t1)).isEqualTo(registryId(a));
        assertThat(assignCid("bi-2", t2)).isEqualTo(registryId(b));

        repo.importBatch(TENANT, "meta", List.of(
            Map.of("collection", a, "last_discover_doc_count", 1, "last_discover_at", TS),
            Map.of("collection", b, "last_discover_doc_count", 2, "last_discover_at", TS)));
        assertThat(metaCid(a)).isEqualTo(registryId(a));
        assertThat(metaCid(b)).isEqualTo(registryId(b));
    }

    // ---- taxonomy_centroids ------------------------------------------------------------------------------------

    @Test
    void upsertCentroidsNamesTheRegistryIdOnInsertAndOnTheConflictArm() throws Exception {
        String c = collection("ce");
        long t = topic(c);
        centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(c, t, new float[768], "label-1", 1)));
        assertThat(centroidCid(c, t)).isNotNull().isEqualTo(registryId(c));
        unstamp("vectors-068-4", d -> d.update(TAXONOMY_CENTROIDS).setNull(TAXONOMY_CENTROIDS.COLLECTION_ID)
            .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT)).and(TAXONOMY_CENTROIDS.COLLECTION.eq(c)).execute());
        assertThat(centroidCid(c, t)).as("fixture: unstamped").isNull();

        centroids.upsertCentroids(TENANT, List.of(new CentroidRecord(c, t, new float[768], "label-2", 2)));

        assertThat(centroidCid(c, t)).as("the DO UPDATE arm").isEqualTo(registryId(c));
    }
}
