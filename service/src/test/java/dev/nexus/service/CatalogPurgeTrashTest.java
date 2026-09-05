// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
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
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-3ck2g E3/E4 — {@code POST /v1/catalog/purge-trash}, exercised at the
 * {@link CatalogRepository} layer ({@code purgeTrashPreview}/{@code purgeTrash}, the
 * exact methods {@code CatalogHandler#handlePurgeTrash} calls into). The handler's
 * own body-validation branching (the {@code older_than_days}/{@code dry_run}
 * present-but-wrong-typed-value 400s added by nexus-8j1zx's fix round) is covered
 * separately by {@code dev.nexus.service.http.CatalogHandlerPurgeTrashTest}.
 *
 * <p>{@code nexus.purge_trash(interval)} (catalog-003-soft-delete.xml:200-296) existed
 * with ZERO Java callers before this bead — the stranded-chunk sweep and the physical
 * tombstone GC it implements were both dead code.
 *
 * <p>Fixture (one collection, {@code chunks_384}):
 * <ul>
 *   <li>{@code DOC_LIVE} — never tombstoned.</li>
 *   <li>{@code DOC_FRESH_TOMB} — tombstoned via {@link CatalogRepository#deleteDocument}
 *       moments ago (real production path).</li>
 *   <li>{@code DOC_AGED_TOMB} — tombstoned via {@code deleteDocument}, then its {@code
 *       deleted_at} is backdated 60 days (superuser {@code UPDATE}) to simulate an old
 *       tombstone without waiting real time.</li>
 *   <li>One manifest-less orphan chunk (RDR-145 note-chunk shape,
 *       {@code SoftDeleteTest}:814-867's pin) — never stranded, never swept.</li>
 * </ul>
 *
 * <p><strong>Contract this suite pins (nexus-5da44, catalog-026-purge-trash-chunk-age.xml)
 * </strong>: {@code nexus.purge_trash}'s Steps 1-3 (the {@code chunks_&lt;dim&gt;}
 * stranded-row sweep) now apply the SAME {@code older_than} grace-window scoping Step 4
 * (the {@code catalog_documents} physical delete) already applies. A FRESHLY tombstoned
 * document's chunk therefore SURVIVES the very next purge call exactly as its document
 * row does — chunk, document, and manifest rows travel together, either all surviving
 * (in-window) or all being reaped in the same purge call (past-window). {@code
 * fresh_tombstone_docSurvives_andItsChunkSurvivesToo} pins this explicitly. PRIOR TO
 * nexus-5da44 the chunk sweep was age-INDEPENDENT (swept on the very next call
 * regardless of the tombstone's own age) — that was the bug, not a documented contract;
 * do not resurrect it.
 *
 * <p><strong>FIXED, nexus-erwvd</strong>: {@link CatalogRepository#purgeTrashPreview}'s
 * {@code chunks_384_stranded} field is powered by {@link
 * CatalogRepository#strandedChunkCount}, a SEPARATE Java/jOOQ query mirroring {@code
 * nexus.purge_trash}'s SQL predicate — nexus-5da44 updated the SQL function's Steps 1-3
 * to the grace-window-aware predicate but left this Java mirror on the old
 * age-independent one (out of scope that session — {@code CatalogRepository.java} was
 * under concurrent edit by a sibling agent), so the dry-run preview OVER-promised
 * relative to what {@link CatalogRepository#purgeTrash} would actually sweep once an
 * in-window tombstone existed — the same defect CLASS nexus-ff85q was a P1 incident
 * for, via a different mechanism. nexus-erwvd brings {@code strandedChunkCount} onto
 * the identical grace-window predicate catalog-026-purge-trash-chunk-age.xml uses, so
 * preview and execute now agree by construction. {@code
 * dryRunPreview_countsStrandedChunksAndAgedTombstones_withoutMutating} pins the FIXED
 * (age-aware) count, and {@code
 * execute_purgesAgedTombstoneAndItsChunk_protectsFreshTombstonesChunk_liveAndOrphanSurvive}
 * carries a mechanical parity pin: preview's {@code chunks_384_stranded}, taken
 * immediately before {@code purgeTrash} runs on the same fixture, must equal the number
 * of {@code chunks_384} rows execute actually removes in that call.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogPurgeTrashTest {

    private static final String SVC_ROLE = "svc_purge_trash";
    private static final String SVC_PASS = "svc_purge_trash_pass";

    private static final String TENANT = "purge-trash";
    private static final String COLLECTION = "knowledge__purge-trash__minilm-l6-v2-384__v1";

    private static final String DOC_LIVE        = "purge-doc-live";
    private static final String DOC_FRESH_TOMB  = "purge-doc-fresh-tomb";
    private static final String DOC_AGED_TOMB   = "purge-doc-aged-tomb";

    private static final String CHASH_LIVE   = Chash.ofText("purge-chunk-live").toHex();
    private static final String CHASH_FRESH  = Chash.ofText("purge-chunk-fresh").toHex();
    private static final String CHASH_AGED   = Chash.ofText("purge-chunk-aged").toHex();
    private static final String CHASH_ORPHAN = Chash.ofText("purge-chunk-orphan").toHex();

    // ── boundary-exact grace-window cutoff fixture (Order 30) — deliberately its
    // OWN docs/chashes, never shared with DOC_LIVE/FRESH/AGED above, which Order
    // 20 already consumed/mutated.
    private static final String DOC_BOUNDARY_AT      = "purge-doc-boundary-at-cutoff";
    private static final String DOC_BOUNDARY_INSIDE  = "purge-doc-boundary-just-inside";
    private static final String DOC_BOUNDARY_OUTSIDE = "purge-doc-boundary-just-outside";

    private static final String CHASH_BOUNDARY_AT      = Chash.ofText("purge-chunk-boundary-at").toHex();
    private static final String CHASH_BOUNDARY_INSIDE  = Chash.ofText("purge-chunk-boundary-inside").toHex();
    private static final String CHASH_BOUNDARY_OUTSIDE = Chash.ofText("purge-chunk-boundary-outside").toHex();

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
            // purge_trash(interval) EXECUTE is not part of bootstrapServiceRole's
            // fixed grant set (nexus-cbo4a batch 1a) -- kept as an explicit grant.
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.purge_trash(interval) TO " + SVC_ROLE);
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

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', '"
                + COLLECTION + "') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', '" + DOC_LIVE + "', 'Live', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_FRESH_TOMB + "', 'Fresh Tomb', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_AGED_TOMB + "', 'Aged Tomb', '" + COLLECTION + "')");
            // nexus-tk070.p1 (RDR-194 § D2): one catalog_links row per tombstone
            // class, DOC_LIVE as the FROM side, so purge_trash's Step 4 physical
            // delete of DOC_AGED_TOMB exercises fk_catalog_links_to_document's
            // cascade (execute_... below), while DOC_FRESH_TOMB's link — still
            // inside the grace window, never physically deleted by this fixture —
            // stays a live pin that the link never disappears out from under.
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by, created_at) VALUES "
                + "('" + TENANT + "', '" + DOC_LIVE + "', '" + DOC_AGED_TOMB + "', 'relates', 'test', NOW()), "
                + "('" + TENANT + "', '" + DOC_LIVE + "', '" + DOC_FRESH_TOMB + "', 'relates', 'test', NOW())");
        }

        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for every catalog_document_chunks insert, so
        // the chunk vectors must land BEFORE the manifest rows below (previously
        // order-independent; upsertChunks ran after insertManifestRow here).
        vecRepo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_LIVE, CHASH_FRESH, CHASH_AGED, CHASH_ORPHAN),
            List.of("live text", "fresh tomb text", "aged tomb text", "orphan text"),
            List.of(Map.of(), Map.of(), Map.of(), Map.of()));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertManifestRow(su, DOC_LIVE, CHASH_LIVE);
            insertManifestRow(su, DOC_FRESH_TOMB, CHASH_FRESH);
            insertManifestRow(su, DOC_AGED_TOMB, CHASH_AGED);
            // CHASH_ORPHAN: no manifest row (RDR-145 manifest-less note chunk).
        }

        // Tombstone FRESH and AGED via the real production path.
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_FRESH_TOMB)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_AGED_TOMB)).isEqualTo(1);

        // Backdate AGED's tombstone 60 days so it clears a 30-day older_than threshold;
        // FRESH's tombstone stays at "just now".
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() - interval '60 days' "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + DOC_AGED_TOMB + "'");
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void insertManifestRow(Connection su, String docId, String chashHex) throws Exception {
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml) — every doc in this fixture
        // is registered under COLLECTION, so stamp the manifest row the same.
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "VALUES (?, ?, 0, decode(?, 'hex'), ?)")) {
            ps.setString(1, TENANT);
            ps.setString(2, docId);
            ps.setString(3, chashHex);
            ps.setString(4, COLLECTION);
            ps.execute();
        }
    }

    /** RDR-191 (nexus-o8dil.48): chunks_384 unified into nexus.chunks --
     *  {@code embedding_384 IS NOT NULL} replaces table membership as the dim
     *  filter (this fixture's tenant is otherwise 384-only, but the explicit
     *  guard keeps this helper correct if that ever changes). */
    private long chunks384Count(String chashHex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = ? AND chash = decode(?, 'hex') "
                + "AND embedding_384 IS NOT NULL");
            ps.setString(1, TENANT);
            ps.setString(2, chashHex);
            var rs = ps.executeQuery();
            rs.next();
            return rs.getLong(1);
        }
    }

    /** nexus-erwvd parity pin support: total {@code nexus.chunks} rows (RDR-191
     *  unified; formerly {@code chunks_384}) for TENANT, unfiltered by chash --
     *  the mechanical before/after diff a preview count is checked against. */
    private long totalChunks384Rows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = ? AND embedding_384 IS NOT NULL");
            ps.setString(1, TENANT);
            var rs = ps.executeQuery();
            rs.next();
            return rs.getLong(1);
        }
    }

    private long countManifestRows(String chashHex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id = ? AND chash = decode(?, 'hex')");
            ps.setString(1, TENANT);
            ps.setString(2, chashHex);
            var rs = ps.executeQuery();
            rs.next();
            return rs.getLong(1);
        }
    }

    private boolean documentExists(String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT 1 FROM nexus.catalog_documents WHERE tenant_id = ? AND tumbler = ?");
            ps.setString(1, TENANT);
            ps.setString(2, tumbler);
            return ps.executeQuery().next();
        }
    }

    /** nexus-tk070.p1 (RDR-194 § D2): does a catalog_links row naming {@code toTumbler}
     *  as its to_tumbler still exist for TENANT? Used to pin that purge_trash Step 4's
     *  hard delete of catalog_documents cascades catalog_links via
     *  fk_catalog_links_to_document, with no explicit Java or plpgsql step required. */
    private boolean linkToExists(String toTumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT 1 FROM nexus.catalog_links WHERE tenant_id = ? AND to_tumbler = ?");
            ps.setString(1, TENANT);
            ps.setString(2, toTumbler);
            return ps.executeQuery().next();
        }
    }

    /** Server-captured {@code NOW()} (nexus-ff85q precedent: pin against a SQL-side
     *  instant, never a Java-clock read) — the reference point the boundary test
     *  backdates {@code deleted_at} relative to. */
    private java.time.OffsetDateTime capturedNow() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery("SELECT NOW()");
            rs.next();
            return rs.getObject(1, java.time.OffsetDateTime.class);
        }
    }

    // ── dry_run preview: no mutation ────────────────────────────────────────────

    @Test @Order(10)
    void dryRunPreview_countsStrandedChunksAndAgedTombstones_withoutMutating() throws Exception {
        Map<String, Object> result = catalogRepo.purgeTrashPreview(TENANT, 30);

        assertThat(result.get("dry_run")).isEqualTo(true);
        // nexus-erwvd: only AGED's chunk is stranded — FRESH is still inside the 30-day
        // grace window, so strandedChunkCount's grace-window-aware predicate (mirroring
        // catalog-026's chunk sweep) protects it exactly as purgeTrash's execute path does.
        assertThat(((Number) result.get("chunks_384_stranded")).longValue())
            .as("stranded chunk count is grace-window-aware: only the PAST-window tombstone's "
                + "chunk counts, matching what execute will actually sweep")
            .isEqualTo(1L);
        assertThat(((Number) result.get("chunks_768_stranded")).longValue()).isEqualTo(0L);
        assertThat(((Number) result.get("chunks_1024_stranded")).longValue()).isEqualTo(0L);
        // Only AGED clears the 30-day threshold.
        assertThat(((Number) result.get("documents_purged")).longValue())
            .as("preview 'documents_purged' = would-purge count under a 30-day threshold")
            .isEqualTo(1L);

        // No mutation: everything from setup must still be exactly as it was.
        assertThat(documentExists(DOC_LIVE)).isTrue();
        assertThat(documentExists(DOC_FRESH_TOMB)).isTrue();
        assertThat(documentExists(DOC_AGED_TOMB)).isTrue();
        assertThat(chunks384Count(CHASH_LIVE)).isEqualTo(1L);
        assertThat(chunks384Count(CHASH_FRESH)).isEqualTo(1L);
        assertThat(chunks384Count(CHASH_AGED)).isEqualTo(1L);
        assertThat(chunks384Count(CHASH_ORPHAN))
            .as("manifest-less orphan chunk is never counted as stranded, dry-run or not")
            .isEqualTo(1L);
    }

    @Test @Order(15)
    void dryRunPreview_withSmallerOlderThan_stillDoesNotMutate() throws Exception {
        // older_than_days=1 clears BOTH tombstones (fresh is still < 1 day old in wall-clock
        // terms, but the point here is only that dry_run never mutates regardless of the
        // threshold chosen).
        Map<String, Object> result = catalogRepo.purgeTrashPreview(TENANT, 1);
        assertThat(result.get("dry_run")).isEqualTo(true);
        assertThat(documentExists(DOC_LIVE)).isTrue();
        assertThat(documentExists(DOC_FRESH_TOMB)).isTrue();
        assertThat(documentExists(DOC_AGED_TOMB)).isTrue();
    }

    // ── execute: actually purges ────────────────────────────────────────────────

    @Test @Order(20)
    void execute_purgesAgedTombstoneAndItsChunk_protectsFreshTombstonesChunk_liveAndOrphanSurvive() throws Exception {
        // nexus-erwvd parity pin setup: preview immediately before execute, on the same
        // (as-yet-unmutated) fixture, so the two are comparable against the same state.
        long totalBefore = totalChunks384Rows();
        Map<String, Object> preview = catalogRepo.purgeTrashPreview(TENANT, 30);
        long previewedStranded384 = ((Number) preview.get("chunks_384_stranded")).longValue();

        Map<String, Object> result = catalogRepo.purgeTrash(TENANT, 30);

        assertThat(result.get("dry_run")).isEqualTo(false);
        assertThat(((Number) result.get("documents_purged")).longValue())
            .as("nexus.purge_trash's own return value: exactly the aged doc")
            .isEqualTo(1L);

        // AGED: fully gone — catalog_documents row hard-deleted, manifest row cascaded,
        // chunk swept. All three go together (nexus-5da44).
        assertThat(documentExists(DOC_AGED_TOMB)).as("aged tombstone physically purged").isFalse();
        assertThat(chunks384Count(CHASH_AGED)).as("aged doc's chunk swept").isEqualTo(0L);
        // nexus-tk070.p1 (RDR-194 § D2): purge_trash Step 4's hard delete cascades
        // catalog_links too now, via fk_catalog_links_to_document — a FIFTH cascade
        // child at zero code cost, exactly as D2 designed it.
        assertThat(linkToExists(DOC_AGED_TOMB))
            .as("catalog_links row pointing at the purged doc cascaded off purge_trash's own hard delete")
            .isFalse();

        // FRESH: still inside the grace window — nexus-5da44 protects its chunk exactly
        // as its document row was always protected. See Order(21) for the explicit pin.
        assertThat(documentExists(DOC_FRESH_TOMB)).isTrue();
        assertThat(chunks384Count(CHASH_FRESH))
            .as("fresh (in-window) tombstone's chunk must survive alongside its document row")
            .isEqualTo(1L);
        assertThat(linkToExists(DOC_FRESH_TOMB))
            .as("fresh (in-window) tombstone's document row is not yet purged, so its link survives too")
            .isTrue();

        // LIVE and its chunk: untouched.
        assertThat(documentExists(DOC_LIVE)).isTrue();
        assertThat(chunks384Count(CHASH_LIVE)).isEqualTo(1L);

        // Manifest-less orphan chunk: RDR-145 pin — never swept regardless.
        assertThat(chunks384Count(CHASH_ORPHAN)).isEqualTo(1L);

        // nexus-erwvd parity pin: purgeTrashPreview's chunks_384_stranded count (taken
        // above, immediately before this same purgeTrash call) must equal the number of
        // chunks_384 rows this call actually removed — preview and execute must agree by
        // construction, both now mirroring catalog-026's grace-window predicate. This is
        // the durable, mechanical backstop for the preview-lies class (cf. nexus-5uoxu):
        // a future edit to either predicate that lets them drift again fails HERE, not
        // just in a hand-maintained assertion pinned to the current fixture shape.
        long totalAfter = totalChunks384Rows();
        assertThat(previewedStranded384)
            .as("preview's chunks_384_stranded must equal chunks_384 rows purgeTrash actually "
                + "swept in the same call")
            .isEqualTo(totalBefore - totalAfter);
    }

    @Test @Order(21)
    void fresh_tombstone_docSurvives_andItsChunkSurvivesToo() throws Exception {
        // nexus-5da44: pinning the FIXED contract from the class javadoc. FRESH's document
        // row survives (not yet aged past 30 days) and, as of this fix, so does its chunk —
        // the chunk-sweep predicate (purge_trash Steps 1-3) now applies the SAME older_than
        // grace-window scoping as the document DELETE (Step 4). Chunk, document, and
        // manifest row travel together for an in-window tombstone.
        assertThat(documentExists(DOC_FRESH_TOMB))
            .as("fresh tombstone's document row must survive — not yet aged past older_than")
            .isTrue();
        assertThat(chunks384Count(CHASH_FRESH))
            .as("fresh tombstone's chunk must ALSO survive — it is still inside the grace "
                + "window, so the chunk sweep (nexus-5da44) protects it the same as the "
                + "document delete always protected the document row. A tombstone whose "
                + "content vanished before its own grace window elapsed was exactly the "
                + "nexus-5da44 defect; this pin exists so it cannot silently regress.")
            .isEqualTo(1L);
        assertThat(countManifestRows(CHASH_FRESH))
            .as("fresh tombstone's manifest row must survive too — all three (chunk, "
                + "document, manifest) travel together for an in-window tombstone")
            .isEqualTo(1L);
    }

    // ── boundary-exact grace-window cutoff (nexus-5da44/nexus-erwvd, reviewer
    // "Important" finding) ──────────────────────────────────────────────────────

    @Test @Order(30)
    void boundaryExactCutoff_atAndJustOutsideReapedTogether_justInsideProtectedTogether_previewAgreesWithExecute()
            throws Exception {
        final int olderThanDays = 30;

        // Fresh, self-contained fixture: three docs, own chashes.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', '" + DOC_BOUNDARY_AT + "', 'Boundary At', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_BOUNDARY_INSIDE + "', 'Boundary Inside', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_BOUNDARY_OUTSIDE + "', 'Boundary Outside', '" + COLLECTION + "')");
        }
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
        // chunk vectors to land BEFORE the manifest rows below.
        vecRepo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_BOUNDARY_AT, CHASH_BOUNDARY_INSIDE, CHASH_BOUNDARY_OUTSIDE),
            List.of("boundary at text", "boundary inside text", "boundary outside text"),
            List.of(Map.of(), Map.of(), Map.of()));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertManifestRow(su, DOC_BOUNDARY_AT, CHASH_BOUNDARY_AT);
            insertManifestRow(su, DOC_BOUNDARY_INSIDE, CHASH_BOUNDARY_INSIDE);
            insertManifestRow(su, DOC_BOUNDARY_OUTSIDE, CHASH_BOUNDARY_OUTSIDE);
        }

        assertThat(catalogRepo.deleteDocument(TENANT, DOC_BOUNDARY_AT)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_BOUNDARY_INSIDE)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_BOUNDARY_OUTSIDE)).isEqualTo(1);

        // nexus-ff85q precedent: pin the boundary against a SERVER-CAPTURED reference
        // instant (capturedNow()), not a Java-clock computation. olderThanInterval puts
        // the whole magnitude in the interval's DAY field (no calendar-month
        // normalisation, catalog-025/26's own discipline), so the ONLY remaining
        // variable is wall-clock drift between capturing tRef here and purge_trash's
        // own NOW() a few statements below. Postgres transaction-start time is
        // monotonic non-decreasing across separate transactions on a hermetic
        // Testcontainers clock, so the LIVE cutoff (tPurge - olderThanDays) can only
        // be >= (tRef - olderThanDays), never earlier — pushing the boundary FORWARD
        // in time, never backward. That makes both ends deterministic with no race:
        //   AT      = tRef - olderThanDays            -> ALWAYS <= live cutoff -> reaped
        //   OUTSIDE = tRef - olderThanDays - 30s        -> ALWAYS <= live cutoff -> reaped
        //   INSIDE  = tRef - olderThanDays + 30s        -> stays > live cutoff as long as
        //             the wall-clock drift between tRef and the purge_trash call below
        //             is under 30s, which is not a real risk for a few in-process SQL
        //             calls against a local Testcontainers instance -> protected
        java.time.OffsetDateTime tRef = capturedNow();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ps = su.prepareStatement(
                "UPDATE nexus.catalog_documents SET deleted_at = ? WHERE tenant_id = ? AND tumbler = ?");
            ps.setObject(1, tRef.minusDays(olderThanDays));
            ps.setString(2, TENANT);
            ps.setString(3, DOC_BOUNDARY_AT);
            ps.executeUpdate();
            ps.setObject(1, tRef.minusDays(olderThanDays).plusSeconds(30));
            ps.setString(3, DOC_BOUNDARY_INSIDE);
            ps.executeUpdate();
            ps.setObject(1, tRef.minusDays(olderThanDays).minusSeconds(30));
            ps.setString(3, DOC_BOUNDARY_OUTSIDE);
            ps.executeUpdate();
        }

        // nexus-erwvd parity setup: preview immediately before execute, on this exact
        // (as-yet-unmutated-by-this-test) fixture state, plus a total-row before-count
        // for the mechanical diff — same idiom as Order(20)'s parity pin.
        long totalBefore = totalChunks384Rows();
        Map<String, Object> preview = catalogRepo.purgeTrashPreview(TENANT, olderThanDays);
        long previewedStranded384 = ((Number) preview.get("chunks_384_stranded")).longValue();
        long previewedDocsEligible = ((Number) preview.get("documents_purged")).longValue();

        Map<String, Object> executed = catalogRepo.purgeTrash(TENANT, olderThanDays);
        assertThat(executed.get("dry_run")).isEqualTo(false);

        // DOC surface: AT and OUTSIDE purged, INSIDE survives.
        assertThat(documentExists(DOC_BOUNDARY_AT))
            .as("a tombstone EXACTLY at the reference cutoff must be treated as "
                + "past-or-at the live cutoff (deleted_at <= NOW() - older_than) and purged")
            .isFalse();
        assertThat(documentExists(DOC_BOUNDARY_OUTSIDE))
            .as("a tombstone 30s older than the reference cutoff must also be purged")
            .isFalse();
        assertThat(documentExists(DOC_BOUNDARY_INSIDE))
            .as("a tombstone 30s inside the grace window must survive this purge call")
            .isTrue();

        // CHUNK surface: AT and OUTSIDE swept, INSIDE survives — same grace-window
        // predicate protects the chunk that protects the document (nexus-5da44).
        assertThat(chunks384Count(CHASH_BOUNDARY_AT))
            .as("the at-cutoff tombstone's chunk must be swept in the same call")
            .isEqualTo(0L);
        assertThat(chunks384Count(CHASH_BOUNDARY_OUTSIDE))
            .as("the just-outside tombstone's chunk must be swept in the same call")
            .isEqualTo(0L);
        assertThat(chunks384Count(CHASH_BOUNDARY_INSIDE))
            .as("the just-inside tombstone's chunk must survive, protected by the "
                + "identical predicate that protects its document row")
            .isEqualTo(1L);

        // MANIFEST surface: purgeTrashPreview reports no manifest field at all (only
        // documents_purged + chunks_<dim>_stranded), so there is no preview number to
        // cross-check here — asserted directly against post-execute state instead.
        // AT/OUTSIDE: gone via fk-001's CASCADE off the document delete. INSIDE:
        // survives because the document delete that would cascade it never fires.
        assertThat(countManifestRows(CHASH_BOUNDARY_AT)).isEqualTo(0L);
        assertThat(countManifestRows(CHASH_BOUNDARY_OUTSIDE)).isEqualTo(0L);
        assertThat(countManifestRows(CHASH_BOUNDARY_INSIDE))
            .as("the just-inside tombstone's manifest row must survive alongside its "
                + "document and chunk — the cascade that would remove it never fires")
            .isEqualTo(1L);

        // nexus-erwvd preview/execute AGREEMENT, both remaining surfaces preview DOES
        // report:
        //   documents: preview's "documents_purged" (eligible count) at this fixture
        //   state must equal exactly 2 (AT + OUTSIDE; INSIDE is not eligible; LIVE/
        //   FRESH/AGED from the shared Order(10-21) fixture are already accounted for
        //   elsewhere and unaffected by this test's own docs) — and execute's own
        //   returned count must match it exactly, not just the boundary subset.
        assertThat(previewedDocsEligible)
            .as("preview must count exactly the 2 genuinely past-cutoff boundary docs "
                + "(AT, OUTSIDE) as eligible — not 3 (INSIDE incorrectly included) and "
                + "not 1 (AT or OUTSIDE incorrectly excluded)")
            .isEqualTo(2L);
        assertThat(((Number) executed.get("documents_purged")).longValue())
            .as("execute must purge EXACTLY the population preview reported eligible")
            .isEqualTo(previewedDocsEligible);

        //   chunks: preview's chunks_384_stranded (captured before this call mutated
        //   anything) must equal the actual chunks_384 row-count delta this exact
        //   purgeTrash call produced — preview and execute agree by construction, not
        //   by a hand-restated number.
        long totalAfter = totalChunks384Rows();
        assertThat(previewedStranded384)
            .as("preview's chunks_384_stranded must equal the chunks_384 rows this "
                + "purgeTrash call actually swept, including but not limited to the "
                + "two boundary chunks (AT, OUTSIDE) verified individually above")
            .isEqualTo(totalBefore - totalAfter);
    }
}
