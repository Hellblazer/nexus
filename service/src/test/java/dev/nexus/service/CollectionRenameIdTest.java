// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogMoves;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
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
 * RDR-229 P3.1 stage 2b (nexus-p2m1x.17), catalog family: a name-path rename, a re-home, the chash rename and the
 * curator moves give every row they rewrite the id of the registry row the name now points at.
 *
 * <p>The bead's test: "a name-path rename gives every rewritten row, and the collection's chunk_orphaned_at rows,
 * the new registry row's id." The check is one invariant, run after each operation over every table the writers
 * touch: no row's id disagrees with the registry row of its own name. The rows the chunk foreign keys carry (the
 * manifest, the topic assignments, chunk_orphaned_at) are the case that a plain UPDATE of {@code chunks.collection}
 * would leave behind, so each seed holds one.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRenameIdTest {

    private static final String T = "rn-id";
    private static final String SVC_ROLE = "svc_rn_id";
    private static final String SVC_PASS = "svc_rn_id_pass";

    /** A table, its tenant and name columns, and its id column. */
    private record Probe(String label, Table<?> table, Field<String> tenant, Field<String> name, Field<Long> id) {}

    private static final List<Probe> PROBES = List.of(
        new Probe("chunks", CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID),
        new Probe("catalog_document_chunks", CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
            CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID),
        new Probe("chunk_orphaned_at", CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
            CHUNK_ORPHANED_AT.COLLECTION_ID),
        new Probe("topic_assignments", TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID,
            TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID),
        new Probe("topics", TOPICS, TOPICS.TENANT_ID, TOPICS.COLLECTION, TOPICS.COLLECTION_ID),
        new Probe("taxonomy_meta", TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION,
            TAXONOMY_META.COLLECTION_ID),
        new Probe("taxonomy_centroids", TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID,
            TAXONOMY_CENTROIDS.COLLECTION, TAXONOMY_CENTROIDS.COLLECTION_ID),
        new Probe("document_aspects", DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
            DOCUMENT_ASPECTS.COLLECTION_ID),
        new Probe("document_highlights", DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID,
            DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.COLLECTION_ID),
        new Probe("catalog_documents", CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID,
            CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID),
        new Probe("relevance_log", RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.COLLECTION,
            RELEVANCE_LOG.COLLECTION_ID),
        new Probe("search_telemetry", SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.COLLECTION,
            SEARCH_TELEMETRY.COLLECTION_ID),
        new Probe("hook_failures", HOOK_FAILURES, HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.COLLECTION,
            HOOK_FAILURES.COLLECTION_ID),
        new Probe("gc_audit", GC_AUDIT, GC_AUDIT.TENANT_ID, GC_AUDIT.COLLECTION, GC_AUDIT.COLLECTION_ID));

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    ChashRepository chash;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        var scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);
        chash = new ChashRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private <R> R su(Function<DSLContext, R> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private Long regId(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private static byte[] bytes(String hex) {
        return HexFormat.of().parseHex(hex);
    }

    private static String hex(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    /** A collection holding one row in every table of {@link #PROBES}, each carrying the collection's id. */
    private void seed(DSLContext ctx, String coll, String p) {
        PgContainerHelper.insertCollection(ctx, T, coll);
        final Long id = CollectionIds.require(ctx, T, coll);
        final String model = PgContainerHelper.collectionModel(ctx, T, coll);
        final String doc = p + "-doc";
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .values(T, doc, "Doc " + p, coll, id).execute();
        PgContainerHelper.insertChunk384(ctx, T, coll, bytes(hex(p + "1")), VectorSeeds.vec(384, 0.1f));
        PgContainerHelper.insertChunk384(ctx, T, coll, bytes(hex(p + "2")), VectorSeeds.vec(384, 0.1f));
        CollectionIdSeeds.manifestRows(ctx, T, doc, coll, 0, hex(p + "1"));
        // chunk 2 has no manifest owner: its orphaned-at record is the row the chunk cascade carries without its id
        ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
                CHUNK_ORPHANED_AT.COLLECTION_ID, CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.EMBEDDING_MODEL,
                CHUNK_ORPHANED_AT.ORPHANED_AT)
            .values(T, coll, id, bytes(hex(p + "2")), model, OffsetDateTime.now()).onConflictDoNothing().execute();
        long topicId = Math.abs((long) (T + coll).hashCode());
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.COLLECTION_ID,
                TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(topicId, T, "topic-" + p, coll, id, 0, OffsetDateTime.now(), "pending").execute();
        ctx.insertInto(TAXONOMY_META, TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION, TAXONOMY_META.COLLECTION_ID)
            .values(T, coll, id).execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
            .values(T, bytes(hex(p + "1")), topicId, "projection", coll, id, OffsetDateTime.now(), model).execute();
        ctx.insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.COLLECTION_ID, TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL,
                TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_384)
            .values(T, coll, id, topicId, model, "", VectorSeeds.vec(384, 0.1f)).execute();
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT,
                DOCUMENT_ASPECTS.MODEL_VERSION, DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID,
                DOCUMENT_ASPECTS.SOURCE_URI)
            .values(T, coll, id, "/p/" + p + ".md", OffsetDateTime.now(), "v1", "docling", doc, "file:///p/" + p + ".md")
            .execute();
        ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.COLLECTION_ID, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
            .values(T, doc, coll, id, "file:///" + p + "-hl", "hi", OffsetDateTime.now()).execute();
        ctx.insertInto(RELEVANCE_LOG, RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.COLLECTION_ID, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                RELEVANCE_LOG.TIMESTAMP)
            .values(T, "q", hex(p + "1"), coll, id, "click", "s1", OffsetDateTime.now()).execute();
        ctx.insertInto(SEARCH_TELEMETRY, SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.COLLECTION_ID, SEARCH_TELEMETRY.RAW_COUNT,
                SEARCH_TELEMETRY.KEPT_COUNT)
            .values(T, OffsetDateTime.now(), "qh-" + p, coll, id, 10, 5).execute();
        ctx.insertInto(HOOK_FAILURES, HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.DOC_ID, HOOK_FAILURES.COLLECTION,
                HOOK_FAILURES.COLLECTION_ID, HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.ERROR, HOOK_FAILURES.OCCURRED_AT)
            .values(T, doc, coll, id, "post_store", "boom", OffsetDateTime.now()).execute();
        ctx.insertInto(GC_AUDIT, GC_AUDIT.TENANT_ID, GC_AUDIT.OPERATION, GC_AUDIT.COLLECTION, GC_AUDIT.COLLECTION_ID)
            .values(T, "purge", coll, id).execute();
    }

    private int rowsNamed(String coll) {
        return su(ctx -> {
            int n = 0;
            for (Probe p : PROBES) {
                n += ctx.fetchCount(p.table(), p.tenant().eq(T).and(p.name().eq(coll)));
            }
            return n;
        });
    }

    /** The invariant: no row of any probed table carries an id other than its own name's registry id. */
    private void assertIdsAgreeWithNames(String when) {
        su(ctx -> {
            for (Probe p : PROBES) {
                int bad = ctx.selectCount().from(p.table())
                    .join(CATALOG_COLLECTIONS).on(CATALOG_COLLECTIONS.TENANT_ID.eq(p.tenant()))
                    .and(CATALOG_COLLECTIONS.NAME.eq(p.name()))
                    .where(p.tenant().eq(T)).and(p.id().isDistinctFrom(CATALOG_COLLECTIONS.COLLECTION_ID))
                    .fetchOne(0, int.class);
                assertThat(bad).as(when + ": " + p.label() + " rows whose id disagrees with their name's registry id")
                    .isZero();
            }
            return null;
        });
    }

    // ── tests ─────────────────────────────────────────────────────────────────

    @Test
    void nameRename_givesEveryRewrittenRowAndTheOrphanedAtRowsTheNewRegistryRowsId() {
        String x = "knowledge__rn-x__minilm-l6-v2-384__v1";
        String y = "knowledge__rn-y__minilm-l6-v2-384__v1";
        su(ctx -> {
            seed(ctx, x, "rx");
            return null;
        });
        Long idX = regId(x);
        int seeded = rowsNamed(x);
        assertThat(seeded).as("precondition: at least one row in each probed table").isGreaterThanOrEqualTo(PROBES.size());

        repo.renameCollection(T, x, y);

        Long idY = regId(y);
        assertThat(idY).as("the rename inserted a new registry row, so a new id").isNotNull().isNotEqualTo(idX);
        assertThat(rowsNamed(y)).as("every row moved to the new name").isEqualTo(seeded);
        assertIdsAgreeWithNames("after the rename");
        // chunk_orphaned_at is not in COLLECTION_SCOPED_TABLES: the chunk cascade renames it
        Long orphanedId = su(ctx -> ctx.select(CHUNK_ORPHANED_AT.COLLECTION_ID).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(T)).and(CHUNK_ORPHANED_AT.COLLECTION.eq(y))
            .fetchOne(CHUNK_ORPHANED_AT.COLLECTION_ID));
        assertThat(orphanedId).isEqualTo(idY);
        Long tombstoneId = su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(x))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        assertThat(tombstoneId).as("the retired old name points at the new row's id").isEqualTo(idY);

        // the round trip revives the old registry row, which keeps its id; the rows follow it back
        repo.renameCollection(T, y, x);
        assertThat(regId(x)).as("a revived tombstone keeps its id").isEqualTo(idX);
        assertIdsAgreeWithNames("after the round trip");
        assertThat(rowsNamed(x)).isEqualTo(seeded);
    }

    @Test
    void rehome_givesTheMovedRowsTheTargetsId() {
        String x = "knowledge__rh-x__minilm-l6-v2-384__v1";
        String z = "knowledge__rh-z__minilm-l6-v2-384__v1";
        su(ctx -> {
            seed(ctx, x, "hx");
            seed(ctx, z, "hz");
            return null;
        });
        CatalogRepository.RehomeResult r = repo.rehomeCollection(T, x, z);
        assertThat(r.movedChunks()).isEqualTo(2);
        assertIdsAgreeWithNames("after the re-home");
        Long idZ = regId(z);
        long chunksUnderZ = su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(z))
            .and(CHUNKS.COLLECTION_ID.eq(idZ))));
        assertThat(chunksUnderZ).as("the moved chunks and the target's own chunks all carry the target's id").isEqualTo(4);
        long orphanedUnderZ = su(ctx -> ctx.fetchCount(CHUNK_ORPHANED_AT,
            CHUNK_ORPHANED_AT.TENANT_ID.eq(T).and(CHUNK_ORPHANED_AT.COLLECTION.eq(z))
                .and(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(idZ))));
        assertThat(orphanedUnderZ).as("the orphaned-at records the cascade moved carry it too").isEqualTo(2);
    }

    @Test
    void chashRename_givesTheRowsTheNewCollectionsId() {
        String x = "knowledge__ch-x__minilm-l6-v2-384__v1";
        String y = "knowledge__ch-y__minilm-l6-v2-384__v1";
        su(ctx -> {
            seed(ctx, x, "cx");
            PgContainerHelper.insertCollection(ctx, T, y);
            return null;
        });
        chash.renameCollection(T, x, y);
        su(ctx -> {
            for (Probe p : List.of(PROBES.get(0), PROBES.get(1), PROBES.get(2), PROBES.get(3))) {
                int bad = ctx.selectCount().from(p.table())
                    .join(CATALOG_COLLECTIONS).on(CATALOG_COLLECTIONS.TENANT_ID.eq(p.tenant()))
                    .and(CATALOG_COLLECTIONS.NAME.eq(p.name()))
                    .where(p.tenant().eq(T)).and(p.name().eq(y)).and(p.id().isDistinctFrom(CATALOG_COLLECTIONS.COLLECTION_ID))
                    .fetchOne(0, int.class);
                assertThat(bad).as("chash rename: " + p.label()).isZero();
                assertThat(ctx.fetchCount(p.table(), p.tenant().eq(T).and(p.name().eq(y)))).as(p.label() + " moved")
                    .isPositive();
            }
            return null;
        });
    }

    // ── the curator moves, on rows the stamp has not reached ──────────────────

    @Test
    void documentMove_stampsTheUnstampedRowsItRewrites() {
        String coll = "knowledge__mv-x__minilm-l6-v2-384__v1";
        repo.upsertOwner(T, Map.of("tumbler_prefix", "1.9", "name", "curator-mv", "owner_type", "curator",
            "repo_hash", "", "description", "", "repo_root", ""));
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, coll);
            P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            P31CheckReplay.lift(ctx, P31CheckReplay.DOCUMENT_ASPECTS);
            P31CheckReplay.lift(ctx, P31CheckReplay.DOCUMENT_HIGHLIGHTS);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.FILE_PATH)
                .values(T, "1.9.1", "moving", coll, "file:///old/a.md", "/old/a.md").execute();
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(T, coll, "/old/a.md", OffsetDateTime.now(), "v1", "docling", "1.9.1", "file:///old/a.md").execute();
            ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(T, "1.9.1", coll, "file:///old/a.md", "hi", OffsetDateTime.now()).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            P31CheckReplay.restore(ctx, P31CheckReplay.DOCUMENT_ASPECTS);
            P31CheckReplay.restore(ctx, P31CheckReplay.DOCUMENT_HIGHLIGHTS);
            return null;
        });
        new CatalogMoves(repo).moveDocument(T, "1.9.1", "file:///new/a.md", "/new/a.md", false);
        Long id = regId(coll);
        su(ctx -> {
            assertThat(ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.9.1"))
                .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)).as("document").isEqualTo(id);
            assertThat(ctx.select(DOCUMENT_ASPECTS.COLLECTION_ID).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(T)).and(DOCUMENT_ASPECTS.DOC_ID.eq("1.9.1"))
                .fetchOne(DOCUMENT_ASPECTS.COLLECTION_ID)).as("aspect row").isEqualTo(id);
            assertThat(ctx.select(DOCUMENT_HIGHLIGHTS.COLLECTION_ID).from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(T)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("1.9.1"))
                .fetchOne(DOCUMENT_HIGHLIGHTS.COLLECTION_ID)).as("highlights row").isEqualTo(id);
            return null;
        });
    }
}
