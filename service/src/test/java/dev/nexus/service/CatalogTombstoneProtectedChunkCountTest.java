// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

/**
 * nexus-zewg3 (engine-side redo, critique T2 nexus/critique-nexus-zewg3): {@link
 * CatalogRepository#tombstoneProtectedChunkCount} — the engine-computed count backing
 * {@code GET /v1/catalog/manifest/chashes}'s {@code tombstone_protected_count} field, which
 * {@code nx t3 gc}'s report line reads instead of re-deriving client-side.
 *
 * <p>Every fixture write and read goes through {@link CatalogRepository}'s own typed-jOOQ
 * public API ({@code upsertCollection}, {@code upsertDocument}, {@code writeManifestMany},
 * {@code deleteDocument}, {@code purgeTrash}) or a direct typed jOOQ query against the
 * generated {@code CATALOG_DOCUMENTS}/{@code CHUNKS} tables — no raw SQL strings, per the
 * repo's standing "jOOQ generated DSL only" directive (nexus-zrcj7).
 *
 * <p>Fixture (one tenant, two collections — {@code COLLECTION_A} is the one every count in
 * this suite is taken against, {@code COLLECTION_B} exists solely to host the cross-collection
 * scenario's live document):
 * <ul>
 *   <li>{@code DOC_LIVE_ONLY} / {@code CHASH_LIVE_ONLY} (collection A, never tombstoned) —
 *       baseline noise: a chash with a manifest reference that is never a tombstone
 *       candidate at all.</li>
 *   <li>{@code DOC_TOMB_ONLY} / {@code CHASH_TOMB_ONLY} (collection A) — its ONLY manifest
 *       reference, once tombstoned, is the tombstoned doc itself. This is the row the count
 *       exists to surface.</li>
 *   <li>{@code DOC_SHARED_LIVE} + {@code DOC_SHARED_TOMB} / {@code CHASH_SHARED} (both
 *       collection A) — the SAME chash referenced by a live doc and, later, a tombstoned
 *       doc IN THE SAME COLLECTION. Must never count: a live reference protects it.</li>
 *   <li>{@code DOC_CROSS_TOMB} (collection A) + {@code DOC_CROSS_LIVE} (collection B) /
 *       {@code CHASH_CROSS} — the cross-collection case the FIRST, rejected client-side cut
 *       of nexus-zewg3 could not resolve correctly (critique T2 nexus/critique-nexus-zewg3
 *       Significant 1): a chash physically stored in collection A, tombstone-referenced from
 *       a doc in A, but ALSO live-referenced from a doc in B. {@code nexus.purge_trash}'s own
 *       chunk-sweep predicate is collection-blind on the manifest side, so this chash is
 *       protected tenant-wide and must never count toward collection A's total either.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogTombstoneProtectedChunkCountTest {

    private static final String SVC_ROLE = "svc_tomb_protected_count";
    private static final String SVC_PASS = "svc_tomb_protected_count_pass";

    private static final String TENANT = "tombstone-protected-count";
    private static final String COLLECTION_A = "knowledge__tombstone-protected-count-a__minilm-l6-v2-384__v1";
    private static final String COLLECTION_B = "knowledge__tombstone-protected-count-b__minilm-l6-v2-384__v1";

    private static final String DOC_LIVE_ONLY   = "tpc-doc-live-only";
    private static final String DOC_TOMB_ONLY   = "tpc-doc-tomb-only";
    private static final String DOC_SHARED_LIVE = "tpc-doc-shared-live";
    private static final String DOC_SHARED_TOMB = "tpc-doc-shared-tomb";
    private static final String DOC_CROSS_TOMB  = "tpc-doc-cross-tomb";
    private static final String DOC_CROSS_LIVE  = "tpc-doc-cross-live";

    private static final String CHASH_LIVE_ONLY = Chash.ofText("tpc-chunk-live-only").toHex();
    private static final String CHASH_TOMB_ONLY = Chash.ofText("tpc-chunk-tomb-only").toHex();
    private static final String CHASH_SHARED    = Chash.ofText("tpc-chunk-shared").toHex();
    private static final String CHASH_CROSS     = Chash.ofText("tpc-chunk-cross").toHex();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // purgeTrash's EXECUTE grant is not part of bootstrapServiceRole's fixed
            // set (nexus-cbo4a batch 1a) -- same explicit grant CatalogPurgeTrashTest
            // needs, since Order(30) below calls catalogRepo.purgeTrash directly.
            // nexus-cbo4a batch 11 (04f759535): routed through
            // PgContainerHelper#grantExecuteOnFunction / nexus_test.grant_execute_on_function
            // -- jOOQ's typed GRANT DSL targets tables, not a function's argument-type
            // signature, so this is genuinely raw-SQL-free (zero TEST_TREE_RAW_SQL_CEILING
            // entry needed for this file).
            PgContainerHelper.grantExecuteOnFunction(su, "nexus.purge_trash(interval)", SVC_ROLE);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        catalogRepo = new CatalogRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        // RDR-204 nexus-ft04v.4/.5: hygiene-002-1's non-empty CHECK on content_type/
        // embedding_model means a registration naming only "name" now 23514s (the
        // repository's contentType/requestedModel default to blank when the map omits
        // them, per CatalogRepository#upsertCollection's own docstring: "new
        // registrations carry content_type explicitly"). Both names are RDR-103
        // conformant (knowledge__<owner>__minilm-l6-v2-384__v1) so content_type/
        // owner_id/embedding_model below are exactly what a real client derives from
        // the name before calling this route.
        catalogRepo.upsertCollection(TENANT, Map.of("name", COLLECTION_A,
            "content_type", "knowledge", "owner_id", "tombstone-protected-count-a",
            "embedding_model", "minilm-l6-v2-384"));
        catalogRepo.upsertCollection(TENANT, Map.of("name", COLLECTION_B,
            "content_type", "knowledge", "owner_id", "tombstone-protected-count-b",
            "embedding_model", "minilm-l6-v2-384"));

        registerDoc(DOC_LIVE_ONLY,   "Live Only",   COLLECTION_A);
        registerDoc(DOC_TOMB_ONLY,   "Tomb Only",   COLLECTION_A);
        registerDoc(DOC_SHARED_LIVE, "Shared Live", COLLECTION_A);
        registerDoc(DOC_SHARED_TOMB, "Shared Tomb", COLLECTION_A);
        registerDoc(DOC_CROSS_TOMB,  "Cross Tomb",  COLLECTION_A);
        registerDoc(DOC_CROSS_LIVE,  "Cross Live",  COLLECTION_B);

        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires a
        // chunks row for EVERY (tenant, collection, chash) a manifest row names —
        // CHASH_CROSS's manifest is referenced from BOTH collections (DOC_CROSS_TOMB
        // in A, DOC_CROSS_LIVE in B), so it needs a physical chunks row in each.
        vecRepo.upsertChunks(TENANT, COLLECTION_A,
            List.of(CHASH_LIVE_ONLY, CHASH_TOMB_ONLY, CHASH_SHARED, CHASH_CROSS),
            List.of("live only text", "tomb only text", "shared text", "cross text"),
            List.of(Map.of(), Map.of(), Map.of(), Map.of()));
        vecRepo.upsertChunks(TENANT, COLLECTION_B,
            List.of(CHASH_CROSS),
            List.of("cross text (collection B copy)"),
            List.of(Map.of()));

        writeManifestRow(DOC_LIVE_ONLY,   COLLECTION_A, CHASH_LIVE_ONLY);
        writeManifestRow(DOC_TOMB_ONLY,   COLLECTION_A, CHASH_TOMB_ONLY);
        writeManifestRow(DOC_SHARED_LIVE, COLLECTION_A, CHASH_SHARED);
        writeManifestRow(DOC_SHARED_TOMB, COLLECTION_A, CHASH_SHARED);
        writeManifestRow(DOC_CROSS_TOMB,  COLLECTION_A, CHASH_CROSS);
        writeManifestRow(DOC_CROSS_LIVE,  COLLECTION_B, CHASH_CROSS);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void registerDoc(String tumbler, String title, String collection) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", title, "physical_collection", collection));
    }

    private void writeManifestRow(String docId, String collection, String chashHex) {
        catalogRepo.writeManifestMany(TENANT,
            List.of(Map.of("doc_id", docId, "rows",
                List.of(Map.of("position", 0, "chash", chashHex)))),
            collection);
    }

    /** Typed jOOQ read (no raw SQL): does a physical {@code nexus.chunks} row for
     *  {@code chashHex} exist in {@code collection} with an embedding? */
    private long chunks384CountIn(String collection, String chashHex) {
        return tenantScope.withTenant(TENANT, ctx -> {
            Long count = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)
                       .and(CHUNKS.COLLECTION.eq(collection))
                       .and(ChashHex.hex(CHUNKS.CHASH).eq(chashHex))
                       .and(CHUNKS.EMBEDDING_384.isNotNull()))
                .fetchOne(0, Long.class);
            return count != null ? count : 0L;
        });
    }

    private boolean documentExists(String tumbler) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.fetchExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)
                       .and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))));
    }

    // ── no tombstones yet: baseline zero ────────────────────────────────────────

    @Test @Order(10)
    void noTombstonesYet_countIsZero() {
        assertThat(catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_A))
            .as("nothing is tombstoned yet -- every chash in COLLECTION_A is referenced "
                + "only by live documents")
            .isEqualTo(0L);
        assertThat(catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_B))
            .isEqualTo(0L);
    }

    // ── tombstone the fixture's three candidate docs ────────────────────────────

    @Test @Order(20)
    void afterTombstoning_onlyTheTombstoneOnlyChashCounts_sharedAndCrossCollectionChashesDoNot() {
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_TOMB_ONLY)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_SHARED_TOMB)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_CROSS_TOMB)).isEqualTo(1);

        assertThat(catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_A))
            .as("exactly CHASH_TOMB_ONLY: CHASH_SHARED is still live-referenced by "
                + "DOC_SHARED_LIVE in the SAME collection, and CHASH_CROSS is still "
                + "live-referenced by DOC_CROSS_LIVE in COLLECTION_B -- purge_trash's own "
                + "predicate is collection-blind on the manifest side, so a live reference "
                + "in ANY collection protects it (the nexus-zewg3 critique's Significant 1 "
                + "cross-collection gap the first, rejected client-side cut could not close)")
            .isEqualTo(1L);
        assertThat(catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_B))
            .as("COLLECTION_B's only chunk (CHASH_CROSS) is live-referenced by "
                + "DOC_CROSS_LIVE, which is not tombstoned")
            .isEqualTo(0L);
    }

    // ── parity: tenant-wide stranded sum must equal the per-collection sum ─────

    @Test @Order(25)
    void parityWithPurgeTrashPreview_tenantWideStrandedSumEqualsPerCollectionSum() {
        // olderThanDays=0 matches tombstoneProtectedChunkCount's own fixed semantics
        // (see that method's javadoc): both reduce hasProtectingManifest to "referenced
        // by a live document" only. The tenant here has exactly two collections, so the
        // tenant-wide stranded total purgeTrashPreview reports must equal the sum of the
        // SAME predicate run per collection -- this is the mechanical backstop that a
        // future edit to either the collection-scoped or the tenant-wide predicate
        // cannot silently drift apart (same "parity obligation" discipline
        // strandedChunkCount's own javadoc documents against nexus.purge_trash).
        Map<String, Object> preview = catalogRepo.purgeTrashPreview(TENANT, 0);
        long tenantWideStranded =
            ((Number) preview.get("chunks_384_stranded")).longValue()
            + ((Number) preview.get("chunks_768_stranded")).longValue()
            + ((Number) preview.get("chunks_1024_stranded")).longValue();

        long perCollectionSum =
            catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_A)
            + catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_B);

        assertThat(perCollectionSum)
            .as("summing the collection-scoped count across every collection in the "
                + "tenant must equal the tenant-wide stranded count purgeTrashPreview "
                + "reports at the same olderThanDays=0 threshold")
            .isEqualTo(tenantWideStranded)
            .isEqualTo(1L);
    }

    // ── execute: purge_trash physically reclaims the tombstone-only chash ──────

    @Test @Order(30)
    void afterPurgeTrash_tombstoneOnlyChashCountDropsToZero_sharedAndCrossSurviveUnaffected() {
        Map<String, Object> executed = catalogRepo.purgeTrash(TENANT, 0);
        assertThat(executed.get("dry_run")).isEqualTo(false);

        // The three tombstoned docs are all past a 0-day window -- all three are
        // physically reclaimed in this one call (fk-001 cascades their manifest rows).
        assertThat(documentExists(DOC_TOMB_ONLY)).isFalse();
        assertThat(documentExists(DOC_SHARED_TOMB)).isFalse();
        assertThat(documentExists(DOC_CROSS_TOMB)).isFalse();

        assertThat(catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_A))
            .as("CHASH_TOMB_ONLY's chunk row was physically swept by this purgeTrash call "
                + "-- nothing left in COLLECTION_A held alive only by a tombstone")
            .isEqualTo(0L);
        assertThat(catalogRepo.tombstoneProtectedChunkCount(TENANT, COLLECTION_B))
            .isEqualTo(0L);

        // CHASH_SHARED and CHASH_CROSS were never stranded (a live doc protected each
        // one throughout) -- their physical chunk rows must survive this purge call
        // untouched, exactly as purge_trash's own predicate promises.
        assertThat(chunks384CountIn(COLLECTION_A, CHASH_SHARED))
            .as("live-protected chunk must survive purge_trash")
            .isEqualTo(1L);
        assertThat(chunks384CountIn(COLLECTION_A, CHASH_CROSS))
            .as("cross-collection-live-protected chunk must survive purge_trash")
            .isEqualTo(1L);
        assertThat(chunks384CountIn(COLLECTION_A, CHASH_TOMB_ONLY))
            .as("the genuinely tombstone-only chunk is gone")
            .isEqualTo(0L);
    }
}
