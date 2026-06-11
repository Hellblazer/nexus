// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-156 bead nexus-70r3c.8 — TDD-RED suite for P2 manifest functions (Decision 3).
 *
 * <p><strong>Scope (Decision 3):</strong>
 * Promote the RDR-155 generated-SQL artifacts to first-class stored functions:
 * <ul>
 *   <li>{@code nexus.manifest_orphans(dim int)} — manifest rows with no corresponding chunk
 *       row in {@code chunks_<dim>}; tombstone-aware (deleted_at IS NULL on parent doc).</li>
 *   <li>{@code nexus.manifest_backfill()} — idempotent collection-stamping backfill;
 *       stamps {@code catalog_document_chunks.collection} from the owning doc's
 *       {@code physical_collection} where NULL.</li>
 *   <li>{@code nexus.document_text(doc_id text)} — ordered manifest⋈chunk_text reconstruction,
 *       tombstone-aware (tombstoned doc returns empty set).</li>
 * </ul>
 *
 * <p><strong>Call protocol note (documented in function comments):</strong>
 * Run {@code manifest_backfill()} BEFORE {@code manifest_orphans(dim)} —
 * rows with {@code collection IS NULL} are pre-backfill state, not orphans.
 *
 * <p><strong>Non-vacuity guarantee:</strong>
 * Each test that asserts N &gt; 0 orphans ALSO asserts the count drops to 0 after the
 * missing chunk is inserted. Both directions are tested so empty-table vacuity cannot
 * pass silently (Gap 4 evidence from the RDR-155 first production cutover run).
 *
 * <p><strong>Expected RED/GREEN before catalog-004 lands:</strong>
 * <ul>
 *   <li>GROUP 1 (manifest_orphans basic): RED — function absent</li>
 *   <li>GROUP 2 (manifest_orphans non-vacuity): RED — function absent</li>
 *   <li>GROUP 3 (manifest_orphans tombstone exclusion): RED — function absent</li>
 *   <li>GROUP 4 (manifest_backfill basic): RED — function absent</li>
 *   <li>GROUP 5 (manifest_backfill idempotent): RED — function absent</li>
 *   <li>GROUP 6 (manifest_backfill tombstone-aware): RED — function absent</li>
 *   <li>GROUP 7 (document_text ordering): RED — function absent</li>
 *   <li>GROUP 8 (document_text tombstone-aware): RED — function absent</li>
 *   <li>GROUP 9 (document_text manifest gap contract): RED — function absent</li>
 *   <li>GROUP 10 (SECURITY INVOKER / grants): RED — function absent</li>
 *   <li>GROUP 11 (RLS isolation — svc role sees only its tenant): RED — function absent</li>
 *   <li>All CONTROL paths (fixture inserts, schema presence checks): GREEN always</li>
 * </ul>
 *
 * <p>Mirror conventions from SoftDeleteTest: PgContainerHelper, Liquibase master, PER_CLASS,
 * {@link Order}, superuser fixtures, svc-role + GUC for RLS tests, 32-char chashes,
 * registered collections per fk-002.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ManifestFunctionsTest {

    // ── Tenant IDs ────────────────────────────────────────────────────────────
    private static final String TENANT_A = "mf-tenant-a";
    private static final String TENANT_B = "mf-tenant-b";

    // ── Svc role (NOSUPERUSER, NOBYPASSRLS — subject to FORCE RLS) ───────────
    private static final String SVC_ROLE = "svc_mf_test";
    private static final String SVC_PASS = "svc_mf_test_pass";

    // ── Function names (pinned contract for catalog-004 to honor) ─────────────
    /**
     * Returns manifest rows whose chash has no chunk row in {@code chunks_<dim>} for
     * that tenant+collection. Tombstone-aware: orphan manifest rows whose parent doc
     * has {@code deleted_at IS NOT NULL} are excluded.
     * {@code nexus.manifest_orphans(dim int) RETURNS TABLE(...)}.
     * SECURITY INVOKER; no GUC scoping — admin/superuser cross-tenant function.
     */
    private static final String FN_ORPHANS   = "nexus.manifest_orphans";

    /**
     * Stamps {@code catalog_document_chunks.collection} from the owning doc's
     * {@code physical_collection} where NULL. Idempotent.
     * {@code nexus.manifest_backfill() RETURNS bigint}.
     * SECURITY INVOKER; no GUC scoping — admin/superuser maintenance function.
     */
    private static final String FN_BACKFILL  = "nexus.manifest_backfill";

    /**
     * Ordered manifest⋈chunk_text reconstruction for a document.
     * {@code nexus.document_text(doc_id text) RETURNS TABLE(position int, chunk_text text)}.
     * SECURITY INVOKER; tenant-scoped via nexus.tenant GUC.
     * Tombstone-aware: tombstoned doc returns empty set (never stale text).
     */
    private static final String FN_DOC_TEXT  = "nexus.document_text";

    // ── Test collections (must be registered in catalog_collections per fk-002) ─
    private static final String COLLECTION_384  = "knowledge__mf-owner-a__minilm__v1";
    private static final String COLLECTION_1024 = "knowledge__mf-owner-a__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    // ══════════════════════════════════════════════════════════════════════════
    // LIFECYCLE
    // ══════════════════════════════════════════════════════════════════════════

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Phase 1: create svc role (autoCommit=true; CREATE ROLE cannot run in a transaction)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }

        // Phase 2: apply full master changelog
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to tables
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of(
                    "catalog_collections",
                    "catalog_documents",
                    "catalog_links",
                    "catalog_document_chunks",
                    "document_aspects",
                    "document_highlights",
                    "aspect_extraction_queue",
                    "chunks_384",
                    "chunks_768",
                    "chunks_1024")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.document_aspects_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.document_highlights_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.aspect_extraction_queue_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // HikariCP svc role pool (NOSUPERUSER NOBYPASSRLS — subject to RLS)
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — manifest_orphans: basic operation (dim=384)
    //
    // EXPECTED RED: nexus.manifest_orphans function absent until catalog-004.
    //
    // Fixture: 1 live doc, 2 manifest rows:
    //   - chash_present: has a chunks_384 row    → NOT an orphan
    //   - chash_missing:  NO chunks_384 row      → IS an orphan
    //
    // Assert: manifest_orphans(384) returns exactly 1 row (chash_missing).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void manifestOrphans_basic_returnsOrphanRows_dim384() throws Exception {
        // RED until catalog-004 adds nexus.manifest_orphans(int).
        String docId        = "mf-orphans-doc-1";
        String chashPresent = validChash("mf-present-chunk1");
        String chashMissing = validChash("mf-missing-chunk1");

        // Fixture: doc + 2 manifest rows; only one has a chunk row
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 0, chashPresent, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 1, chashMissing, COLLECTION_384);
            // Only insert the chunk for chashPresent
            insertChunk384(su, TENANT_A, COLLECTION_384, chashPresent, "chunk present text");
            // chashMissing has NO chunk row — it IS an orphan
        }

        // CONTROL: verify fixture state
        try (Connection su = pg.createConnection("")) {
            assertThat(countManifest(su, TENANT_A, docId))
                .as("CONTROL: 2 manifest rows must exist")
                .isEqualTo(2);
            assertThat(countChunks384(su, TENANT_A, COLLECTION_384))
                .as("CONTROL: 1 chunk row must exist (chashPresent only)")
                .isEqualTo(1);
        }

        // Call manifest_orphans(384) — RED trigger: function absent
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_ORPHANS + "(384)");
            rs.next();
            assertThat(rs.getLong(1))
                .as("manifest_orphans(384) must return exactly 1 orphan row (chash_missing has no chunk)")
                .isEqualTo(1L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — manifest_orphans: NON-VACUITY (the Gap 4 evidence property)
    //
    // EXPECTED RED: nexus.manifest_orphans function absent until catalog-004.
    //
    // Two-direction test: first assert N orphans returned (non-vacuous),
    // then insert the missing chunk and assert 0 orphans (repair validates).
    // An empty-table / vacuous implementation cannot pass both directions.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void manifestOrphans_nonVacuity_bothDirections() throws Exception {
        // RED until catalog-004 adds nexus.manifest_orphans(int).
        String docId       = "mf-nonvac-doc-1";
        String chashOrphan = validChash("mf-nonvac-orphan1");

        // Fixture: doc + manifest row for chashOrphan; NO chunk row yet
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 0, chashOrphan, COLLECTION_384);
            // Intentionally NO chunk row — chashOrphan IS an orphan
        }

        // Direction 1: manifest_orphans must return >= 1 (non-vacuous — there IS an orphan)
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_ORPHANS + "(384) " +
                "WHERE doc_id = '" + docId + "'");
            rs.next();
            assertThat(rs.getLong(1))
                .as("manifest_orphans(384) must return >= 1 for the seeded orphan " +
                    "(non-vacuity: function must not vacuously return 0 against populated data)")
                .isGreaterThanOrEqualTo(1L);
        }

        // Repair: insert the missing chunk
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertChunk384(su, TENANT_A, COLLECTION_384, chashOrphan, "repaired chunk text");
        }

        // Direction 2: after repair, manifest_orphans must return 0 for this doc
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_ORPHANS + "(384) " +
                "WHERE doc_id = '" + docId + "'");
            rs.next();
            assertThat(rs.getLong(1))
                .as("manifest_orphans(384) must return 0 after the missing chunk is inserted " +
                    "(repair validates: the function detects repair, not just presence of any rows)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — manifest_orphans: tombstone exclusion
    //
    // EXPECTED RED: nexus.manifest_orphans function absent until catalog-004.
    //
    // A manifest row whose parent doc is TOMBSTONED (deleted_at IS NOT NULL)
    // must be EXCLUDED from manifest_orphans output.
    // Rationale: orphan sweep is a forward-migration health check; tombstoned
    // documents are being retired, not migrated — they should not appear.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void manifestOrphans_excludesTombstonedDocRows() throws Exception {
        // RED until catalog-004 adds nexus.manifest_orphans(int).
        String docTombstoned = "mf-tomb-orphan-doc-1";
        String docLive       = "mf-live-orphan-doc-1";
        String chashTomb     = validChash("mf-tomb-orphan-chsh");
        String chashLive     = validChash("mf-live-orphan-chsh");

        // Fixture:
        //   - Tombstoned doc with an orphan manifest row (no chunk)
        //   - Live doc with an orphan manifest row (no chunk)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docTombstoned, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docTombstoned, 0, chashTomb, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docLive, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docLive, 0, chashLive, COLLECTION_384);
            // Neither chash has a chunk row — both are orphans if the function is tombstone-naive
        }

        // Tombstone the first doc via document_trash
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docTombstoned + "'");
        }

        // manifest_orphans must exclude the tombstoned doc's manifest row
        try (Connection su = pg.createConnection("")) {
            // tombstoned doc must NOT appear
            ResultSet tombRs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_ORPHANS + "(384) " +
                "WHERE doc_id = '" + docTombstoned + "'");
            tombRs.next();
            assertThat(tombRs.getLong(1))
                .as("manifest_orphans(384) must EXCLUDE orphan rows for tombstoned docs " +
                    "(tombstone-aware: deleted_at IS NULL join on parent doc)")
                .isEqualTo(0L);

            // live doc MUST still appear (confirms the function is not returning 0 vacuously)
            ResultSet liveRs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_ORPHANS + "(384) " +
                "WHERE doc_id = '" + docLive + "'");
            liveRs.next();
            assertThat(liveRs.getLong(1))
                .as("manifest_orphans(384) must INCLUDE orphan rows for live docs " +
                    "(non-vacuity: live orphan must be returned even while tombstoned is excluded)")
                .isEqualTo(1L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — manifest_backfill: basic collection stamping
    //
    // EXPECTED RED: nexus.manifest_backfill function absent until catalog-004.
    //
    // Fixture: doc with physical_collection set; manifest row with collection = NULL.
    // After manifest_backfill(): manifest row's collection is stamped with physical_collection.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void manifestBackfill_stampsCollectionFromDoc() throws Exception {
        // RED until catalog-004 adds nexus.manifest_backfill().
        String docId = "mf-backfill-doc-1";
        String chash = validChash("mf-backfill-chsh01");

        // Fixture: doc with physical_collection; manifest row with collection = NULL
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            // Insert manifest row with collection = NULL (pre-backfill state)
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks " +
                "  (tenant_id, doc_id, position, chash) " +
                "VALUES ('" + TENANT_A + "', '" + docId + "', 0, '" + chash + "') " +
                "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
        }

        // CONTROL: confirm collection IS NULL before backfill
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT collection FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id = '" + TENANT_A + "' AND doc_id = '" + docId + "' AND position = 0");
            assertThat(rs.next()).as("CONTROL: manifest row must exist").isTrue();
            assertThat(rs.getString("collection"))
                .as("CONTROL: collection must be NULL before backfill")
                .isNull();
        }

        // Call manifest_backfill() — RED trigger: function absent
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute("SELECT " + FN_BACKFILL + "()");
        }

        // Post-backfill: manifest row's collection must be stamped
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT collection FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id = '" + TENANT_A + "' AND doc_id = '" + docId + "' AND position = 0");
            assertThat(rs.next()).as("manifest row must still exist after backfill").isTrue();
            assertThat(rs.getString("collection"))
                .as("manifest_backfill() must stamp collection = doc's physical_collection")
                .isEqualTo(COLLECTION_384);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — manifest_backfill: idempotent (second call no-op)
    //
    // EXPECTED RED: nexus.manifest_backfill function absent until catalog-004.
    //
    // Second call on already-backfilled rows must return 0 rows affected.
    // The function should only UPDATE rows WHERE collection IS NULL.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void manifestBackfill_idempotent_secondCallReturnsZero() throws Exception {
        // RED until catalog-004 adds nexus.manifest_backfill() with idempotency.
        String docId = "mf-idem-doc-1";
        String chash = validChash("mf-idem-chsh0001");

        // Fixture: doc with physical_collection; manifest row with collection = NULL
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks " +
                "  (tenant_id, doc_id, position, chash) " +
                "VALUES ('" + TENANT_A + "', '" + docId + "', 0, '" + chash + "') " +
                "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
        }

        // First call — stamps collection
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery("SELECT " + FN_BACKFILL + "()");
            rs.next();
            long firstCount = rs.getLong(1);
            assertThat(firstCount)
                .as("manifest_backfill() first call must stamp >= 1 row (the fixture NULL row)")
                .isGreaterThanOrEqualTo(1L);
        }

        // Second call — must return 0 (idempotent: nothing left to stamp)
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery("SELECT " + FN_BACKFILL + "()");
            rs.next();
            long secondCount = rs.getLong(1);
            assertThat(secondCount)
                .as("manifest_backfill() second call must return 0 (idempotent: no NULL collections remain)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — manifest_backfill: tombstone-aware (skips tombstoned doc rows)
    //
    // EXPECTED RED: nexus.manifest_backfill function absent until catalog-004.
    //
    // Per the bead spec: manifest_backfill is tombstone-aware.
    // A manifest row whose parent doc is tombstoned should NOT be stamped
    // (the doc is being retired, not migrated).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void manifestBackfill_skipsTombstonedDocRows() throws Exception {
        // RED until catalog-004 adds nexus.manifest_backfill() tombstone-aware.
        String docTombed = "mf-bf-tomb-doc-1";
        String chash     = validChash("mf-bf-tomb-chsh01");

        // Fixture: tombstoned doc; manifest row with collection = NULL
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, docTombed, COLLECTION_384);
            // Tombstone it
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docTombed + "'");
            // Insert manifest row with collection = NULL
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks " +
                "  (tenant_id, doc_id, position, chash) " +
                "VALUES ('" + TENANT_A + "', '" + docTombed + "', 0, '" + chash + "') " +
                "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
        }

        // Call manifest_backfill()
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute("SELECT " + FN_BACKFILL + "()");
        }

        // Tombstoned doc's manifest row must NOT be stamped (collection stays NULL)
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT collection FROM nexus.catalog_document_chunks " +
                "WHERE tenant_id = '" + TENANT_A + "' AND doc_id = '" + docTombed + "' AND position = 0");
            assertThat(rs.next()).as("manifest row for tombstoned doc must still exist").isTrue();
            assertThat(rs.getString("collection"))
                .as("manifest_backfill() must NOT stamp collection for tombstoned docs " +
                    "(tombstone-aware: joins deleted_at IS NULL on parent doc)")
                .isNull();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 7 — document_text: ordered chunk reconstruction
    //
    // EXPECTED RED: nexus.document_text function absent until catalog-004.
    //
    // Fixture: doc with 3 manifest rows at positions 0, 1, 2;
    // each position has a chunk with distinct text.
    // Assert: document_text returns rows in manifest position order
    // with the correct chunk_text at each position.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void documentText_returnsChunksInManifestPositionOrder() throws Exception {
        // RED until catalog-004 adds nexus.document_text(text).
        String docId  = "mf-doctext-doc-1";
        String chash0 = validChash("mf-doctext-c0000");
        String chash1 = validChash("mf-doctext-c1111");
        String chash2 = validChash("mf-doctext-c2222");
        String text0  = "first chunk of document";
        String text1  = "second chunk of document";
        String text2  = "third chunk of document";

        // Fixture: 3-chunk doc with ordered manifest
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 0, chash0, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 1, chash1, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 2, chash2, COLLECTION_384);
            insertChunk384(su, TENANT_A, COLLECTION_384, chash0, text0);
            insertChunk384(su, TENANT_A, COLLECTION_384, chash1, text1);
            insertChunk384(su, TENANT_A, COLLECTION_384, chash2, text2);
        }

        // Call document_text via svc role with GUC set — RED trigger: function absent
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT position, chunk_text FROM " + FN_DOC_TEXT + "('" + docId + "') " +
                "ORDER BY position");

            // Position 0
            assertThat(rs.next()).as("row 0 must exist").isTrue();
            assertThat(rs.getInt("position")).as("position 0 must be 0").isEqualTo(0);
            assertThat(rs.getString("chunk_text"))
                .as("position 0 chunk_text must be '" + text0 + "'")
                .isEqualTo(text0);

            // Position 1
            assertThat(rs.next()).as("row 1 must exist").isTrue();
            assertThat(rs.getInt("position")).as("position 1 must be 1").isEqualTo(1);
            assertThat(rs.getString("chunk_text"))
                .as("position 1 chunk_text must be '" + text1 + "'")
                .isEqualTo(text1);

            // Position 2
            assertThat(rs.next()).as("row 2 must exist").isTrue();
            assertThat(rs.getInt("position")).as("position 2 must be 2").isEqualTo(2);
            assertThat(rs.getString("chunk_text"))
                .as("position 2 chunk_text must be '" + text2 + "'")
                .isEqualTo(text2);

            // No more rows
            assertThat(rs.next()).as("document_text must return exactly 3 rows (3 manifest positions)").isFalse();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — document_text: tombstone-aware (tombstoned doc returns empty set)
    //
    // EXPECTED RED: nexus.document_text function absent until catalog-004.
    //
    // A TOMBSTONED document must return an empty result set from document_text.
    // Rationale: never serve stale text from a retired document.
    // This is the "tombstone-aware" contract baked in from day one.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void documentText_tombstonedDoc_returnsEmptySet() throws Exception {
        // RED until catalog-004 adds nexus.document_text(text) tombstone-aware.
        String docId  = "mf-doctext-tomb-1";
        String chash  = validChash("mf-doctext-tomb-c");
        String chunkText = "chunk text that must never appear for tombstoned doc";

        // Fixture: doc + manifest + chunk (all present and correct for a live doc)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 0, chash, COLLECTION_384);
            insertChunk384(su, TENANT_A, COLLECTION_384, chash, chunkText);
        }

        // Tombstone the document
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docId + "'");
        }

        // document_text must return EMPTY SET for the tombstoned doc
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_DOC_TEXT + "('" + docId + "')");
            rs.next();
            assertThat(rs.getLong(1))
                .as("document_text must return empty set for a tombstoned doc " +
                    "(tombstone-aware contract: never stale text from retired documents)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — document_text: manifest gap contract
    //
    // EXPECTED RED: nexus.document_text function absent until catalog-004.
    //
    // Contract (pinned and documented in the function comment):
    //   A manifest row with no corresponding chunk_text in chunks_<dim> is SKIPPED
    //   (the function returns the chunks it can resolve, silently omitting gaps).
    //   Rationale: partial reconstruction is better than a hard failure for callers
    //   that just need the available text. The manifest_orphans function is the
    //   integrity check; document_text is the reader.
    //
    // Test: doc with 2 manifest rows; chunk at position 0 exists, chunk at position 1
    // is missing. document_text must return exactly 1 row (position 0 only).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void documentText_manifestGap_skipsGapRow() throws Exception {
        // RED until catalog-004 adds nexus.document_text(text).
        //
        // Contract: manifest gap (missing chunk) is silently SKIPPED — document_text
        // returns the resolvable positions. Use manifest_orphans for integrity checks.
        String docId   = "mf-doctext-gap-1";
        String chash0  = validChash("mf-doctext-gap-c0");
        String chash1  = validChash("mf-doctext-gap-c1");
        String text0   = "first chunk present";
        // chash1 intentionally has NO chunk row

        // Fixture: doc + 2 manifest rows; only position 0 has a chunk
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_384);
            insertCatalogDocument(su, TENANT_A, docId, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 0, chash0, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_A, docId, 1, chash1, COLLECTION_384);
            insertChunk384(su, TENANT_A, COLLECTION_384, chash0, text0);
            // chash1 has NO chunk row — this is the gap
        }

        // document_text must return exactly 1 row (position 0; position 1 gap is skipped)
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT position, chunk_text FROM " + FN_DOC_TEXT + "('" + docId + "') " +
                "ORDER BY position");

            assertThat(rs.next()).as("first row must exist (position 0)").isTrue();
            assertThat(rs.getInt("position")).as("first row must be position 0").isEqualTo(0);
            assertThat(rs.getString("chunk_text"))
                .as("position 0 chunk_text must match")
                .isEqualTo(text0);

            assertThat(rs.next())
                .as("document_text must return exactly 1 row when position 1 chunk is missing " +
                    "(contract: manifest gap is silently skipped — use manifest_orphans for integrity checks)")
                .isFalse();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 10 — SECURITY INVOKER / grants sanity
    //
    // EXPECTED RED: functions absent until catalog-004.
    //
    // (a) All three functions exist in the nexus schema after catalog-004 lands.
    // (b) They are SECURITY INVOKER (not DEFINER).
    // (c) nexus_svc has EXECUTE privilege on all three.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(100)
    void allFunctions_existInNexusSchema() throws Exception {
        // RED until catalog-004 creates the functions.
        try (Connection su = pg.createConnection("")) {
            for (String fn : List.of("manifest_orphans", "manifest_backfill", "document_text")) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT count(*) FROM information_schema.routines " +
                    "WHERE routine_schema = 'nexus' AND routine_name = '" + fn + "'");
                rs.next();
                assertThat(rs.getLong(1))
                    .as("function nexus." + fn + " must exist in the nexus schema (catalog-004)")
                    .isGreaterThanOrEqualTo(1L);
            }
        }
    }

    @Test @Order(101)
    void allFunctions_areSecurityInvoker() throws Exception {
        // RED until catalog-004 creates SECURITY INVOKER functions.
        try (Connection su = pg.createConnection("")) {
            // pg_proc.prosecdef = true means SECURITY DEFINER; false means SECURITY INVOKER
            for (String fn : List.of("manifest_orphans", "manifest_backfill", "document_text")) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT prosecdef FROM pg_proc p " +
                    "JOIN pg_namespace n ON n.oid = p.pronamespace " +
                    "WHERE n.nspname = 'nexus' AND p.proname = '" + fn + "' " +
                    "LIMIT 1");
                assertThat(rs.next())
                    .as("function nexus." + fn + " must exist (catalog-004)")
                    .isTrue();
                assertThat(rs.getBoolean("prosecdef"))
                    .as("nexus." + fn + " must be SECURITY INVOKER (prosecdef=false), not SECURITY DEFINER")
                    .isFalse();
            }
        }
    }

    @Test @Order(102)
    void allFunctions_nexusSvcHasExecuteGrant() throws Exception {
        // RED until catalog-004 grants EXECUTE to nexus_svc.
        try (Connection su = pg.createConnection("")) {
            for (String fn : List.of("manifest_orphans(integer)", "manifest_backfill()", "document_text(text)")) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT has_function_privilege('nexus_svc', 'nexus." + fn + "', 'EXECUTE')");
                rs.next();
                assertThat(rs.getBoolean(1))
                    .as("nexus_svc must have EXECUTE on nexus." + fn + " (catalog-004 grants)")
                    .isTrue();
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 11 — RLS isolation: document_text via svc role
    //
    // EXPECTED RED: nexus.document_text function absent until catalog-004.
    //
    // document_text is tenant-scoped via nexus.tenant GUC (SECURITY INVOKER under
    // FORCE RLS on catalog_document_chunks). A svc role with GUC=A must not see
    // text from tenant B's documents.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(110)
    void documentText_rlsIsolation_tenantACannotReadTenantBDoc() throws Exception {
        // RED until catalog-004 adds nexus.document_text(text).
        String docB  = "mf-rls-doc-b-1";
        String chashB = validChash("mf-rls-doc-b-c00");
        String textB  = "tenant B secret chunk text";

        // Fixture: tenant B's doc with a chunk
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_B, COLLECTION_384);
            insertCatalogDocument(su, TENANT_B, docB, COLLECTION_384);
            insertManifestRowWithCollection(su, TENANT_B, docB, 0, chashB, COLLECTION_384);
            insertChunk384(su, TENANT_B, COLLECTION_384, chashB, textB);
        }

        // Call document_text for tenant B's doc via GUC=A — must return empty set (RLS filters)
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_DOC_TEXT + "('" + docB + "')");
            rs.next();
            assertThat(rs.getLong(1))
                .as("document_text called with GUC=A must return 0 rows for tenant B's doc " +
                    "(RLS isolation: SECURITY INVOKER under FORCE RLS filters by tenant)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Insert a minimal catalog_documents row with a physical_collection.
     * Idempotent via ON CONFLICT DO NOTHING.
     */
    private static void insertCatalogDocument(Connection su, String tenantId, String tumbler,
                                               String physicalCollection) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, physical_collection) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "', '" + physicalCollection + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    /**
     * Insert a catalog_collections row. Required by fk-002 NOT VALID FKs.
     * Idempotent via ON CONFLICT DO NOTHING.
     */
    private static void insertCollection(Connection su, String tenantId, String name)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
            "VALUES ('" + tenantId + "', '" + name + "') " +
            "ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    /**
     * Insert a catalog_document_chunks manifest row WITH a collection value.
     * chash MUST be exactly 32 hex characters (catalog-002-hygiene CHECK, NOT VALID).
     * PK: (tenant_id, doc_id, position). Idempotent via ON CONFLICT DO NOTHING.
     */
    private static void insertManifestRowWithCollection(Connection su, String tenantId,
                                                         String docId, int position,
                                                         String chash, String collection)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks " +
            "  (tenant_id, doc_id, position, chash, collection) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", '" + chash + "', '" + collection + "') " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    /**
     * Insert a chunks_384 row. Collection must be pre-registered (fk-002 NOT VALID FK).
     * chash MUST be exactly 32 hex characters. PK: (tenant_id, collection, chash).
     * Superuser insert bypasses FORCE RLS so direct fixture setup is possible.
     */
    private static void insertChunk384(Connection su, String tenantId, String collection,
                                        String chash, String chunkText) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
            "VALUES ('" + tenantId + "', '" + collection + "', '" + chash + "', " +
            "'" + chunkText.replace("'", "''") + "', " + vectorLiteral(384) + "::vector) " +
            "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    /**
     * Count catalog_document_chunks rows for (tenantId, docId).
     */
    private static int countManifest(Connection conn, String tenantId, String docId)
            throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(
            "SELECT COUNT(*) FROM nexus.catalog_document_chunks " +
            "WHERE tenant_id = '" + tenantId + "' AND doc_id = '" + docId + "'");
        rs.next();
        return rs.getInt(1);
    }

    /**
     * Count chunks_384 rows for (tenantId, collection).
     */
    private static int countChunks384(Connection conn, String tenantId, String collection)
            throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(
            "SELECT COUNT(*) FROM nexus.chunks_384 " +
            "WHERE tenant_id = '" + tenantId + "' AND collection = '" + collection + "'");
        rs.next();
        return rs.getInt(1);
    }

    /**
     * Generate a pgvector literal string of {@code dim} uniform 0.1 components.
     * Format: {@code '[0.1,0.1,...,0.1]'} — safe for inline {@code ::vector} cast.
     */
    private static String vectorLiteral(int dim) {
        return IntStream.range(0, dim)
                        .mapToObj(i -> "0.1")
                        .collect(Collectors.joining(",", "'[", "]'"));
    }

    /**
     * Return a valid 32-character hex chash deterministically derived from {@code seed}.
     * Matches SoftDeleteTest.validChash() pattern.
     */
    private static String validChash(String seed) {
        String hex = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return hex;
    }
}
