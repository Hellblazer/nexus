// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TaxonomyCoverage;
import dev.nexus.service.db.TaxonomyCoverage.Counts;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Consumer;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.2 (RDR-134 Step 2): the per-collection coverage count ({@code nexus.taxonomy_coverage_counts_<dim>},
 * taxonomy-022, set-based bodies in taxonomy-023) and the cache that serves it ({@link TaxonomyCoverage}).
 *
 * <p>The function, at each dimension (it exists once per dimension, so a missed dimension is a hole nothing else
 * notices): an orphaned taxonomy (topics and a centroid, zero chunks) has no coverage and is never routed; a half
 * assigned collection reads 0.5; a projection row (an assignment to ANOTHER collection's topic) does not count as
 * assigned; a manifest-less chunk and a chunk whose only owner is tombstoned are excluded from the numerator AND the
 * denominator, so the two share one predicate; a collection with no centroids reads has_taxonomy false; another
 * tenant's rows are not counted.
 *
 * <p>The cache: a value is served until it is invalidated for its collection (another collection's entry and another
 * tenant's are kept), until the ten-minute ceiling passes (an injected clock), or never, when the count failed. A fill
 * that was in flight when its collection was invalidated does not cache what it counted. Whether the engine's routes
 * invalidate is {@code TaxonomyCoverageInvalidationRouteTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCoverageTest {

    private static final String TENANT = "7fku212-coverage";
    private static final String OTHER_TENANT = "7fku212-coverage-other";
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    /** One registered collection with its dimension and model, and the topic the fixture assigns its chunks to. */
    private record Coll(String tenant, int dim, String name, String model, long topic) {}

    private void seed(Consumer<DSLContext> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            work.accept(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private static String modelToken(int dim) {
        return switch (dim) {
            case 384 -> "minilm-l6-v2-384";
            case 768 -> "bge-base-en-v15-768";
            default -> "voyage-context-3";
        };
    }

    /** A registered, empty collection of {@code tenant} at {@code dim}, with one topic and, if asked, its centroid. */
    private Coll collection(String tenant, int dim, String stem, boolean centroid) throws Exception {
        String name = "knowledge__7fku212-" + stem + "-" + seq.incrementAndGet() + "__" + modelToken(dim) + "__v1";
        Coll[] out = new Coll[1];
        seed(dsl -> {
            PgContainerHelper.insertCollection(dsl, tenant, name);
            String model = PgContainerHelper.collectionModel(dsl, tenant, name);
            long topic = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, tenant).set(TOPICS.LABEL, "topic of " + name).set(TOPICS.COLLECTION, name).set(TOPICS.COLLECTION_ID, CollectionIds.idOf(tenant, name))
                .set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            if (centroid) {
                dsl.insertInto(TAXONOMY_CENTROIDS)
                    .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, name).set(TAXONOMY_CENTROIDS.COLLECTION_ID, CollectionIds.idOf(tenant, name))
                    .set(TAXONOMY_CENTROIDS.TOPIC_ID, topic).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
                    .set(TAXONOMY_CENTROIDS.LABEL, "centroid of " + name)
                    .set(VectorSeeds.centroidEmbedding(dim), VectorSeeds.vecXY(dim, 1f, 0f))
                    .execute();
            }
            out[0] = new Coll(tenant, dim, name, model, topic);
        });
        return out[0];
    }

    private static byte[] chash(Coll c, String key) {
        return HexFormat.of().parseHex(Chash.ofText(c.name() + "/" + key).toHex());
    }

    private void storeChunk(DSLContext dsl, Coll c, String key) {
        storeChunk(dsl, c, chash(c, key));
    }

    private void storeChunk(DSLContext dsl, Coll c, byte[] chash) {
        dsl.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, c.tenant()).set(CHUNKS.COLLECTION, c.name()).set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(c.tenant(), c.name())).set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, c.model()).set(CHUNKS.CHUNK_TEXT, "coverage chunk " + HexFormat.of().formatHex(chash))
            .set(VectorSeeds.chunkEmbedding(c.dim()), VectorSeeds.vecXY(c.dim(), 1f, 0f))
            .execute();
    }

    private void ownBy(DSLContext dsl, Coll c, String key, String doc, boolean tombstoned) {
        dsl.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, c.tenant()).set(CATALOG_DOCUMENTS.TUMBLER, doc)
            .set(CATALOG_DOCUMENTS.TITLE, "Doc " + doc).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, c.name())
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(c.tenant(), c.name()))
            .set(CATALOG_DOCUMENTS.DELETED_AT, tombstoned ? SEED_TS : null)
            .onConflictDoNothing()
            .execute();
        dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, c.tenant()).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash(c, key))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, c.name())
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(c.tenant(), c.name()))
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, c.model())
            .execute();
    }

    private void assign(DSLContext dsl, Coll chunkCollection, long topic, String key) {
        dsl.insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, chunkCollection.tenant()).set(TOPIC_ASSIGNMENTS.DOC_ID, chash(chunkCollection, key))
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, chunkCollection.name()).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(chunkCollection.tenant(), chunkCollection.name()))
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, chunkCollection.model())
            .execute();
    }

    /** Live chunks {@code prefix0 .. prefix(n-1)}: stored, owned by a live document; the first {@code assigned} carry an own assignment. */
    private void liveChunks(Coll c, String prefix, int n, int assigned) throws Exception {
        seed(dsl -> {
            for (int i = 0; i < n; i++) {
                String key = prefix + i;
                storeChunk(dsl, c, key);
                ownBy(dsl, c, key, "doc-" + c.name() + "-" + key, false);
                if (i < assigned) assign(dsl, c, c.topic(), key);
            }
        });
    }

    private Counts read(TaxonomyCoverage coverage, Coll c) {
        Optional<Counts> counts = coverage.lookup(c.tenant(), c.name());
        assertThat(counts).as("the count could be taken for %s", c.name()).isPresent();
        return counts.get();
    }

    private TaxonomyCoverage fresh() {
        return new TaxonomyCoverage(scope);
    }

    // ── the function ──────────────────────────────────────────────────────────

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void anOrphanedTaxonomy_topicsAndACentroidButNoChunks_hasNoCoverageAndIsNeverRouted(int dim) throws Exception {
        Coll c = collection(TENANT, dim, "orphan", true);

        Counts counts = read(fresh(), c);

        assertThat(counts).isEqualTo(new Counts(true, 0, 0));
        assertThat(counts.coverage()).as("no chunks is no coverage, not 1.0 and not a division by zero").isZero();
        assertThat(counts.routable()).isFalse();
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aHalfAssignedCollection_readsHalf_andIsBelowTheRoutingThreshold(int dim) throws Exception {
        Coll c = collection(TENANT, dim, "half", true);
        liveChunks(c, "k", 10, 5);

        Counts counts = read(fresh(), c);

        assertThat(counts).isEqualTo(new Counts(true, 10, 5));
        assertThat(counts.coverage()).isEqualTo(0.5);
        assertThat(counts.routable()).isFalse();
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void theRoutingThreshold_nineOfTenIsRouted_eightOfTenIsNot_andAFullCollectionIs(int dim) throws Exception {
        Coll nine = collection(TENANT, dim, "nine", true);
        liveChunks(nine, "k", 10, 9);
        Coll eight = collection(TENANT, dim, "eight", true);
        liveChunks(eight, "k", 10, 8);
        Coll full = collection(TENANT, dim, "full", true);
        liveChunks(full, "k", 10, 10);

        TaxonomyCoverage coverage = fresh();
        assertThat(read(coverage, nine).routable()).as("0.9 is at the threshold").isTrue();
        assertThat(read(coverage, eight).routable()).as("0.8 is below it").isFalse();
        assertThat(read(coverage, full)).isEqualTo(new Counts(true, 10, 0));
        assertThat(read(coverage, full).routable()).isTrue();
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aProjectionRow_anAssignmentToAnotherCollectionsTopic_doesNotCountAsAssigned(int dim) throws Exception {
        Coll own = collection(TENANT, dim, "projown", true);
        Coll foreign = collection(TENANT, dim, "projforeign", true);
        liveChunks(own, "k", 4, 0);
        seed(dsl -> {
            // k0 and k1 carry ONLY a projection onto the foreign collection's topic: the row's source is the
            // chunk's own collection, its topic belongs to the other one (what a cross-collection pass writes)
            assign(dsl, own, foreign.topic(), "k0");
            assign(dsl, own, foreign.topic(), "k1");
            // k2 carries an own assignment, so the count is not trivially 4 of 4
            assign(dsl, own, own.topic(), "k2");
        });

        Counts counts = read(fresh(), own);

        assertThat(counts).as("k0, k1 and k3 are unassigned: a projection reaches no routed search of this collection")
            .isEqualTo(new Counts(true, 4, 3));
    }

    @Test
    void aChunkSharingItsChashWithAnAssignmentOfThisCollectionsTopic_countsAsAssignedWhateverTheRowsSource()
            throws Exception {
        // The pinned side of the predicate (taxonomy-022's header): the membership test of search_topic_set, which
        // reads the topic's collection and not the assignment's source_collection. A same-chash chunk reached by
        // that row IS returned by the routed search, so it is counted reachable.
        Coll own = collection(TENANT, 1024, "sharedchash", true);
        Coll sibling = collection(TENANT, 1024, "sharedchashsib", true);
        liveChunks(own, "k", 2, 0);
        seed(dsl -> {
            // the sibling holds a chunk with k0's chash (the assignment's foreign key wants one under its source)
            storeChunk(dsl, sibling, chash(own, "k0"));
            dsl.insertInto(TOPIC_ASSIGNMENTS)
                .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT).set(TOPIC_ASSIGNMENTS.DOC_ID, chash(own, "k0"))
                .set(TOPIC_ASSIGNMENTS.TOPIC_ID, own.topic()).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, sibling.name()).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(TENANT, sibling.name()))
                .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, own.model())
                .execute();
        });

        assertThat(read(fresh(), own)).isEqualTo(new Counts(true, 2, 1));
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aManifestLessChunk_isInNeitherTheNumeratorNorTheDenominator(int dim) throws Exception {
        Coll c = collection(TENANT, dim, "ghost", true);
        liveChunks(c, "k", 4, 2);
        seed(dsl -> {
            // three ghosts: stored, unassigned, owned by no document. Counted in the denominator alone they would
            // read as a gap no assignment can close.
            for (String key : List.of("g0", "g1", "g2")) storeChunk(dsl, c, key);
            // and one ghost that IS assigned, so the numerator side is exercised too
            storeChunk(dsl, c, "g3");
            assign(dsl, c, c.topic(), "g3");
        });

        assertThat(read(fresh(), c)).isEqualTo(new Counts(true, 4, 2));
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aChunkWhoseOnlyOwnerIsTombstoned_isInNeitherTheNumeratorNorTheDenominator(int dim) throws Exception {
        Coll c = collection(TENANT, dim, "tomb", true);
        liveChunks(c, "k", 4, 2);
        seed(dsl -> {
            // t0: owned only by a tombstoned document, unassigned. t1: the same, assigned.
            storeChunk(dsl, c, "t0");
            ownBy(dsl, c, "t0", "tomb-doc-t0-" + c.name(), true);
            storeChunk(dsl, c, "t1");
            ownBy(dsl, c, "t1", "tomb-doc-t1-" + c.name(), true);
            assign(dsl, c, c.topic(), "t1");
            // l0: a tombstoned owner AND a live one: live, and unassigned, so it is counted
            storeChunk(dsl, c, "l0");
            ownBy(dsl, c, "l0", "tomb-doc-l0-" + c.name(), true);
            ownBy(dsl, c, "l0", "live-doc-l0-" + c.name(), false);
        });

        assertThat(read(fresh(), c))
            .as("4 live + l0; t0 and t1 are in neither count (taxonomy_unassigned_chashes would also list t0)")
            .isEqualTo(new Counts(true, 5, 3));
    }

    /**
     * One chunk, many rows: taxonomy-023 groups its three arms by chash, so a chunk with two live owners (two documents)
     * and two assignments to topics of its own collection is still one chunk and one assigned chunk. A count that
     * joined the arms instead of grouping them would count d0 four times.
     */
    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aChunkWithTwoLiveOwnersAndTwoOwnTopicAssignments_isCountedOnce(int dim) throws Exception {
        Coll c = collection(TENANT, dim, "dedup", true);
        liveChunks(c, "k", 2, 1);
        seed(dsl -> {
            long second = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, c.tenant()).set(TOPICS.LABEL, "second topic of " + c.name())
                .set(TOPICS.COLLECTION, c.name())
                .set(TOPICS.COLLECTION_ID, CollectionIds.idOf(c.tenant(), c.name())).set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            // d0: two live owners, assigned to both of the collection's topics
            storeChunk(dsl, c, "d0");
            ownBy(dsl, c, "d0", "live-doc-a-d0-" + c.name(), false);
            ownBy(dsl, c, "d0", "live-doc-b-d0-" + c.name(), false);
            assign(dsl, c, c.topic(), "d0");
            assign(dsl, c, second, "d0");
            // d1: two live owners, unassigned
            storeChunk(dsl, c, "d1");
            ownBy(dsl, c, "d1", "live-doc-a-d1-" + c.name(), false);
            ownBy(dsl, c, "d1", "live-doc-b-d1-" + c.name(), false);
        });

        assertThat(read(fresh(), c)).as("k0, k1, d0, d1; k1 and d1 unassigned").isEqualTo(new Counts(true, 4, 2));
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aCollectionWithNoCentroids_readsNoTaxonomy_andIsNotRouted(int dim) throws Exception {
        Coll c = collection(TENANT, dim, "nocentroid", false);
        liveChunks(c, "k", 6, 6); // even fully assigned: no centroid means nothing to route by

        Counts counts = read(fresh(), c);

        assertThat(counts.hasTaxonomy()).isFalse();
        assertThat(counts.routable()).isFalse();
        assertThat(counts).as("not measured when there is no taxonomy: the chunk scan is skipped")
            .isEqualTo(new Counts(false, 0, 0));
    }

    @Test
    void anotherTenantsRowsInTheSameNamedCollection_areNotCounted() throws Exception {
        Coll mine = collection(TENANT, 1024, "tenant", true);
        liveChunks(mine, "k", 4, 4);
        // the other tenant registers the SAME collection name and stores unassigned live chunks in it
        Coll theirs = otherTenantSameName(mine);
        liveChunks(theirs, "x", 7, 0);

        TaxonomyCoverage coverage = fresh();
        assertThat(read(coverage, mine)).as("only this tenant's four chunks").isEqualTo(new Counts(true, 4, 0));
        assertThat(read(coverage, theirs)).as("and theirs are theirs").isEqualTo(new Counts(true, 7, 7));
    }

    private Coll otherTenantSameName(Coll mine) throws Exception {
        Coll[] out = new Coll[1];
        seed(dsl -> {
            PgContainerHelper.insertCollection(dsl, OTHER_TENANT, mine.name());
            String model = PgContainerHelper.collectionModel(dsl, OTHER_TENANT, mine.name());
            long topic = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, OTHER_TENANT).set(TOPICS.LABEL, "theirs").set(TOPICS.COLLECTION, mine.name()).set(TOPICS.COLLECTION_ID, CollectionIds.idOf(OTHER_TENANT, mine.name()))
                .set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            dsl.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, OTHER_TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, mine.name()).set(TAXONOMY_CENTROIDS.COLLECTION_ID, CollectionIds.idOf(OTHER_TENANT, mine.name()))
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, topic).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
                .set(TAXONOMY_CENTROIDS.LABEL, "theirs")
                .set(VectorSeeds.centroidEmbedding(mine.dim()), VectorSeeds.vecXY(mine.dim(), 1f, 0f))
                .execute();
            out[0] = new Coll(OTHER_TENANT, mine.dim(), mine.name(), model, topic);
        });
        return out[0];
    }

    /**
     * The count over 50,000 live chunks, 5 of them unassigned, in one statement and a bounded time, first with the
     * planner holding NO statistics on the manifest (catalog_document_chunks), then with the manifest analyzed. The
     * first state is a fresh install, a restore or a tenant import between a large ingest and autovacuum's first
     * ANALYZE: taxonomy-022's per-chunk chunk_live_owners probe read the whole manifest once per chunk there and ran
     * past the 30 s bound (develop CI, ea8a47528); taxonomy-023 counts set-based. Autovacuum is off for the manifest
     * and its statistics are cleared, so the state is pinned and asserted rather than left to a race.
     */
    @Test
    void scale_fiftyThousandLiveChunks_areCountedInABoundedTime_withAndWithoutManifestStatistics() throws Exception {
        Coll c = collection(TENANT, 1024, "scale", true);
        int total = 50_000;
        List<Integer> unassignedIdx = List.of(1000, 15000, 25000, 35000, 49000);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext suDsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.setTableStorageParams(su, CATALOG_DOCUMENT_CHUNKS, Map.of("autovacuum_enabled", "false"));
            try {
                PgCatalogProbes.clearStatistics(suDsl, "nexus", "catalog_document_chunks");
                seedScale(c, "scale-doc", total, unassignedIdx);
                assertThat(PgCatalogProbes.statisticRows(suDsl, "nexus", "catalog_document_chunks"))
                    .as("precondition: the manifest has no statistics").isZero();
                assertBoundedCount(c, total, unassignedIdx.size(), "no manifest statistics");

                PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
                assertThat(PgCatalogProbes.statisticRows(suDsl, "nexus", "catalog_document_chunks"))
                    .as("precondition: the manifest is analyzed").isPositive();
                assertBoundedCount(c, total, unassignedIdx.size(), "manifest analyzed");
            } finally {
                PgContainerHelper.resetTableStorageParams(su, CATALOG_DOCUMENT_CHUNKS, "autovacuum_enabled");
            }
        }
    }

    private void assertBoundedCount(Coll c, int total, int unassigned, String state) {
        long started = System.nanoTime();
        Counts counts = read(fresh(), c);
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
        assertThat(counts).as(state).isEqualTo(new Counts(true, total, unassigned));
        assertThat(counts.routable()).isTrue();
        // A bound only: it catches the quadratic plan (over 30 s at 50k), not a constant factor (about 0.2 s here).
        assertThat(elapsedMs).as("%s: 50k live chunks counted in a bounded time", state).isLessThan(15_000L);
    }

    /** {@code total} live chunks of {@code c}, owned by one live document {@code doc}, assigned except at {@code unassignedIdx}. */
    private void seedScale(Coll c, String doc, int total, List<Integer> unassignedIdx) throws Exception {
        seed(dsl -> {
            var series = DSL.generateSeries(1, total).as("s", "i");
            Field<Integer> i = series.field("i", Integer.class);
            dsl.insertInto(CHUNKS,
                    CHUNKS.TENANT_ID,
                    CHUNKS.COLLECTION,
                    CHUNKS.COLLECTION_ID,
                    CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL,
                    CHUNKS.CHUNK_TEXT,
                    CHUNKS.EMBEDDING_1024)
                .select(dsl.select(DSL.val(c.tenant(), CHUNKS.TENANT_ID),
                   DSL.val(c.name(), CHUNKS.COLLECTION),
                   CollectionIds.idOf(DSL.val(c.tenant(), CHUNKS.TENANT_ID), DSL.val(c.name(), CHUNKS.COLLECTION)),
                   indexChash(i),
                   DSL.val(c.model(), CHUNKS.EMBEDDING_MODEL),
                   DSL.inline("text"),
                   DSL.val(VectorSeeds.vecXY(1024, 1f, 0f), CHUNKS.EMBEDDING_1024)).from(series))
                .execute();
            dsl.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, c.tenant()).set(CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(CATALOG_DOCUMENTS.TITLE, doc).onConflictDoNothing().execute();
            dsl.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                    CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION,
                    CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                .select(dsl.select(DSL.val(c.tenant(), CATALOG_DOCUMENT_CHUNKS.TENANT_ID),
                   DSL.inline(doc),
                   i,
                   indexChash(i),
                   i,
                   DSL.val(c.name(), CATALOG_DOCUMENT_CHUNKS.COLLECTION),
                   CollectionIds.idOf(DSL.val(c.tenant(), CATALOG_DOCUMENT_CHUNKS.TENANT_ID), DSL.val(c.name(), CATALOG_DOCUMENT_CHUNKS.COLLECTION)),
                   DSL.val(c.model(), CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)).from(series))
                .execute();
            dsl.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID,
                    TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID,
                    TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .select(dsl.select(DSL.val(c.tenant(), TOPIC_ASSIGNMENTS.TENANT_ID),
                   indexChash(i),
                   DSL.val(c.topic(), TOPIC_ASSIGNMENTS.TOPIC_ID),
                   DSL.inline("centroid"),
                   DSL.val(c.name(), TOPIC_ASSIGNMENTS.SOURCE_COLLECTION),
                   CollectionIds.idOf(DSL.val(c.tenant(), TOPIC_ASSIGNMENTS.TENANT_ID), DSL.val(c.name(), TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)),
                   DSL.val(c.model(), TOPIC_ASSIGNMENTS.EMBEDDING_MODEL))
                    .from(series).where(i.notIn(unassignedIdx)))
                .execute();
        });
    }

    private static Field<byte[]> indexChash(Field<Integer> i) {
        return DSL.function("decode", SQLDataType.BLOB,
            DSL.lpad(DSL.function("to_hex", SQLDataType.VARCHAR, i), DSL.inline(64), DSL.inline("0")),
            DSL.inline("hex"));
    }

    // ── the cache ─────────────────────────────────────────────────────────────

    /** A clock a test moves by hand. */
    private static final class TestClock extends Clock {
        final AtomicReference<Instant> now = new AtomicReference<>(Instant.parse("2026-11-16T00:00:00Z"));

        @Override public java.time.ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(java.time.ZoneId zone) { return this; }
        @Override public Instant instant() { return now.get(); }
        void advance(Duration d) { now.updateAndGet(t -> t.plus(d)); }
    }

    @Test
    void aReadIsServedFromTheCache_untilThatCollectionIsInvalidated() throws Exception {
        Coll c = collection(TENANT, 1024, "cache", true);
        liveChunks(c, "k", 10, 10);
        TaxonomyCoverage coverage = fresh();
        assertThat(read(coverage, c)).isEqualTo(new Counts(true, 10, 0));

        liveChunks(c, "late", 10, 0); // ten more live, unassigned chunks land behind the cache's back
        assertThat(read(coverage, c)).as("served from the cache: it has not been told").isEqualTo(new Counts(true, 10, 0));

        coverage.invalidate(c.tenant(), c.name());
        assertThat(read(coverage, c)).as("recounted after the invalidation").isEqualTo(new Counts(true, 20, 10));
    }

    @Test
    void invalidatingOneCollection_leavesAnotherCollectionsEntry_andAnotherTenantsOfTheSameName() throws Exception {
        Coll a = collection(TENANT, 1024, "keepA", true);
        Coll b = collection(TENANT, 1024, "keepB", true);
        Coll theirs = otherTenantSameName(a);
        liveChunks(a, "k", 4, 4);
        liveChunks(b, "k", 4, 4);
        liveChunks(theirs, "k", 4, 4);
        TaxonomyCoverage coverage = fresh();
        read(coverage, a);
        read(coverage, b);
        read(coverage, theirs);
        liveChunks(a, "late", 4, 0);
        liveChunks(b, "late", 4, 0);
        liveChunks(theirs, "late", 4, 0);

        coverage.invalidate(a.tenant(), a.name());

        assertThat(read(coverage, a)).as("A recounted").isEqualTo(new Counts(true, 8, 4));
        assertThat(read(coverage, b)).as("B still cached: a store to A is not a store to B").isEqualTo(new Counts(true, 4, 0));
        assertThat(read(coverage, theirs)).as("the other tenant's same-named collection is another key")
            .isEqualTo(new Counts(true, 4, 0));
    }

    @Test
    void invalidatingATenant_dropsEveryCollectionOfThatTenant_andNoOtherTenants() throws Exception {
        Coll a = collection(TENANT, 1024, "tenA", true);
        Coll b = collection(TENANT, 1024, "tenB", true);
        Coll theirs = otherTenantSameName(a);
        for (Coll c : List.of(a, b, theirs)) liveChunks(c, "k", 4, 4);
        TaxonomyCoverage coverage = fresh();
        for (Coll c : List.of(a, b, theirs)) read(coverage, c);
        for (Coll c : List.of(a, b, theirs)) liveChunks(c, "late", 4, 0);

        coverage.invalidateTenant(TENANT);

        assertThat(read(coverage, a)).isEqualTo(new Counts(true, 8, 4));
        assertThat(read(coverage, b)).isEqualTo(new Counts(true, 8, 4));
        assertThat(read(coverage, theirs)).as("another tenant is untouched").isEqualTo(new Counts(true, 4, 0));
    }

    @Test
    void anEntryOlderThanTheTenMinuteCeiling_isRecounted_whateverInvalidationDid() throws Exception {
        Coll c = collection(TENANT, 1024, "ttl", true);
        liveChunks(c, "k", 10, 10);
        TestClock clock = new TestClock();
        TaxonomyCoverage coverage = new TaxonomyCoverage(scope, clock, TaxonomyCoverage.DEFAULT_TTL, 100);
        assertThat(TaxonomyCoverage.DEFAULT_TTL).isEqualTo(Duration.ofMinutes(10));
        assertThat(read(coverage, c)).isEqualTo(new Counts(true, 10, 0));
        liveChunks(c, "late", 10, 0); // a write no hook told the cache about

        clock.advance(Duration.ofMinutes(9).plusSeconds(59));
        assertThat(read(coverage, c)).as("one second inside the ceiling: still served").isEqualTo(new Counts(true, 10, 0));

        clock.advance(Duration.ofSeconds(1));
        assertThat(read(coverage, c)).as("at the ceiling: recounted").isEqualTo(new Counts(true, 20, 10));
    }

    @Test
    void aFailedLookup_isEmptyAndCachesNothing_soTheNextRequestTriesAgain() throws Exception {
        TaxonomyCoverage coverage = fresh();
        String unregistered = "knowledge__7fku212-never-registered__voyage-context-3__v1";

        assertThat(coverage.lookup(TENANT, unregistered))
            .as("an unregistered collection: no coverage, no exception (the caller searches unscoped)").isEmpty();

        // the same key, now registered: a cached failure would still answer empty
        seed(dsl -> PgContainerHelper.insertCollection(dsl, TENANT, unregistered));
        Optional<Counts> later = coverage.lookup(TENANT, unregistered);
        assertThat(later).as("the failure was not cached").isPresent();
        assertThat(later.get()).isEqualTo(new Counts(false, 0, 0));
    }

    @Test
    void aStatementThatCannotRun_isEmptyToo() throws Exception {
        Coll c = collection(TENANT, 1024, "broken", true);
        liveChunks(c, "k", 2, 2);
        TaxonomyCoverage warm = fresh();
        read(warm, c); // the collection is in the process-wide registry cache now, so only the count can fail

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(1);
        var closed = new HikariDataSource(cfg);
        closed.close();

        assertThat(new TaxonomyCoverage(new TenantScope(closed)).lookup(c.tenant(), c.name()))
            .as("a pool that cannot hand out a connection: empty, not an exception").isEmpty();
    }

    @Test
    void aFillInFlightWhenItsCollectionIsInvalidated_doesNotCacheWhatItCounted() throws Exception {
        Coll c = collection(TENANT, 1024, "race", true);
        liveChunks(c, "k", 10, 10);
        TaxonomyCoverage coverage = fresh();
        // The read counts 10 of 10, and a store commits and invalidates before the read stores its value.
        coverage.setAfterCountHookForTests(() -> {
            coverage.setAfterCountHookForTests(null);
            try {
                liveChunks(c, "raced", 10, 0);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            coverage.invalidate(c.tenant(), c.name());
        });

        Counts first = read(coverage, c);
        assertThat(first).as("what the racing read itself counted, before the store").isEqualTo(new Counts(true, 10, 0));

        assertThat(read(coverage, c)).as("not the discarded fill: the next read sees the store")
            .isEqualTo(new Counts(true, 20, 10));
    }

    @Test
    void evictingATenant_removesItsEntriesAndNoOthers_andTheNextReadRecounts() throws Exception {
        Coll a = collection(TENANT, 1024, "evictA", true);
        Coll b = collection(TENANT, 1024, "evictB", true);
        Coll theirs = otherTenantSameName(a);
        for (Coll c : List.of(a, b, theirs)) liveChunks(c, "k", 4, 4);
        TaxonomyCoverage coverage = fresh();
        for (Coll c : List.of(a, b, theirs)) read(coverage, c);
        for (Coll c : List.of(a, b, theirs)) liveChunks(c, "late", 4, 0);

        assertThat(coverage.evictTenant(TENANT)).as("the two entries of this tenant").isEqualTo(2);
        assertThat(coverage.evictTenant(TENANT)).as("nothing left to remove").isZero();

        assertThat(read(coverage, a)).isEqualTo(new Counts(true, 8, 4));
        assertThat(read(coverage, theirs)).as("another tenant's entry is kept").isEqualTo(new Counts(true, 4, 0));
    }

    @Test
    void theCacheIsBounded_anOverflowRecountsAndNeverServesAStaleValue() throws Exception {
        Coll a = collection(TENANT, 1024, "capA", true);
        Coll b = collection(TENANT, 1024, "capB", true);
        liveChunks(a, "k", 4, 4);
        liveChunks(b, "k", 4, 4);
        TaxonomyCoverage coverage = new TaxonomyCoverage(scope, Clock.systemUTC(), TaxonomyCoverage.DEFAULT_TTL, 1);
        read(coverage, a);
        liveChunks(a, "late", 4, 0);

        read(coverage, b); // the second key finds the map full and clears it

        assertThat(read(coverage, a)).as("A's entry went with the clear: recounted, current")
            .isEqualTo(new Counts(true, 8, 4));
    }
}
