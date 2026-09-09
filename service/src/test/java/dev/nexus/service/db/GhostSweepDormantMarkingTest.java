// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 (bead nexus-ft04v.3) — {@link CatalogRepository#sweepGhostsAndMarkDormant}
 * and {@link CatalogRepository#ensureGhostSweepRanOnce}.
 *
 * <p>Same-package placement (deliberate, not incidental): {@link
 * CatalogRepository#COLLECTION_SCOPED_TABLES} and its element type {@link
 * CatalogRepository.CollectionScopedTable} were widened from {@code private} to
 * package-private for exactly this test — see both symbols' own javadoc. The
 * parametrised test below iterates the REAL constant directly, so a table added
 * there in the future is covered automatically; it is never a copied inventory
 * (nexus-v6za0 is the standing lesson two separate table lists already drifted
 * once).
 *
 * <p>Two of the fourteen {@link CatalogRepository#COLLECTION_SCOPED_TABLES}
 * entries — {@code catalog_document_chunks} and {@code topic_assignments} —
 * carry a REAL {@code ON DELETE RESTRICT}/{@code ON UPDATE CASCADE} FK to
 * {@code nexus.chunks} ({@code fk_catalog_chunks_chunk} /
 * {@code topic_assignments_chunk_fk}), so a row in either of those tables
 * structurally REQUIRES a matching {@code chunks} row too — a collection whose
 * only content is one of those two is never "only" that table in the strictest
 * sense, but it still proves the same property the bead's TESTS section asks
 * for (the row survives the sweep), and the coupling is a real schema fact, not
 * a fixture shortcut. Both cases are also, for that reason, UNCHANGED rather
 * than dormant (a live {@code chunks} row means {@code
 * nexus.collection_vector_stats} has a row too — see {@code live_chunks},
 * vectors-005-1: a chunk row with NO manifest reference at all counts as live).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class GhostSweepDormantMarkingTest {

    private static final String SVC_ROLE = "svc_ghost_sweep";
    private static final String SVC_PASS = "svc_ghost_sweep_pass";

    private static final String TENANT_PARAM = "ghost-sweep-param";
    private static final String ANCHOR_COLL = "knowledge__gs-param-anchor__minilm-l6-v2-384__v1";
    private static final String ANCHOR_TOPICS_COLL = "knowledge__gs-param-anchor-topics__minilm-l6-v2-384__v1";
    private static final String ANCHOR_DOC = "gs-anchor-doc";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
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
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);

        // Anchor fixtures for the parametrised test below: an existing document and
        // two existing (never-swept-in-an-assertion) collections to hang the
        // FK-required prerequisites of document_aspects/document_highlights/
        // aspect_extraction_queue/catalog_document_chunks/topic_assignments/topics
        // off of, WITHOUT registering those prerequisite rows under the collection
        // actually under test in each parametrised case — keeping each case's own
        // catalog_collections row isolated to (at most) the FK-coupled tables
        // documented in the class javadoc above.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_PARAM, ANCHOR_COLL);
            PgContainerHelper.insertCollection(ctx, TENANT_PARAM, ANCHOR_TOPICS_COLL);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(TENANT_PARAM, ANCHOR_DOC, "Anchor Doc", ANCHOR_COLL).execute();
            // nexus-ft04v.3 fix check: ANCHOR_TOPICS_COLL has NOTHING referencing it
            // until the "topic_assignments" parametrised case runs (that case is what
            // populates a topics row against it) -- but EVERY earlier parametrised
            // iteration ALSO calls sweepGhostsAndMarkDormant(TENANT_PARAM), which scans
            // every catalog_collections row for the tenant, ANCHOR_TOPICS_COLL included.
            // Left unreferenced, the very FIRST iteration's own sweep call deletes it as
            // a ghost, so by the time "topic_assignments" runs, topics_collection_fk has
            // nothing to reference and the insert fails. This keep-alive row (a fixed,
            // never-touched topic id, distinct from every per-case hash below) keeps
            // ANCHOR_TOPICS_COLL non-empty (dormant, not deleted) from the very first
            // sweep call onward -- the same reason ANCHOR_COLL needs no such fix: its own
            // ANCHOR_DOC row above already keeps it non-empty from the start.
            ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                           TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
               .values(9_000_000_000L, TENANT_PARAM, "anchor-topics-keep-alive", ANCHOR_TOPICS_COLL, 0,
                       OffsetDateTime.now(), "pending")
               .execute();
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════
    // PARAMETRISED — a row referenced from table X survives the sweep
    // (RDR-204 bead nexus-ft04v.3 TESTS bullet 1). Non-vacuity: the stream is
    // the REAL CatalogRepository.COLLECTION_SCOPED_TABLES, so an empty or
    // shrunk constant fails this method's own precondition assertion below,
    // not a silently-empty parametrised run.
    // ══════════════════════════════════════════════════════════════════════

    static Stream<CatalogRepository.CollectionScopedTable> scopedTables() {
        assertThat(CatalogRepository.COLLECTION_SCOPED_TABLES)
            .as("non-vacuity precondition: COLLECTION_SCOPED_TABLES must not be empty")
            .isNotEmpty();
        return CatalogRepository.COLLECTION_SCOPED_TABLES.stream();
    }

    /** Tables whose lone row also structurally requires a {@code chunks} row (class javadoc). */
    private static final List<String> CHUNK_COUPLED =
        List.of("chunks", "catalog_document_chunks", "topic_assignments");

    @ParameterizedTest(name = "[{index}] {0}")
    @Order(10)
    @MethodSource("scopedTables")
    void rowReferencedFromTable_survivesSweep(CatalogRepository.CollectionScopedTable t) throws Exception {
        String coll = "knowledge__gs-param-" + t.countKey().replace('_', '-') + "__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_PARAM, coll);
            seedOneRowFor(ctx, t.countKey(), coll);
        }

        repo.sweepGhostsAndMarkDormant(TENANT_PARAM);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_PARAM)).and(CATALOG_COLLECTIONS.NAME.eq(coll))
                .fetchOne();
            assertThat(row).as("row referenced only from " + t.countKey() + " must survive the sweep").isNotNull();
            if (CHUNK_COUPLED.contains(t.countKey())) {
                // RDR-204 nexus-ft04v.4/.5: lifecycle_state is NOT NULL after hygiene-002-1
                // (PgContainerHelper.insertCollection always writes a real value now, "live"
                // for this conformant name), so "UNCHANGED, not dormant" means the seeded
                // value survives untouched, not that the column stays NULL as it could
                // before the walk/constraints landed.
                assertThat(row.value1())
                    .as(t.countKey() + " carries a live chunks row (FK-coupled) -> UNCHANGED, not dormant")
                    .isEqualTo("live");
            } else {
                assertThat(row.value1())
                    .as(t.countKey() + " has no collection_vector_stats row -> dormant")
                    .isEqualTo("dormant");
            }
        }
    }

    /** One minimal row in table {@code countKey}, referencing {@code coll} under {@link #TENANT_PARAM}. */
    private static void seedOneRowFor(DSLContext ctx, String countKey, String coll) {
        switch (countKey) {
            case "chunks" -> insertChunk384(ctx, TENANT_PARAM, coll, chashBytes(coll), vector(384));
            case "catalog_document_chunks" -> {
                insertChunk384(ctx, TENANT_PARAM, coll, chashBytes(coll + "-mf"), vector(384));
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                               CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                               CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                   .values(TENANT_PARAM, ANCHOR_DOC, 0, chashBytes(coll + "-mf"), coll)
                   .execute();
            }
            case "topic_assignments" -> {
                long topicId = Math.abs((long) (coll + "-anchor-topic").hashCode());
                ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                               TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                   .values(topicId, TENANT_PARAM, "anchor-topic", ANCHOR_TOPICS_COLL, 0, OffsetDateTime.now(),
                           "pending")
                   .execute();
                insertChunk384(ctx, TENANT_PARAM, coll, hexChashBytes(coll + "-ta"), vector(384));
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                               TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                               TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                   .values(TENANT_PARAM, hexChashBytes(coll + "-ta"), topicId, "projection", coll,
                           OffsetDateTime.now())
                   .execute();
            }
            case "topics" -> {
                long topicId = Math.abs((long) (coll + "-topic").hashCode());
                ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                               TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                   .values(topicId, TENANT_PARAM, "topic-gs", coll, 0, OffsetDateTime.now(), "pending")
                   .execute();
            }
            case "taxonomy_meta" -> ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
                .values(TENANT_PARAM, coll).execute();
            case "taxonomy_centroids" -> {
                long topicId = Math.abs((long) (coll + "-centroid").hashCode());
                ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                               TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL,
                               TAXONOMY_CENTROIDS.EMBEDDING_384)
                   .values(TENANT_PARAM, coll, topicId, "", vector(384))
                   .execute();
            }
            case "document_aspects" -> ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID,
                               DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                               DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                               DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID,
                               DOCUMENT_ASPECTS.SOURCE_URI)
                .values(TENANT_PARAM, coll, "/p/" + coll + ".md", OffsetDateTime.now(), "v1", "docling", ANCHOR_DOC,
                        "file:///p/" + coll + ".md")
                .execute();
            case "document_highlights" -> ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID,
                               DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.COLLECTION,
                               DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                               DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(TENANT_PARAM, ANCHOR_DOC, coll, "file:///" + coll + "-hl", "hi", OffsetDateTime.now())
                .execute();
            case "aspect_extraction_queue" -> ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                               ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                               ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                               ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .values(TENANT_PARAM, coll, "/p/" + coll + "-q.md", "pending", OffsetDateTime.now(), ANCHOR_DOC)
                .execute();
            case "catalog_documents" -> ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID,
                               CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                               CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                .values(TENANT_PARAM, "gs-doc-" + coll, "Doc", coll)
                .execute();
            case "relevance_log" -> ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY,
                               RELEVANCE_LOG.CHUNK_ID, RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION,
                               RELEVANCE_LOG.SESSION_ID, RELEVANCE_LOG.TIMESTAMP)
                .values(TENANT_PARAM, "q1", hexChash(coll), coll, "click", "s1", OffsetDateTime.now())
                .execute();
            case "search_telemetry" -> ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID,
                               SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH, SEARCH_TELEMETRY.COLLECTION,
                               SEARCH_TELEMETRY.RAW_COUNT, SEARCH_TELEMETRY.KEPT_COUNT)
                .values(TENANT_PARAM, OffsetDateTime.now(), "qh-" + coll, coll, 10, 5)
                .execute();
            case "hook_failures" -> ctx.insertInto(HOOK_FAILURES, HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.DOC_ID,
                               HOOK_FAILURES.COLLECTION, HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.ERROR,
                               HOOK_FAILURES.OCCURRED_AT)
                .values(TENANT_PARAM, "gs-hf-doc", coll, "post_store", "boom", OffsetDateTime.now())
                .execute();
            case "gc_audit" -> ctx.insertInto(GC_AUDIT, GC_AUDIT.TENANT_ID, GC_AUDIT.OPERATION, GC_AUDIT.COLLECTION)
                .values(TENANT_PARAM, "purge", coll)
                .execute();
            default -> throw new IllegalArgumentException(
                "no seed case for COLLECTION_SCOPED_TABLES entry '" + countKey + "' -- "
                + "add one here (GhostSweepDormantMarkingTest.seedOneRowFor), not a skip");
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    // An unreferenced row is deleted (TESTS bullet 2).
    // ══════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void unreferencedRow_isDeleted() throws Exception {
        String tenant = "ghost-sweep-unref";
        String coll = "knowledge__gs-unref__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, coll);
        }
        CollectionRegistry.markKnown(tenant, coll,
            new CollectionRow("knowledge", "gs-unref", "minilm-l6-v2-384", 384, "live"));

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.scanned()).isEqualTo(1);
        assertThat(result.ghostsDeleted()).isEqualTo(1);
        assertThat(result.markedDormant()).isEqualTo(0);
        assertThat(CollectionRegistry.isKnown(tenant, coll))
            .as("registry cache evicted post-sweep, mirroring deleteCollection's discipline").isFalse();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))))
                .as("ghost row must be gone").isFalse();
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    // A row marked DORMANT is evicted from CollectionRegistry too, not just
    // a deleted one (RDR-204 Phase 2 fix round, nexus-ft04v.18 substantive-
    // critique C2: the MARKED_DORMANT branch used to write lifecycle_state
    // with no eviction, an asymmetry with the DELETED branch above).
    // ══════════════════════════════════════════════════════════════════════

    @Test @Order(25)
    void dormantMarking_evictsCollectionRegistry_mirroringDeleteDiscipline() throws Exception {
        String tenant = "ghost-sweep-dormant-evict";
        String coll = "knowledge__gs-dormant-evict__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, coll);
            // A taxonomy_meta row keeps the collection non-empty (collectionIsEmpty
            // false) with no collection_vector_stats row -- MARKED_DORMANT, not
            // DELETED, exactly like countsLoggedPerTenant's "dormant" fixture above.
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .values(tenant, coll).execute();
        }
        CollectionRegistry.markKnown(tenant, coll,
            new CollectionRow("knowledge", "gs-dormant-evict", "minilm-l6-v2-384", 384, "live"));
        assertThat(CollectionRegistry.cached(tenant, coll))
            .as("precondition: the row is cached before the sweep runs").isPresent();

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.markedDormant()).isEqualTo(1);
        assertThat(result.ghostsDeleted()).isEqualTo(0);
        assertThat(CollectionRegistry.cached(tenant, coll))
            .as("dormant-marking must evict the cache exactly like deleteCollection does -- "
                + "a reader trusting a stale cached row would never see the lifecycle_state flip")
            .isEmpty();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))
                .fetchOne();
            assertThat(row.value1()).as("the row itself is genuinely dormant, not deleted")
                .isEqualTo("dormant");
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    // MULTI-TENANT: each tenant is swept at its OWN first request; neither
    // is swept by the other's (TESTS bullet 3).
    // ══════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void multiTenant_eachSweptAtItsOwnFirstRequest_neitherByTheOthers() throws Exception {
        String tenantA = "ghost-sweep-mt-a";
        String tenantB = "ghost-sweep-mt-b";
        String ghostA = "knowledge__gs-mt-ghost-a__minilm-l6-v2-384__v1";
        String ghostB = "knowledge__gs-mt-ghost-b__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenantA, ghostA);
            PgContainerHelper.insertCollection(ctx, tenantB, ghostB);
        }

        repo.ensureGhostSweepRanOnce(tenantA);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenantA)).and(CATALOG_COLLECTIONS.NAME.eq(ghostA))))
                .as("tenant A's own ghost is swept at its first request").isFalse();
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenantB)).and(CATALOG_COLLECTIONS.NAME.eq(ghostB))))
                .as("tenant B is untouched by tenant A's sweep").isTrue();
        }

        repo.ensureGhostSweepRanOnce(tenantB);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenantB)).and(CATALOG_COLLECTIONS.NAME.eq(ghostB))))
                .as("tenant B's own ghost is swept at ITS first request").isFalse();
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    // The marker makes a second boot a no-op, per tenant (TESTS bullet 4).
    // ══════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void markerMakesSecondBootANoOp() throws Exception {
        String tenant = "ghost-sweep-second-boot";
        String firstGhost = "knowledge__gs-boot1-ghost__minilm-l6-v2-384__v1";
        String secondGhost = "knowledge__gs-boot2-ghost__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, firstGhost);
        }

        // "Boot 1": a fresh CatalogRepository instance (fresh in-process gate) sweeps
        // this tenant for the first time and writes the durable catalog_meta marker.
        CatalogRepository boot1 = new CatalogRepository(scope);
        boot1.ensureGhostSweepRanOnce(tenant);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(firstGhost))))
                .as("boot 1 sweeps the tenant's ghost").isFalse();
        }

        // A collection that becomes a ghost between boots.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, secondGhost);
        }

        // "Boot 2": a SECOND fresh CatalogRepository instance (empty in-process gate,
        // same underlying database/marker) must find the durable marker and skip the
        // sweep entirely -- the new ghost must survive untouched.
        CatalogRepository boot2 = new CatalogRepository(scope);
        boot2.ensureGhostSweepRanOnce(tenant);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(secondGhost))))
                .as("marker makes boot 2 a no-op -- the post-boot-1 ghost is never swept").isTrue();
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    // Counts reported per tenant through structured logging (TESTS bullet 5).
    // ══════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void countsLoggedPerTenant() throws Exception {
        String tenant = "ghost-sweep-logging";
        String ghost = "knowledge__gs-log-ghost__minilm-l6-v2-384__v1";
        String dormant = "knowledge__gs-log-dormant__minilm-l6-v2-384__v1";
        String unchanged = "knowledge__gs-log-unchanged__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, ghost);
            PgContainerHelper.insertCollection(ctx, tenant, dormant);
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .values(tenant, dormant).execute();
            PgContainerHelper.insertCollection(ctx, tenant, unchanged);
            insertChunk384(ctx, tenant, unchanged, chashBytes(unchanged), vector(384));
        }

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            repo.ensureGhostSweepRanOnce(tenant);
            var matches = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .filter(m -> m.startsWith("event=rdr204_ghost_sweep "))
                .toList();
            assertThat(matches).as("exactly one ghost-sweep log line for this tenant").hasSize(1);
            String line = matches.getFirst();
            assertThat(line).contains("tenant=" + tenant);
            assertThat(line).contains("scanned=3");
            assertThat(line).contains("deleted=1");
            assertThat(line).contains("dormant=1");
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    // QUARANTINE RECLAIM (nexus-n060e, refining nexus-snm4y): a row already
    // lifecycle_state = 'quarantine' is held by the sweep ONLY while it is
    // still referenced somewhere. A quarantine row that is a TRUE ghost --
    // collectionIsEmpty is true, nothing in ANY COLLECTION_SCOPED_TABLES
    // entry names it -- is deleted exactly like any other ghost, counted in
    // ghostsDeleted (never quarantineHeld), and evicted from the registry.
    // ══════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void quarantineGhostRow_isDeletedAsGhost() throws Exception {
        String tenant = "ghost-sweep-quarantine-ghost";
        String coll = "quarantine-knowledge__gs-q-ghost__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, coll);
        }
        CollectionRegistry.markKnown(tenant, coll,
            new CollectionRow("quarantine-knowledge", "gs-q-ghost", "minilm-l6-v2-384", 384, "quarantine"));

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.scanned()).isEqualTo(1);
        assertThat(result.ghostsDeleted())
            .as("a drained quarantine row is a ghost like any other and is reclaimed").isEqualTo(1);
        assertThat(result.markedDormant()).isEqualTo(0);
        assertThat(result.quarantineHeld())
            .as("a deleted quarantine ghost is counted as deleted, never as held").isEqualTo(0);
        assertThat(CollectionRegistry.isKnown(tenant, coll))
            .as("the reclaimed quarantine row is evicted from the registry like any other delete").isFalse();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))))
                .as("the drained quarantine row must be gone").isFalse();
        }
    }

    @Test @Order(62)
    void quarantineReferencedNoVectorStats_heldAsQuarantine_notMarkedDormant() throws Exception {
        String tenant = "ghost-sweep-quarantine-referenced";
        String coll = "quarantine-knowledge__gs-q-referenced__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, coll);
            // A taxonomy_meta row keeps collectionIsEmpty false (referenced), with no
            // collection_vector_stats row -- exactly the shape that flips a non-quarantine
            // row to 'dormant' (see dormantMarking_evictsCollectionRegistry_mirroringDeleteDiscipline).
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .values(tenant, coll).execute();
        }

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.markedDormant())
            .as("a referenced-but-empty quarantine row is held, never marked dormant").isEqualTo(0);
        assertThat(result.ghostsDeleted()).isEqualTo(0);
        assertThat(result.quarantineHeld()).isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))
                .fetchOne();
            assertThat(row.value1()).as("lifecycle_state stays 'quarantine', not flipped to 'dormant'")
                .isEqualTo("quarantine");
        }
    }

    @Test @Order(64)
    void quarantineWithLiveChunks_unchanged() throws Exception {
        String tenant = "ghost-sweep-quarantine-live-chunks";
        String coll = "quarantine-knowledge__gs-q-live__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, coll);
            insertChunk384(ctx, tenant, coll, chashBytes(coll), vector(384));
        }

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.quarantineHeld()).isEqualTo(1);
        assertThat(result.ghostsDeleted()).isEqualTo(0);
        assertThat(result.markedDormant()).isEqualTo(0);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))
                .fetchOne();
            assertThat(row.value1()).as("a quarantine row with live chunks is unchanged, still 'quarantine'")
                .isEqualTo("quarantine");
        }
    }

    /**
     * All four dispositions ({@code DELETED}, {@code MARKED_DORMANT},
     * {@code HELD_QUARANTINE}, {@code UNCHANGED}) in the SAME sweep, side
     * by side: a drained quarantine
     * ghost, a referenced-empty quarantine row, a live-chunks quarantine
     * row, a non-quarantine ghost, a non-quarantine referenced-empty row,
     * and a non-quarantine live-chunks row -- one sweep call, one set of
     * exact counts (nexus-n060e). Proves the quarantine reclaim does not
     * perturb how the other three dispositions are counted, and that a
     * quarantine ghost lands in {@code ghostsDeleted}, never {@code
     * quarantineHeld}.
     */
    @Test @Order(66)
    void mixedSweep_reportsExactCountsAcrossAllFourDispositions() throws Exception {
        String tenant = "ghost-sweep-quarantine-mixed";
        String qGhost = "quarantine-knowledge__gs-q-mixed-ghost__minilm-l6-v2-384__v1";
        String qReferenced = "quarantine-knowledge__gs-q-mixed-referenced__minilm-l6-v2-384__v1";
        String qLive = "quarantine-knowledge__gs-q-mixed-live__minilm-l6-v2-384__v1";
        String ghost = "knowledge__gs-mixed-ghost__minilm-l6-v2-384__v1";
        String dormant = "knowledge__gs-mixed-dormant__minilm-l6-v2-384__v1";
        String unchanged = "knowledge__gs-mixed-unchanged__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, qGhost);
            PgContainerHelper.insertCollection(ctx, tenant, qReferenced);
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .values(tenant, qReferenced).execute();
            PgContainerHelper.insertCollection(ctx, tenant, qLive);
            insertChunk384(ctx, tenant, qLive, chashBytes(qLive), vector(384));
            PgContainerHelper.insertCollection(ctx, tenant, ghost);
            PgContainerHelper.insertCollection(ctx, tenant, dormant);
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .values(tenant, dormant).execute();
            PgContainerHelper.insertCollection(ctx, tenant, unchanged);
            insertChunk384(ctx, tenant, unchanged, chashBytes(unchanged), vector(384));
        }

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.scanned()).isEqualTo(6);
        assertThat(result.ghostsDeleted())
            .as("the drained quarantine ghost AND the non-quarantine ghost are both reclaimed").isEqualTo(2);
        assertThat(result.markedDormant()).as("only the non-quarantine referenced-empty row is dormant")
            .isEqualTo(1);
        assertThat(result.quarantineHeld())
            .as("only the two STILL-REFERENCED quarantine rows are held; the drained one is not")
            .isEqualTo(2);
        int accountedFor = result.ghostsDeleted() + result.markedDormant() + result.quarantineHeld();
        assertThat(result.scanned() - accountedFor)
            .as("exactly one row (the non-quarantine live-chunks row) is UNCHANGED").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(qGhost))))
                .as("the drained quarantine ghost row must be gone").isFalse();
            var unchangedRow = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(unchanged))
                .fetchOne();
            assertThat(unchangedRow.value1()).as("the live-chunks non-quarantine row is untouched")
                .isEqualTo("live");
        }
    }

    /**
     * Non-vacuity, part 1 (bead nexus-n060e TESTS bullet 5): the EXACT
     * fixture from {@link #quarantineGhostRow_isDeletedAsGhost} but with
     * {@code lifecycle_state = 'live'} instead of {@code 'quarantine'} IS
     * ALSO deleted. Both a quarantine ghost and a live-state ghost land in
     * {@code ghostsDeleted} today, so this alone would not distinguish "the
     * quarantine branch fires" from "ghosts are always deleted regardless
     * of state" -- part 2 below is what isolates the quarantine branch's
     * actual effect.
     */
    @Test @Order(68)
    void nonVacuity_sameGhostFixtureWithLiveState_isDeleted() throws Exception {
        String tenant = "ghost-sweep-quarantine-nonvacuity";
        String coll = "knowledge__gs-q-nonvacuity-live__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, coll);
        }

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.quarantineHeld()).isEqualTo(0);
        assertThat(result.ghostsDeleted())
            .as("the identical ghost shape, with lifecycle_state='live', IS deleted").isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))))
                .as("the live-state ghost row must be gone").isFalse();
        }
    }

    /**
     * Non-vacuity, part 2 (bead nexus-n060e TESTS bullet 5) -- the one that
     * actually isolates the quarantine branch's effect: the EXACT fixture
     * from {@link #quarantineReferencedNoVectorStats_heldAsQuarantine_notMarkedDormant}
     * (referenced by a manifest row, no {@code collection_vector_stats} row)
     * but seeded {@code lifecycle_state = 'live'} instead of {@code
     * 'quarantine'} IS marked {@code dormant}. Held-quarantine and
     * non-vacuity part 1 together would both pass even if the quarantine
     * branch never ran (a ghost is deleted either way, and a referenced
     * quarantine row surviving is consistent with "sweep does nothing to
     * quarantine rows"); this test is what proves the quarantine check is
     * the ONLY thing standing between this exact row shape and a 'dormant'
     * relabel -- the same shape with 'live' state takes the dormant branch
     * every time, so 'quarantine' state must be what redirects it to held.
     */
    @Test @Order(69)
    void nonVacuity_sameReferencedNoStatsFixtureWithLiveState_isMarkedDormant() throws Exception {
        String tenant = "ghost-sweep-quarantine-nonvacuity-dormant";
        String coll = "knowledge__gs-q-nonvacuity-dormant__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, coll);
            ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .values(tenant, coll).execute();
        }

        CatalogRepository.GhostSweepResult result = repo.sweepGhostsAndMarkDormant(tenant);

        assertThat(result.quarantineHeld()).isEqualTo(0);
        assertThat(result.ghostsDeleted()).isEqualTo(0);
        assertThat(result.markedDormant())
            .as("the identical referenced-no-stats shape, with lifecycle_state='live', "
                + "IS marked dormant -- proving 'quarantine' state is what prevented the relabel")
            .isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))
                .fetchOne();
            assertThat(row.value1()).as("the live-state row is genuinely flipped to dormant")
                .isEqualTo("dormant");
        }
    }

    // ── fixture helpers (typed jOOQ DSL only, mirrors CatalogRenameCollectionTest) ──

    private static void insertChunk384(DSLContext ctx, String tenant, String collection, byte[] chashBytes,
                                        Vector v) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(tenant, collection, chashBytes, "text", v)
           .execute();
    }

    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    private static byte[] chashBytes(String seed) {
        String label = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return label.getBytes(StandardCharsets.US_ASCII);
    }

    private static byte[] hexChashBytes(String seed) {
        return java.util.HexFormat.of().parseHex(hexChash(seed));
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
}
