// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.CollectionModelMismatchException;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.ModelPartitions;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnregisteredCollectionException;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.PartitionScratch.expectedName;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 2 Step 1 (nexus-3wh8d.13): the engine's write families against the partitioned layout, through the
 * repositories and as the production role. Chunk, manifest, topic-assignment and centroid writers supply the
 * collection's model and conflict on the four-column keys; a model with no partition is refused before any SQL, naming
 * the model; a model or dimension that disagrees with its collection is refused; a re-home across models is refused,
 * and the cross-model COPY rename sets the manifest's model. (The quarantine, restore, GC and purge families, and the
 * deferred FKs, are exercised by their own suites on the same layout.)
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class P225WritePathIntegrationTest {

    private static final String SVC_ROLE = "svc_p225_write";
    private static final String SVC_PASS = "svc_p225_write_pass";
    private static final String TA = "p225w-a";
    private static final String CODE_3 = "voyage-code-3";
    private static final String CONTEXT_3 = "voyage-context-3";
    private static final String BGE = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    ChashRepository chash;
    TaxonomyRepository taxonomy;
    TaxonomyCentroidRepository centroids;
    final AtomicInteger embedCalls = new AtomicInteger();

    /** A counting fixed-width embedder, so "refused before any SQL" can be told from "refused after embedding". */
    final class CountingEmbedder implements Embedder {
        private final int dim;

        CountingEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            embedCalls.incrementAndGet();
            List<float[]> out = new ArrayList<>();
            for (String t : texts) {
                float[] v = new float[dim];
                v[0] = 1.0f;
                v[1] = (t.hashCode() & 0xff) / 255f;
                out.add(v);
            }
            return out;
        }

        @Override
        public void close() {
        }
    }

    PgVectorRepository repo(int dim) {
        var e = new CountingEmbedder(dim);
        return new PgVectorRepository(scope, e, e);
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(dsl(su), "tok-p225w-a", TA, "p225w");   // the trigger makes the tenant's leaves
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
        chash = new ChashRepository(scope);
        taxonomy = new TaxonomyRepository(scope);
        centroids = new TaxonomyCentroidRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private static String h(int n) {
        return String.format("%064x", n);
    }

    private void register(String collection, String model) {
        catalog.upsertCollection(TA, Map.of("name", collection, "content_type", collection.substring(0, collection.indexOf("__")),
            "owner_id", "p225w", "embedding_model", model));
    }

    private static org.jooq.Field<String> leafOf() {
        return DSL.field(DSL.name("tableoid")).cast(org.jooq.impl.DefaultDataType.getDefaultDataType("regclass")).cast(String.class);
    }

    private long countIn(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return dsl(su).fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TA), CHUNKS.COLLECTION.eq(collection));
        }
    }

    private String leafHolding(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return dsl(su).select(leafOf()).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(TA)).and(CHUNKS.COLLECTION.eq(collection))
                .limit(1).fetchOne(0, String.class);
        }
    }

    private void chunk(PgVectorRepository r, String collection, int n, String text) {
        r.upsertChunks(TA, collection, List.of(h(n)), List.of(text), List.of(Map.of()));
    }

    // ── chunks ───────────────────────────────────────────────────────────────

    @Test
    void upsertChunks_landInTheirModelAndTenantLeaf_andAReWriteConflictsOnTheFourColumnKey() throws Exception {
        String col = "code__w1__voyage-code-3__v1";
        register(col, CODE_3);
        var r = repo(1024);
        r.upsertChunks(TA, col, List.of(h(1), h(2)), List.of("one", "two"), List.of(Map.of(), Map.of()));
        assertThat(countIn(col)).isEqualTo(2);
        assertThat(leafHolding(col)).endsWith(expectedName("chunks", CODE_3, TA));
        try (Connection su = pg.createConnection("")) {
            assertThat(dsl(su).fetchCount(DSL.table(DSL.name("nexus", expectedName("chunks", CODE_3, TA))),
                DSL.field(DSL.name("collection"), String.class).eq(col))).as("both rows are in the leaf").isEqualTo(2);
            assertThat(dsl(su).fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(col), CHUNKS.EMBEDDING_MODEL.eq(CODE_3))).isEqualTo(2);
        }
        // A forced re-embed of an existing chash reaches ON CONFLICT (tenant_id, collection, chash, embedding_model).
        r.upsertChunks(TA, col, List.of(h(1)), List.of("one-rewritten"), List.of(Map.of()), true);
        assertThat(countIn(col)).as("the conflict updated the row, it did not add one").isEqualTo(2);
        try (Connection su = pg.createConnection("")) {
            assertThat(dsl(su).fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(col), CHUNKS.CHUNK_TEXT.eq("one-rewritten"))).isEqualTo(1);
            assertThat(dsl(su).fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(col), CHUNKS.CHUNK_TEXT.eq("one"))).isZero();
        }
    }

    @Test
    void aCollectionOfAnotherModelLandsInThatModelsPartition() throws Exception {
        String col = "docs__w2__bge-base-en-v15-768__v1";
        register(col, BGE);
        chunk(repo(768), col, 11, "bge text");
        assertThat(leafHolding(col)).endsWith(expectedName("chunks", BGE, TA));
        // The same chash under a collection of the first model is a different row in a different partition.
        String other = "code__w2b__voyage-code-3__v1";
        register(other, CODE_3);
        chunk(repo(1024), other, 11, "code text");
        assertThat(leafHolding(other)).endsWith(expectedName("chunks", CODE_3, TA));
    }

    @Test
    void aModelWithNoPartition_isRefusedBeforeAnyWriteOrEmbed_namingTheModel_andALandsOnceTheChangesetAddsIt() throws Exception {
        String model = "p225-added-1024";
        String col = "code__w3__" + model + "__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            dsl(su).insertInto(EMBEDDING_MODELS, EMBEDDING_MODELS.EMBEDDING_MODEL, EMBEDDING_MODELS.DIMENSION, EMBEDDING_MODELS.PROVIDER)
                .values(model, 1024, "voyage").execute();
            PgContainerHelper.insertCollection(dsl(su), TA, col, model);
        }
        embedCalls.set(0);
        var r = repo(1024);
        assertThatThrownBy(() -> chunk(r, col, 21, "refused"))
            .isInstanceOfSatisfying(ModelPartitions.ModelPartitionMissingException.class, e -> {
                assertThat(e.registered()).as("registered in embedding_models, never given a partition").isTrue();
                assertThat(e.model()).isEqualTo(model);
                assertThat(e.getMessage()).contains(model).contains("create_model_partition");
            });
        assertThat(embedCalls.get()).as("refused before the embedder was called").isZero();
        assertThat(countIn(col)).as("nothing was written").isZero();

        // The changeset's second half: the partition (and a leaf for every tenant with a token) appears.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PartitionScratch.createModelPartition(dsl(su), "chunks", model, true);
            PartitionScratch.createModelPartition(dsl(su), "taxonomy_centroids", model, true);
        }
        chunk(r, col, 21, "now it lands");
        assertThat(countIn(col)).isEqualTo(1);
        assertThat(leafHolding(col)).endsWith(expectedName("chunks", model, TA));
    }

    @Test
    void anUnregisteredModel_isRefusedNamingIt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThatThrownBy(() -> ModelPartitions.require(dsl(su), ModelPartitions.CHUNKS, "no-such-model"))
                .isInstanceOfSatisfying(ModelPartitions.ModelPartitionMissingException.class, e -> {
                    assertThat(e.registered()).isFalse();
                    assertThat(e.getMessage()).contains("no-such-model").contains("not registered");
                });
            assertThat(ModelPartitions.exists(dsl(su), ModelPartitions.CHUNKS, CODE_3)).isTrue();
            assertThat(ModelPartitions.exists(dsl(su), ModelPartitions.CENTROIDS, CODE_3)).isTrue();
            assertThat(ModelPartitions.exists(dsl(su), ModelPartitions.CHUNKS, "no-such-model")).isFalse();
        }
    }

    @Test
    void aModelOrDimensionThatDisagreesWithTheCollection_isRefusedByTheSchema() throws Exception {
        String col = "code__w4__voyage-code-3__v1";
        register(col, CODE_3);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = dsl(su);
            // A 768-wide vector under voyage-code-3: the model partition's dimension CHECK.
            assertThatThrownBy(() -> PgContainerHelper.insertChunks(ctx, TA, col, List.of(h(31)), List.of("x"),
                    List.of(new float[768]), List.of(Map.of())))
                .isInstanceOf(DataAccessException.class)
                .hasMessageContaining("_dimension_chk");
            // A row filed under another model than its collection's: the composite foreign key to the registry.
            var vec = dev.nexus.service.jooq.binding.Vector.of(new float[1024]);
            assertThatThrownBy(() -> ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                        CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_1024)
                    .values(TA, col, dev.nexus.service.db.Chash.fromHex(h(32)).toBytes(), CONTEXT_3, "x", vec).execute())
                .isInstanceOf(DataAccessException.class)
                .satisfies(t -> {
                    Throwable c = t;
                    while (c != null && !(c instanceof SQLException)) c = c.getCause();
                    assertThat(((SQLException) c).getSQLState()).isEqualTo("23503");
                    assertThat(c.getMessage()).contains("violates foreign key constraint");
                });
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(col))).isZero();
        }
        // And through the repository: a vector of the wrong width is refused before SQL.
        assertThatThrownBy(() -> repo(768).upsertChunks(TA, col, List.of(h(33)), List.of("y"), List.of(Map.of())))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("dispatches to embedding_1024");
    }

    // ── manifest ─────────────────────────────────────────────────────────────

    @Test
    void manifestRowsCarryTheirCollectionsModel_andAnUnregisteredCollectionIsRefusedNamedNotAnFkError() throws Exception {
        String col = "docs__w5__voyage-context-3__v1";
        register(col, CONTEXT_3);
        chunk(repo(1024), col, 41, "manifest chunk");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCatalogDocument(dsl(su), TA, "w5-doc");
        }
        catalog.writeManifest(TA, "w5-doc", col, List.of(Map.of("position", 0, "chash", h(41), "chunk_index", 0)));
        try (Connection su = pg.createConnection("")) {
            var rows = dsl(su).select(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                .from(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TA))
                .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("w5-doc")).fetch();
            assertThat(rows).hasSize(1);
            assertThat(rows.get(0).value1()).isEqualTo(col);
            assertThat(rows.get(0).value2()).isEqualTo(CONTEXT_3);
        }
        // An append upsert (the position conflicts) keeps the model with the collection.
        catalog.appendManifestChunks(TA, "w5-doc", col, List.of(Map.of("position", 0, "chash", h(41), "chunk_index", 0)));
        assertThatThrownBy(() -> catalog.writeManifest(TA, "w5-doc", "docs__nowhere__voyage-context-3__v1",
                List.of(Map.of("position", 0, "chash", h(41), "chunk_index", 0))))
            .isInstanceOf(UnregisteredCollectionException.class);
    }

    // ── topic assignments ────────────────────────────────────────────────────

    @Test
    void topicAssignmentsCarryTheirSourceCollectionsModel_andTheModelFollowsAProjectionThatMovesTheCollection() throws Exception {
        String a = "code__w6a__voyage-code-3__v1";
        String b = "docs__w6b__voyage-context-3__v1";
        register(a, CODE_3);
        register(b, CONTEXT_3);
        chunk(repo(1024), a, 51, "same text");
        chunk(repo(1024), b, 51, "same text");   // the same chash, a chunk in each collection (RDR-108)
        chunk(repo(1024), a, 52, "other text");
        long topic1 = taxonomy.insertTopic(TA, "w6-t1", null, a, 0, "2024-01-01T00:00:00Z", null);
        long topic2 = taxonomy.insertTopic(TA, "w6-t2", null, a, 0, "2024-01-01T00:00:00Z", null);

        taxonomy.assignTopic(TA, h(51), topic1, "hdbscan", null, a, null);
        assertThat(assignment(h(51), topic1)).containsExactly(a, CODE_3);

        // A projection from collection a, then a stronger one from collection b: collection and model move together.
        taxonomy.assignTopic(TA, h(51), topic2, "projection", 0.5, a, null);
        assertThat(assignment(h(51), topic2)).containsExactly(a, CODE_3);
        taxonomy.assignTopic(TA, h(51), topic2, "projection", 0.9, b, null);
        assertThat(assignment(h(51), topic2)).as("the winner's collection and its model, one foreign key").containsExactly(b, CONTEXT_3);
        taxonomy.assignTopic(TA, h(51), topic2, "projection", 0.2, a, null);
        assertThat(assignment(h(51), topic2)).as("a weaker one changes neither").containsExactly(b, CONTEXT_3);

        // The batch and import writers.
        taxonomy.assignMany(TA, List.of(Map.of("doc_id", h(52), "topic_id", topic1, "assigned_by", "hdbscan", "source_collection", a)));
        assertThat(assignment(h(52), topic1)).containsExactly(a, CODE_3);
        assertThat(taxonomy.importAssignment(TA, h(52), topic2, "hdbscan", 0.3, "2024-01-01T00:00:00Z", a)).isTrue();
        assertThat(assignment(h(52), topic2)).containsExactly(a, CODE_3);

        // A merge copies the moved rows with the model of the chunk each points at.
        taxonomy.mergeTopics(TA, topic2, topic1);
        assertThat(assignment(h(52), topic1)).containsExactly(a, CODE_3);
        assertThat(assignment(h(51), topic1)).as("the stronger projection won the merge").containsExactly(b, CONTEXT_3);
    }

    private List<String> assignment(String chashHex, long topic) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = dsl(su).select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL).from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TA))
                .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(dev.nexus.service.db.Chash.fromHex(chashHex).toBytes()))
                .and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic)).fetchOne();
            return r == null ? List.of() : List.of(r.value1(), r.value2());
        }
    }

    // ── centroids ────────────────────────────────────────────────────────────

    @Test
    void centroidsFollowTheirCollectionsModel_refuseAWrongWidth_andAReEmbedMovesThePartition() throws Exception {
        String col = "code__w7__voyage-code-3__v1";
        register(col, CODE_3);
        float[] v1024 = new float[1024];
        v1024[0] = 1f;
        centroids.upsertCentroids(TA, List.of(new TaxonomyCentroidRepository.CentroidRecord(col, 1, v1024, "first", 3)));
        centroids.upsertCentroids(TA, List.of(new TaxonomyCentroidRepository.CentroidRecord(col, 1, v1024, "second", 4)));
        try (Connection su = pg.createConnection("")) {
            var rows = dsl(su).select(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT, leafOf())
                .from(TAXONOMY_CENTROIDS).where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TA)).and(TAXONOMY_CENTROIDS.COLLECTION.eq(col)).fetch();
            assertThat(rows).as("the second upsert updated the row on the four-column key").hasSize(1);
            assertThat(rows.get(0).value1()).isEqualTo(CODE_3);
            assertThat(rows.get(0).value2()).isEqualTo("second");
            assertThat(rows.get(0).value3()).isEqualTo(4);
            assertThat(rows.get(0).value4()).endsWith(expectedName("taxonomy_centroids", CODE_3, TA));
        }
        // A vector that is not the model's width is refused before SQL, naming the model.
        assertThatThrownBy(() -> centroids.upsertCentroids(TA, List.of(
                new TaxonomyCentroidRepository.CentroidRecord(col, 2, new float[768], "bad", 1))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(CODE_3).hasMessageContaining("768");
        // A collection with no registry row has no model to file the centroid under.
        assertThatThrownBy(() -> centroids.upsertCentroids(TA, List.of(
                new TaxonomyCentroidRepository.CentroidRecord("code__nowhere__voyage-code-3__v1", 3, v1024, "x", 1))))
            .isInstanceOf(UnregisteredCollectionException.class);

        // nexus-2qryr: the collection is re-embedded under another model (re-registered: it holds no chunks), and the
        // same centroid is upserted with the new width. The stored row cannot move across partitions, so it is replaced.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            dsl(su).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, BGE)
                .set(CATALOG_COLLECTIONS.DIMENSION, 768)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TA)).and(CATALOG_COLLECTIONS.NAME.eq(col)).execute();
        }
        CollectionRegistry.evict(TA, col);
        float[] v768 = new float[768];
        v768[0] = 1f;
        centroids.upsertCentroids(TA, List.of(new TaxonomyCentroidRepository.CentroidRecord(col, 1, v768, "third", 5)));
        try (Connection su = pg.createConnection("")) {
            var rows = dsl(su).select(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.LABEL, leafOf())
                .from(TAXONOMY_CENTROIDS).where(TAXONOMY_CENTROIDS.TENANT_ID.eq(TA)).and(TAXONOMY_CENTROIDS.COLLECTION.eq(col))
                .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(1L)).fetch();
            assertThat(rows).as("the old row was deleted, the new one inserted").hasSize(1);
            assertThat(rows.get(0).value1()).isEqualTo(BGE);
            assertThat(rows.get(0).value2()).isEqualTo("third");
            assertThat(rows.get(0).value3()).endsWith(expectedName("taxonomy_centroids", BGE, TA));
        }
    }

    // ── renames and re-homes ─────────────────────────────────────────────────

    @Test
    void chashRename_acrossModels_isRefusedNamingBoth_sameModelMoves_andACollisionCascadeStillWorks() throws Exception {
        String x = "code__w8x__voyage-code-3__v1";
        String y = "docs__w8y__voyage-context-3__v1";
        String x2 = "code__w8x2__voyage-code-3__v1";
        register(x, CODE_3);
        register(y, CONTEXT_3);
        register(x2, CODE_3);
        chunk(repo(1024), x, 61, "movable");

        assertThatThrownBy(() -> chash.renameCollection(TA, x, y))
            .isInstanceOfSatisfying(CollectionModelMismatchException.class, e -> {
                assertThat(e.sourceModel()).isEqualTo(CODE_3);
                assertThat(e.targetModel()).isEqualTo(CONTEXT_3);
                assertThat(e.getMessage()).contains(x).contains(y).contains(CODE_3).contains(CONTEXT_3);
            });
        assertThat(countIn(x)).as("refused, never re-filed").isEqualTo(1);
        assertThat(countIn(y)).isZero();

        // Same model: the UPDATE of collection stays in the leaf.
        String before = leafHolding(x);
        assertThat(chash.renameCollection(TA, x, x2)).isEqualTo(1);
        assertThat(countIn(x2)).isEqualTo(1);
        assertThat(leafHolding(x2)).as("the same leaf").isEqualTo(before);

        // The RDR-162 cross-model cascade: the target already holds the chash, so the source's copy is dropped and
        // nothing is left to move.
        String x3 = "code__w8x3__voyage-code-3__v1";
        register(x3, CODE_3);
        chunk(repo(1024), x3, 62, "twin");
        chunk(repo(1024), y, 62, "twin");
        assertThat(chash.renameCollection(TA, x3, y)).isZero();
        assertThat(countIn(x3)).isZero();
        assertThat(countIn(y)).isEqualTo(1);
    }

    @Test
    void catalogRename_copyBranchAcrossModels_setsTheManifestModelToTheTargets() throws Exception {
        String x = "code__w9x__voyage-code-3__v1";
        String y = "docs__w9y__voyage-context-3__v1";
        register(x, CODE_3);
        register(y, CONTEXT_3);
        chunk(repo(1024), x, 71, "re-embedded");
        chunk(repo(1024), y, 71, "re-embedded");   // the cross-model re-embed preserved the chash
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.ownChunks(dsl(su), TA, x, h(71));
        }
        Map<String, Integer> counts = catalog.renameCollection(TA, x, y);   // y is live: the COPY branch
        assertThat(counts).containsKey("catalog_document_chunks");
        try (Connection su = pg.createConnection("")) {
            var row = dsl(su).select(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                .from(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TA))
                .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("own-" + x)).fetchOne();
            assertThat(row.value1()).isEqualTo(y);
            assertThat(row.value2()).as("the target's model, or the foreign key to the target's chunk would not hold").isEqualTo(CONTEXT_3);
            assertThat(dsl(su).fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TA), CHUNKS.COLLECTION.eq(x))).as("the source's chunk is untouched").isEqualTo(1);
        }
    }

    @Test
    void catalogRename_canonical_revivingATombstoneWithAnotherModel_isNotServedFromAStaleRegistryRow() throws Exception {
        String x = "code__w10x__voyage-code-3__v1";
        String y = "docs__w10y__voyage-context-3__v1";
        register(x, CODE_3);
        register(y, CONTEXT_3);
        // Retire y as a superseded tombstone (empty), so renaming x onto it takes the canonical branch and revives it
        // with x's model.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            dsl(su).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "somewhere-else")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TA)).and(CATALOG_COLLECTIONS.NAME.eq(y)).execute();
        }
        chunk(repo(1024), x, 81, "to move");
        assertThat(CollectionRegistry.lookup(scope, TA, y).embeddingModel()).as("cached with the old model").isEqualTo(CONTEXT_3);
        catalog.renameCollection(TA, x, y);
        assertThat(CollectionRegistry.lookup(scope, TA, y).embeddingModel()).as("re-read after the revive").isEqualTo(CODE_3);
        assertThat(countIn(y)).isEqualTo(1);
        assertThat(leafHolding(y)).endsWith(expectedName("chunks", CODE_3, TA));
        // A write now lands under the revived row's model.
        chunk(repo(1024), y, 82, "after the revive");
        assertThat(countIn(y)).isEqualTo(2);
    }

    @Test
    void rehome_acrossModels_isRefusedNamingBoth_andSameModelMovesTheChunks() throws Exception {
        String s = "code__w11s__voyage-code-3__v1";
        String t = "docs__w11t__voyage-context-3__v1";
        String t2 = "code__w11t2__voyage-code-3__v1";
        register(s, CODE_3);
        register(t, CONTEXT_3);
        register(t2, CODE_3);
        chunk(repo(1024), s, 91, "rehome me");
        assertThatThrownBy(() -> catalog.rehomeCollection(TA, s, t))
            .isInstanceOf(CatalogRepository.RehomeRefused.class)
            .hasMessageContaining(CODE_3).hasMessageContaining(CONTEXT_3).hasMessageContaining(s).hasMessageContaining(t);
        assertThat(countIn(s)).isEqualTo(1);
        var result = catalog.rehomeCollection(TA, s, t2);
        assertThat(result.movedChunks()).isEqualTo(1);
        assertThat(countIn(t2)).isEqualTo(1);
        assertThat(countIn(s)).isZero();
    }
}
