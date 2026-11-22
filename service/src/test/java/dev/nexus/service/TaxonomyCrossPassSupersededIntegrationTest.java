// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-2k8wi: the cross pass never attaches a live chunk to a topic of a SUPERSEDED collection.
 *
 * <p>A supersede tombstone (re-embed cutover, {@code catalog_collections.superseded_by} set) keeps its
 * {@code taxonomy_centroids} rows, unlike a rename tombstone whose centroids are re-homed to the new name. The
 * cross pass of {@code assign_from_chashes_<dim>}, its read-only twin {@code cross_preview_<dim>}, the
 * cross-collection branch of {@code taxonomy_ann_query_<dim>} and the foreign-centroid read behind the doctor's
 * recompute all took {@code collection <> source} and nothing else, so the tombstone's centroids stayed
 * candidates: a live collection's projection rows attached to its topics and grew their {@code doc_count}.
 *
 * <p>Each test is its own tenant, with a live source collection holding one chunk at (1, 0), a SUPERSEDED foreign
 * collection holding the exact-match centroid, and a LIVE foreign collection holding a weaker one (0.6, 0.8). The
 * non-vacuity step runs the same query BEFORE the supersede and expects the exact-match centroid, so a green
 * result after the supersede is the filter and not a fixture that never ranked the tombstone first.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCrossPassSupersededIntegrationTest {

    private static final String SVC_ROLE = "svc_2k8wi_test";
    private static final String SVC_PASS = "svc_2k8wi_test_pass";
    private static final int DIM = 1024;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TaxonomyRepository repo;
    TaxonomyCentroidRepository centroids;
    CatalogRepository catalog;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // EXECUTE ON FUNCTION is not part of bootstrapServiceRole's fixed grant set.
            for (int dim : new int[] {384, 768, 1024}) {
                PgContainerHelper.grantExecuteOnFunction(su, "nexus.cross_preview_" + dim
                    + "(text, text[], text, text)", SVC_ROLE);
                PgContainerHelper.grantExecuteOnFunction(su, "nexus.assign_from_chashes_" + dim
                    + "(text, text[], boolean, text, text)", SVC_ROLE);
                PgContainerHelper.grantExecuteOnFunction(su, "nexus.taxonomy_ann_query_" + dim
                    + "(nexus.vector, text, boolean, int, text, text)", SVC_ROLE);
            }
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
        centroids = new TaxonomyCentroidRepository(tenantScope);
        catalog = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    /** One tenant's fixture: a live source, a foreign collection (to be superseded) and a live foreign one. */
    private record Fixture(String tenant, String source, String chash,
                           String retired, long retiredTopic, String live, long liveTopic) {}

    private Fixture fixture(String name) throws Exception {
        String tenant = "x2k8wi-" + name;
        String source = "code__x2k8wi_" + name + "_src__voyage-code-3__v1";
        String retired = "code__x2k8wi_" + name + "_old__voyage-code-3__v1";
        String live = "code__x2k8wi_" + name + "_new__voyage-code-3__v1";
        String chash = hexChash("x2k8wi-chash-" + name);
        seedChunk(tenant, source, chash, unit(1.0f, 0.0f));
        long retiredTopic = seedTopic(tenant, retired, "x2k8wi-retired-" + name);
        seedCentroid(tenant, retired, retiredTopic, unit(1.0f, 0.0f));   // the exact match
        long liveTopic = seedTopic(tenant, live, "x2k8wi-live-" + name);
        seedCentroid(tenant, live, liveTopic, unit(0.6f, 0.8f));         // a weaker live candidate
        return new Fixture(tenant, source, chash, retired, retiredTopic, live, liveTopic);
    }

    @Test
    void crossPass_skipsASupersededCollectionsCentroids_andStillPicksALiveOne() throws Exception {
        Fixture f = fixture("assign");

        // Non-vacuity: before the supersede the tombstone-to-be is the nearest candidate for this chunk.
        assertThat(repo.crossPreview(f.tenant, f.source, List.of(f.chash)))
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(f.retiredTopic));

        supersede(f.tenant, f.retired, f.live);

        // The read-only twin and the persisting pass agree, and both take the live collection's topic.
        assertThat(repo.crossPreview(f.tenant, f.source, List.of(f.chash)))
            .as("cross_preview must not offer a superseded collection's centroid")
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(f.liveTopic));

        Map<String, Object> out = repo.assignFromChashes(f.tenant, f.source, List.of(f.chash), true);
        assertThat(out.get("cross_assigned")).isEqualTo(1);
        assertThat(repo.getAssignmentDetails(f.tenant, List.of(f.chash)))
            .as("the projection row attaches to the live collection's topic, not the retired one")
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(f.liveTopic));
        assertThat(docCount(f.tenant, f.retiredTopic))
            .as("the superseded collection's topic does not grow").isZero();
        assertThat(docCount(f.tenant, f.liveTopic)).isEqualTo(1);
    }

    @Test
    void crossPass_withOnlyASupersededForeignCollection_assignsNothing() throws Exception {
        Fixture f = fixture("only");
        // Drop the live candidate: the tombstone is then the only foreign centroid there is.
        centroids.purgeByCollection(f.tenant, f.live);

        assertThat(repo.crossPreview(f.tenant, f.source, List.of(f.chash)))
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(f.retiredTopic));

        supersede(f.tenant, f.retired, f.live);

        assertThat(repo.crossPreview(f.tenant, f.source, List.of(f.chash))).isEmpty();
        Map<String, Object> out = repo.assignFromChashes(f.tenant, f.source, List.of(f.chash), true);
        assertThat(out.get("cross_assigned")).isEqualTo(0);
        assertThat(repo.getAssignmentDetails(f.tenant, List.of(f.chash))).isEmpty();
        assertThat(docCount(f.tenant, f.retiredTopic)).isZero();
    }

    @Test
    void annQuery_crossBranchSkipsASupersededCollection_butItsOwnCentroidsStayReadable() throws Exception {
        Fixture f = fixture("ann");
        float[] probe = unit(1.0f, 0.0f);

        assertThat(centroids.annQuery(f.tenant, probe, f.source, true, 5))
            .extracting(TaxonomyCentroidRepository.AnnHit::topicId)
            .containsExactly(f.retiredTopic, f.liveTopic);

        supersede(f.tenant, f.retired, f.live);

        assertThat(centroids.annQuery(f.tenant, probe, f.source, true, 5))
            .extracting(TaxonomyCentroidRepository.AnnHit::topicId)
            .as("the cross-collection ANN offers live collections' centroids only")
            .containsExactly(f.liveTopic);
        assertThat(centroids.annQuery(f.tenant, probe, f.retired, false, 5))
            .extracting(TaxonomyCentroidRepository.AnnHit::topicId)
            .as("a same-collection query on the tombstone itself is unchanged")
            .containsExactly(f.retiredTopic);
    }

    @Test
    void foreignCentroids_leaveOutASupersededCollection() throws Exception {
        Fixture f = fixture("foreign");
        assertThat(centroids.getForeignCentroids(f.tenant, f.source))
            .extracting(TaxonomyCentroidRepository.CentroidRecord::collection)
            .containsExactlyInAnyOrder(f.retired, f.live);

        supersede(f.tenant, f.retired, f.live);

        assertThat(centroids.getForeignCentroids(f.tenant, f.source))
            .extracting(TaxonomyCentroidRepository.CentroidRecord::collection)
            .as("the doctor's recompute set and the engine's cross pass must see the same centroids")
            .containsExactly(f.live);
        assertThat(centroids.getByCollection(f.tenant, f.retired))
            .as("the tombstone's own centroids are still readable by name").hasSize(1);
    }

    // ── the write side: a caller-named topic_id (nx taxonomy project --persist) ──────────────────────

    private static Map<String, Object> projectionRow(Fixture f, long topicId, double similarity) {
        return Map.of("doc_id", f.chash, "topic_id", topicId, "assigned_by", "projection",
            "similarity", similarity, "source_collection", f.source);
    }

    @Test
    void projectionAssignMany_ontoASupersededCollectionsTopic_isSkippedAndCounted_aLiveTargetIsStillAccepted()
            throws Exception {
        Fixture f = fixture("pmany");

        // Non-vacuity: before the supersede the same projection onto the tombstone-to-be is written.
        assertThat(repo.assignManyCounted(f.tenant, List.of(projectionRow(f, f.retiredTopic, 0.99))))
            .isEqualTo(new TaxonomyRepository.AssignManyResult(1, 0));
        assertThat(docCount(f.tenant, f.retiredTopic)).isEqualTo(1);
        tenantDeleteAssignments(f);

        supersede(f.tenant, f.retired, f.live);

        var out = repo.assignManyCounted(f.tenant, List.of(
            projectionRow(f, f.retiredTopic, 0.99), projectionRow(f, f.liveTopic, 0.6)));

        assertThat(out).as("one row written, the retired target's row skipped and counted")
            .isEqualTo(new TaxonomyRepository.AssignManyResult(1, 1));
        assertThat(repo.getAssignmentDetails(f.tenant, List.of(f.chash)))
            .extracting(r -> r.get("topic_id")).containsExactly(f.liveTopic);
        assertThat(docCount(f.tenant, f.retiredTopic)).as("the retired topic does not grow").isZero();
        assertThat(docCount(f.tenant, f.liveTopic)).isEqualTo(1);
        assertThat(repo.assignMany(f.tenant, List.of(projectionRow(f, f.retiredTopic, 0.99))))
            .as("the int-returning overload reports rows written").isZero();
    }

    @Test
    void projectionAssignTopic_ontoASupersededCollectionsTopic_isSkipped() throws Exception {
        Fixture f = fixture("pone");
        assertThat(repo.assignTopic(f.tenant, f.chash, f.retiredTopic, "projection", 0.9, f.source, null))
            .as("non-vacuity: written while the collection is live").isTrue();
        tenantDeleteAssignments(f);

        supersede(f.tenant, f.retired, f.live);

        assertThat(repo.assignTopic(f.tenant, f.chash, f.retiredTopic, "projection", 0.9, f.source, null)).isFalse();
        assertThat(repo.getAssignmentDetails(f.tenant, List.of(f.chash))).isEmpty();
        assertThat(docCount(f.tenant, f.retiredTopic)).isZero();
        assertThat(repo.assignTopic(f.tenant, f.chash, f.liveTopic, "projection", 0.6, f.source, null)).isTrue();
        assertThat(docCount(f.tenant, f.liveTopic)).isEqualTo(1);
    }

    @Test
    void nonProjectionAssignments_ontoASupersededCollectionsTopic_areNotThisGuardsConcern() throws Exception {
        // The guard is projection-only: a centroid or manual write names its topic on purpose (and the import
        // verbs carry tombstoned collections as they were), so these stay as they were.
        Fixture f = fixture("pother");
        supersede(f.tenant, f.retired, f.live);

        // nexus-h2v0l: pin BOTH non-projection kinds, batch and single, so nothing widens the guard to
        // "any row onto a retired topic" (mergeTopics into a retired topic carries projection rows across,
        // nexus-14lgk; the guard stays at the projection write).
        String centroidChash = hexChash("x2k8wi-chash-pother-centroid");
        String singleChash = hexChash("x2k8wi-chash-pother-single");
        seedChunk(f.tenant, f.source, centroidChash, unit(1.0f, 0.0f));
        seedChunk(f.tenant, f.source, singleChash, unit(1.0f, 0.0f));

        assertThat(repo.assignManyCounted(f.tenant, List.of(
            Map.of("doc_id", f.chash, "topic_id", f.retiredTopic, "assigned_by", "manual",
                "source_collection", f.source),
            Map.of("doc_id", centroidChash, "topic_id", f.retiredTopic, "assigned_by", "centroid",
                "source_collection", f.source))))
            .isEqualTo(new TaxonomyRepository.AssignManyResult(2, 0));
        assertThat(docCount(f.tenant, f.retiredTopic)).isEqualTo(2);

        assertThat(repo.assignTopic(f.tenant, singleChash, f.retiredTopic, "centroid", null, f.source, null))
            .as("a single centroid row onto the retired topic lands").isTrue();
        assertThat(docCount(f.tenant, f.retiredTopic)).isEqualTo(3);
        assertThat(repo.getAssignmentDetails(f.tenant, List.of(f.chash, centroidChash, singleChash)))
            .hasSize(3);
    }

    // ── a rename tombstone holds no centroids: the re-homed ones stay candidates ─────────────────────

    @Test
    void renamedCollection_keepsItsRehomedCentroidsAsCrossPassCandidates() throws Exception {
        Fixture f = fixture("rename");
        String renamed = "code__x2k8wi_rename_moved__voyage-code-3__v1";
        // The weaker live candidate would win if the filter wrongly dropped the renamed collection.
        assertThat(repo.crossPreview(f.tenant, f.source, List.of(f.chash)))
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(f.retiredTopic));

        catalog.renameCollection(f.tenant, f.retired, renamed);

        assertThat(centroids.getByCollection(f.tenant, renamed)).as("the centroid moved with the rename").hasSize(1);
        assertThat(centroids.getByCollection(f.tenant, f.retired)).as("the tombstone holds none").isEmpty();
        assertThat(repo.crossPreview(f.tenant, f.source, List.of(f.chash)))
            .as("cross_preview still offers the renamed collection's (re-homed) centroid")
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(f.retiredTopic));
        Map<String, Object> out = repo.assignFromChashes(f.tenant, f.source, List.of(f.chash), true);
        assertThat(out.get("cross_assigned")).isEqualTo(1);
        assertThat(docCount(f.tenant, f.retiredTopic)).as("the topic followed the rename and takes the row").isEqualTo(1);
    }

    private void tenantDeleteAssignments(Fixture f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(
                dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS)
                .where(dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS.TENANT_ID.eq(f.tenant)).execute();
        }
    }

    // ── helpers (same idiom as CrossPreviewRepositoryTest) ──────────────────────

    private void supersede(String tenant, String name, String successor) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, java.time.OffsetDateTime.now())
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .execute();
            assertThat(n).as("the collection row to supersede exists").isEqualTo(1);
        }
    }

    private int docCount(String tenant, long topicId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            Integer n = DSL.using(su, SQLDialect.POSTGRES).select(TOPICS.DOC_COUNT).from(TOPICS)
                .where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topicId))
                .fetchOne(TOPICS.DOC_COUNT);
            assertThat(n).as("topic %d exists", topicId).isNotNull();
            return n;
        }
    }

    private static float[] unit(float x, float y) {
        float[] v = new float[DIM];
        v[0] = x;
        v[1] = y;
        return v;
    }

    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void registerCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    private void seedChunk(String tenant, String collection, String hexChashValue, float[] emb) throws Exception {
        registerCollection(tenant, collection);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, tenant)
                .set(CHUNKS.COLLECTION, collection)
                .set(CHUNKS.CHASH, java.util.HexFormat.of().parseHex(hexChashValue))
                .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, collection))
                .set(CHUNKS.CHUNK_TEXT, "seed text " + hexChashValue)
                .set(VectorSeeds.chunkEmbedding(DIM), Vector.of(emb))
                .execute();
        }
    }

    private long seedTopic(String tenant, String collection, String label) throws Exception {
        registerCollection(tenant, collection);
        return repo.insertTopic(tenant, label, null, collection, 0, "2026-01-01T00:00:00Z", null);
    }

    private void seedCentroid(String tenant, String collection, long topicId, float[] emb) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant)
                .set(TAXONOMY_CENTROIDS.COLLECTION, collection)
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, topicId)
                .set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, collection))
                .set(TAXONOMY_CENTROIDS.LABEL, "seed-centroid-label")
                .set(VectorSeeds.centroidEmbedding(DIM), Vector.of(emb))
                .execute();
        }
    }
}
