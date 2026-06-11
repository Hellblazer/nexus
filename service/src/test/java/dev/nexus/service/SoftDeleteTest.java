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
 * RDR-156 bead nexus-70r3c.5 — TDD-RED suite for P1 soft delete (Decision 6).
 *
 * <p><strong>Scope boundary (Decision 6)</strong>:
 * <ul>
 *   <li>NO {@code DocumentSoftDeleted} / {@code DocumentPurged} event types — event-sourced
 *       model died with RDR-152; direct Postgres schema feature only.</li>
 *   <li>NO RDR-107 Chroma-metadata tombstones — stay dead (superseded by RDR-108).</li>
 *   <li>NO events.jsonl back-compat — out of scope.</li>
 * </ul>
 *
 * <p><strong>P1.2 (bead nexus-70r3c.6) will deliver:</strong>
 * <ul>
 *   <li>{@code deleted_at timestamptz NULL} column on {@code nexus.catalog_documents}
 *       and {@code nexus.catalog_links}.</li>
 *   <li>Partial indexes {@code WHERE deleted_at IS NULL} on both tables to keep hot
 *       paths as fast as today.</li>
 *   <li>Function {@code nexus.document_trash(tumbler text) RETURNS void}:
 *       {@code UPDATE catalog_documents SET deleted_at = NOW() WHERE tenant_id = current_setting('nexus.tenant',true) AND tumbler = $1}.
 *       SECURITY INVOKER, runs under FORCE RLS — tombstone is an UPDATE so fk-001 CASCADE
 *       chains do NOT fire; manifest/aspects/highlights survive.</li>
 *   <li>Function {@code nexus.document_restore(tumbler text) RETURNS void}:
 *       {@code UPDATE catalog_documents SET deleted_at = NULL WHERE …}.
 *       SECURITY INVOKER under FORCE RLS.</li>
 *   <li>Function {@code nexus.purge_trash(older_than interval) RETURNS bigint}:
 *       SECURITY INVOKER under FORCE RLS. Checks {@code current_setting('nexus.tenant', true)}
 *       is non-empty and RAISEs with a message mentioning "tenant" when unset.
 *       Physically DELETEs catalog_documents rows WHERE deleted_at &lt;= NOW() - older_than
 *       (the fk-001 ON DELETE CASCADE then fires, removing manifest/aspects/highlights).
 *       Then sweeps orphaned chunk rows: a row in {@code nexus.chunks_<dim>} is
 *       removable only when NO live (deleted_at IS NULL) manifest row references its
 *       chash for the same tenant — anti-join against catalog_document_chunks ⋈
 *       catalog_documents WHERE deleted_at IS NULL. Returns count of documents purged.</li>
 *   <li>View {@code nexus.live_chunks}: SECURITY INVOKER anti-join — excludes chunk rows
 *       whose only referencing manifest rows belong to tombstoned documents. Consumers
 *       never see a {@code deleted_at} column from this view. Selects from
 *       {@code nexus.chunks_384} (the primary dimension table used in tests; the full
 *       view covers all three dim tables or is dim-parametric — pin the 384 contract).</li>
 * </ul>
 *
 * <p><strong>Pinned contracts P1.2 MUST honor (derived from test assertions below):</strong>
 * <ol>
 *   <li><em>document_trash signature</em>: {@code nexus.document_trash(text)} — tumbler only;
 *       tenant scoped via {@code current_setting('nexus.tenant', true)} GUC (SECURITY INVOKER
 *       under FORCE RLS). When called with svc-role GUC set, tombstones exactly the matching
 *       tenant's document.</li>
 *   <li><em>document_restore signature</em>: {@code nexus.document_restore(text)} — tumbler
 *       only; same GUC scoping.</li>
 *   <li><em>purge_trash signature</em>: {@code nexus.purge_trash(interval)} — older_than
 *       interval; same GUC scoping. MUST RAISE when GUC is not set (even for BYPASSRLS/superuser
 *       callers) — cross-tenant purge must never be possible via an unscoped call. Message
 *       must contain "tenant" (case-insensitive matching).</li>
 *   <li><em>purge_trash age filter</em>: a tombstoned doc with {@code deleted_at &gt; NOW() - older_than}
 *       is NOT purged.</li>
 *   <li><em>purge_trash orphan sweep</em>: a chunk row referenced by at least one LIVE
 *       (non-tombstoned) document's manifest is NOT swept — shared-chash safety.</li>
 *   <li><em>live_chunks view</em>: selects from {@code nexus.chunks_384} (at minimum);
 *       exposes chunk columns but NOT {@code deleted_at}; a chunk whose only manifest
 *       reference is a tombstoned doc is absent from the view; a shared chunk with a live
 *       doc is present.</li>
 *   <li><em>RLS trash contract</em>: calling {@code document_trash(tumbler)} via svc-role
 *       with GUC=A while targeting tenant-B's tumbler affects 0 rows (RLS filters; the
 *       function does not raise — it silently affects nothing, exactly as a WHERE-filtered
 *       UPDATE does). Test GROUP 8 pins this as the "0 rows affected" contract.</li>
 * </ol>
 *
 * <p><strong>Expected RED/GREEN before P1.2 lands:</strong>
 * <ul>
 *   <li>GROUP 1 (schema — deleted_at columns): RED — columns absent</li>
 *   <li>GROUP 2 (tombstone leaves children intact): RED — function absent</li>
 *   <li>GROUP 3 (restore round-trip): RED — function absent</li>
 *   <li>GROUP 4 (purge orphan sweep): RED — function absent</li>
 *   <li>GROUP 5 (purge GUC guard): RED — function absent</li>
 *   <li>GROUP 6 (purge age filter): RED — function absent</li>
 *   <li>GROUP 7 (live_chunks view): RED — view absent</li>
 *   <li>GROUP 8 (RLS isolation on trash/restore): RED — function absent</li>
 *   <li>All CONTROL paths (fixture inserts, cascade counts before purge): GREEN always</li>
 * </ul>
 *
 * <p>Verified schema facts from the current master changelog (post-P0 schema applies):
 * <ul>
 *   <li>fk-001 CASCADE chains: deleting a catalog_documents row fires ON DELETE CASCADE to
 *       catalog_document_chunks, document_aspects (FK: fk_doc_aspects_catalog_doc),
 *       document_highlights (FK: fk_doc_highlights_catalog_doc),
 *       aspect_extraction_queue (FK: fk_aspect_queue_catalog_doc).</li>
 *   <li>fk-002 (post-P0): chunks_384/768/1024(tenant_id,collection) → catalog_collections NOT VALID
 *       ON DELETE RESTRICT. This means chunk rows require a registered collection.
 *       Chunk inserts in fixtures MUST go via superuser (BYPASSRLS) AND must register the
 *       collection first to satisfy fk-002.</li>
 *   <li>catalog_document_chunks.chash is 32 chars (catalog-002-hygiene CHECK, NOT VALID).
 *       All fixture chash values MUST be exactly 32 hex characters.</li>
 *   <li>catalog_documents PK: (tenant_id, tumbler). FORCE RLS on catalog_documents.</li>
 * </ul>
 *
 * <p>Mirror conventions from CollectionRegistryFkTest / ForeignKeyConstraintTest:
 * PgContainerHelper.start(), Liquibase master changelog, PER_CLASS, @Order, AssertJ,
 * superuser conn for fixtures, svc role + GUC for RLS tests.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class SoftDeleteTest {

    // ── Tenant IDs ─────────────────────────────────────────────────────────────
    private static final String TENANT_A = "sd-tenant-a";
    private static final String TENANT_B = "sd-tenant-b";

    // ── Svc role (NOSUPERUSER, NOBYPASSRLS — subject to FORCE RLS) ────────────
    private static final String SVC_ROLE = "svc_sd_test";
    private static final String SVC_PASS = "svc_sd_test_pass";

    // ── Function / view names (pinned contract for P1.2 to honor) ─────────────
    /**
     * Tombstones a document: {@code nexus.document_trash(tumbler text) RETURNS void}.
     * SECURITY INVOKER; tenant scoped via nexus.tenant GUC.
     */
    private static final String FN_TRASH   = "nexus.document_trash";

    /**
     * Restores a tombstoned document: {@code nexus.document_restore(tumbler text) RETURNS void}.
     * SECURITY INVOKER; tenant scoped via nexus.tenant GUC.
     */
    private static final String FN_RESTORE = "nexus.document_restore";

    /**
     * Purges trash: {@code nexus.purge_trash(older_than interval) RETURNS bigint}.
     * SECURITY INVOKER; must RAISE when nexus.tenant GUC is unset.
     */
    private static final String FN_PURGE   = "nexus.purge_trash";

    /**
     * Anti-join view excluding chunks whose only referencing manifest doc is tombstoned.
     * {@code nexus.live_chunks} — consumers never see {@code deleted_at}.
     */
    private static final String VIEW_LIVE_CHUNKS = "nexus.live_chunks";

    // ── Test collection (post-P0: must be registered in catalog_collections) ──
    private static final String COLLECTION_A = "knowledge__sd-owner-a__voyage-context-3__v1";
    private static final String COLLECTION_B = "knowledge__sd-owner-b__voyage-context-3__v1";

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

        // Phase 2: apply full master changelog (all current changesets; no P1.2 yet)
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to tables needed for RLS tests.
        // The master changelog's grants-nexus-svc.xml covers nexus_svc via runAlways.
        // svc_sd_test is test-specific and gets explicit per-table grants.
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

        // HikariCP svc role pool (NOSUPERUSER NOBYPASSRLS — subject to RLS).
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
    // GROUP 1 — Schema: deleted_at columns exist with correct type and nullability
    //
    // EXPECTED RED: columns absent until P1.2 adds them.
    // The information_schema query must find the column; it cannot if the column is absent.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void catalogDocuments_deletedAt_isTimestamptzNullable() throws Exception {
        // RED until P1.2 adds `deleted_at timestamptz NULL` to catalog_documents.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT data_type, is_nullable " +
                "FROM information_schema.columns " +
                "WHERE table_schema = 'nexus' " +
                "  AND table_name   = 'catalog_documents' " +
                "  AND column_name  = 'deleted_at'");
            assertThat(rs.next())
                .as("catalog_documents.deleted_at column must exist (P1.2 adds `deleted_at timestamptz NULL`)")
                .isTrue();
            assertThat(rs.getString("data_type"))
                .as("catalog_documents.deleted_at must be 'timestamp with time zone'")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.getString("is_nullable"))
                .as("catalog_documents.deleted_at must be nullable (tombstone = set, live = NULL)")
                .isEqualTo("YES");
        }
    }

    @Test @Order(11)
    void catalogLinks_deletedAt_isTimestamptzNullable() throws Exception {
        // RED until P1.2 adds `deleted_at timestamptz NULL` to catalog_links.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT data_type, is_nullable " +
                "FROM information_schema.columns " +
                "WHERE table_schema = 'nexus' " +
                "  AND table_name   = 'catalog_links' " +
                "  AND column_name  = 'deleted_at'");
            assertThat(rs.next())
                .as("catalog_links.deleted_at column must exist (P1.2 adds `deleted_at timestamptz NULL`)")
                .isTrue();
            assertThat(rs.getString("data_type"))
                .as("catalog_links.deleted_at must be 'timestamp with time zone'")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.getString("is_nullable"))
                .as("catalog_links.deleted_at must be nullable")
                .isEqualTo("YES");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — Tombstone leaves fk-001 CASCADE children intact
    //
    // EXPECTED RED: nexus.document_trash function absent until P1.2.
    // The critical property: tombstoning is an UPDATE (sets deleted_at), NOT a DELETE.
    // Therefore the ON DELETE CASCADE chains to manifest/aspects/highlights do NOT fire.
    // Children survive; restore is clearing one column.
    //
    // Fixture: 2 catalog_document_chunks rows + 1 document_aspects row.
    // After trash: deleted_at set; manifest count == 2; aspects count == 1.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void tombstone_leavesChildrenIntact_manifestAndAspectsCountUnchanged() throws Exception {
        // RED until P1.2 adds nexus.document_trash(text).
        // CONTROL: fixture inserts must succeed before the function call.
        String tumbler = "sd-tomb-doc-1";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, tumbler);

            // 2 manifest rows — post-P0: needs 32-char chash
            insertManifestRow(su, TENANT_A, tumbler, 0, validChash("tomb-chunk-0"));
            insertManifestRow(su, TENANT_A, tumbler, 1, validChash("tomb-chunk-1"));

            // 1 document_aspects row (fk-001 ON DELETE CASCADE target)
            insertAspectRow(su, TENANT_A, tumbler, "knowledge__sd-asp__v1", "sd-aspect-path-1");
        }

        // CONTROL: confirm fixture rows present
        try (Connection su = pg.createConnection("")) {
            assertThat(countManifest(su, TENANT_A, tumbler))
                .as("CONTROL: 2 manifest rows must be present before tombstone")
                .isEqualTo(2);
            assertThat(countAspects(su, TENANT_A, tumbler))
                .as("CONTROL: 1 aspect row must be present before tombstone")
                .isEqualTo(1);
        }

        // Call document_trash via svc role with GUC set — this is the RED trigger.
        // The function nexus.document_trash(text) does not exist yet; PSQLException propagates.
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_TRASH + "('" + tumbler + "')");
        }

        // Post-tombstone assertions (green after P1.2 lands):
        try (Connection su = pg.createConnection("")) {
            // deleted_at must be set
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + tumbler + "'");
            assertThat(rs.next()).as("document row must still exist after tombstone").isTrue();
            assertThat(rs.getTimestamp("deleted_at"))
                .as("deleted_at must be set (not NULL) after document_trash")
                .isNotNull();

            // CASCADE chains must NOT have fired — children stay intact
            assertThat(countManifest(su, TENANT_A, tumbler))
                .as("manifest count must be == 2 after tombstone (CASCADE did not fire)")
                .isEqualTo(2);
            assertThat(countAspects(su, TENANT_A, tumbler))
                .as("aspect count must be == 1 after tombstone (CASCADE did not fire)")
                .isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — Restore round-trip
    //
    // EXPECTED RED: nexus.document_restore function absent until P1.2.
    // After restore: deleted_at IS NULL; document is visible on the live path.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void restore_roundTrip_clearsDeletedAt_andDocIsVisible() throws Exception {
        // RED until P1.2 adds nexus.document_restore(text).
        String tumbler = "sd-restore-doc-1";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, tumbler);
        }

        // Tombstone first (also triggers RED if trash absent — acceptable; restore test
        // is the primary target; both are RED for the same reason: function absent).
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_TRASH + "('" + tumbler + "')");
        }

        // Now restore
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_RESTORE + "('" + tumbler + "')");
        }

        // After restore: deleted_at IS NULL; document appears in live-path SELECT
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + tumbler + "'");
            assertThat(rs.next()).as("document row must still exist after restore").isTrue();
            assertThat(rs.getTimestamp("deleted_at"))
                .as("deleted_at must be NULL after document_restore (document is live again)")
                .isNull();

            // Live-path query: SELECT WHERE deleted_at IS NULL
            ResultSet live = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_A + "' " +
                "  AND tumbler = '" + tumbler + "' " +
                "  AND deleted_at IS NULL");
            live.next();
            assertThat(live.getInt(1))
                .as("live-path SELECT must find exactly 1 row after restore")
                .isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — purge_trash: orphan-only chunk sweep
    //
    // EXPECTED RED: nexus.purge_trash function absent until P1.2.
    //
    // Setup:
    //   Doc A (tombstoned, old): manifest references chash_A (A-only) and chash_shared.
    //   Doc B (live):            manifest references chash_shared.
    //   Both docs use COLLECTION_A (registered).
    //
    // After purge_trash('0 seconds'::interval):
    //   - Doc A and its manifest rows are physically DELETEd (cascade from purge).
    //   - chash_A chunk row is swept (no live manifest references it).
    //   - chash_shared chunk row is NOT swept (doc B's live manifest still references it).
    //
    // Exact chunk counts: before=2 (chash_A + chash_shared), after=1 (chash_shared only).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void purge_sweepsOrphanChunks_preservesSharedChunk() throws Exception {
        // RED until P1.2 adds nexus.purge_trash(interval).
        String tumblerA = "sd-purge-doc-a";
        String tumblerB = "sd-purge-doc-b";
        String chashA      = validChash("purge-only-a");     // referenced by A only
        String chashShared = validChash("purge-shared");     // referenced by A and B

        // Fixture setup via superuser (bypasses FORCE RLS + fk-002 check needs su for collection)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Register collection so fk-002 NOT VALID FK is satisfied for new inserts
            insertCollection(su, TENANT_A, COLLECTION_A);

            // Doc A (will be tombstoned)
            insertCatalogDocument(su, TENANT_A, tumblerA);
            insertManifestRow(su, TENANT_A, tumblerA, 0, chashA);
            insertManifestRow(su, TENANT_A, tumblerA, 1, chashShared);

            // Doc B (live — keeps chash_shared alive)
            insertCatalogDocument(su, TENANT_A, tumblerB);
            insertManifestRow(su, TENANT_A, tumblerB, 0, chashShared);

            // Insert the actual chunk rows into chunks_384
            insertChunk384(su, TENANT_A, COLLECTION_A, chashA,      "text for chunk A only");
            insertChunk384(su, TENANT_A, COLLECTION_A, chashShared, "shared chunk text");
        }

        // CONTROL: verify fixture
        try (Connection su = pg.createConnection("")) {
            assertThat(countChunks384(su, TENANT_A, COLLECTION_A))
                .as("CONTROL: 2 chunk rows must exist before tombstone+purge")
                .isEqualTo(2);
        }

        // Tombstone doc A via svc role
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_TRASH + "('" + tumblerA + "')");
        }

        // Purge (older_than = 0 seconds: tombstone is always older than "now - 0s")
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_PURGE + "('0 seconds'::interval)");
        }

        // Post-purge assertions
        try (Connection su = pg.createConnection("")) {
            // Doc A must be gone (physically deleted by purge)
            ResultSet rsA = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + tumblerA + "'");
            rsA.next();
            assertThat(rsA.getInt(1))
                .as("doc A must be physically deleted by purge_trash")
                .isEqualTo(0);

            // Doc B must still be live
            ResultSet rsB = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + tumblerB + "' AND deleted_at IS NULL");
            rsB.next();
            assertThat(rsB.getInt(1))
                .as("doc B must still be live after purge")
                .isEqualTo(1);

            // Only 1 chunk row must survive: chash_shared (referenced by live doc B)
            assertThat(countChunks384(su, TENANT_A, COLLECTION_A))
                .as("chunk count must be == 1 after purge: chash_shared survives (live doc B), chash_A swept")
                .isEqualTo(1);

            // The surviving chunk must be chash_shared
            ResultSet rsChunk = su.createStatement().executeQuery(
                "SELECT chash FROM nexus.chunks_384 " +
                "WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + COLLECTION_A + "'");
            assertThat(rsChunk.next()).isTrue();
            assertThat(rsChunk.getString("chash"))
                .as("surviving chunk must be chash_shared (the chunk still referenced by live doc B)")
                .isEqualTo(chashShared);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — purge_trash GUC guard
    //
    // EXPECTED RED: nexus.purge_trash function absent until P1.2.
    // When called with NO nexus.tenant GUC (superuser / BYPASSRLS connection where
    // current_setting('nexus.tenant', true) returns empty/null), purge_trash MUST RAISE.
    // The function body checks the GUC and raises rather than executing an unscoped purge
    // that would cross tenants. Error message must contain "tenant" (case-insensitive).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void purge_raisesWhenTenantGucUnset() throws Exception {
        // RED until P1.2 adds nexus.purge_trash(interval) with the GUC guard.
        // Superuser connection: BYPASSRLS role, no nexus.tenant GUC set.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Confirm GUC is not set (returns '' or null for missing)
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT current_setting('nexus.tenant', true) AS t");
            rs.next();
            String guc = rs.getString("t");
            assertThat(guc == null || guc.isEmpty())
                .as("CONTROL: nexus.tenant GUC must be unset on fresh superuser connection")
                .isTrue();

            // Call purge_trash — must RAISE with a message mentioning "tenant" (the GUC guard).
            // RED now: PSQLException fires because the function does not exist yet;
            //   message = "function nexus.purge_trash(interval) does not exist" — names the artifact.
            //   The .contains("tenant") assertion then FAILS, which is the RED signal.
            // GREEN after P1.2: function exists; raises with message containing "tenant".
            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "SELECT " + FN_PURGE + "('1 hour'::interval)")
            );
            assertThat(ex.getMessage().toLowerCase())
                .as("purge_trash must raise an error mentioning 'tenant' when GUC is unset " +
                    "(Decision 6: cross-tenant purge must be impossible via unscoped call). " +
                    "RED now because nexus.purge_trash does not exist yet; " +
                    "GREEN after P1.2 when the function exists and enforces the GUC guard.")
                .contains("tenant");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — purge_trash age filter
    //
    // EXPECTED RED: nexus.purge_trash function absent until P1.2.
    // A tombstoned document whose deleted_at is NEWER than older_than must NOT be purged.
    // Children must remain intact; document still has deleted_at set.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void purge_ageFilter_doesNotPurgeRecentTombstone() throws Exception {
        // RED until P1.2 adds nexus.purge_trash(interval).
        String tumbler = "sd-age-filter-doc-1";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_A, tumbler);
            insertManifestRow(su, TENANT_A, tumbler, 0, validChash("age-filter-chunk0"));
            insertAspectRow(su, TENANT_A, tumbler, "knowledge__sd-age__v1", "sd-age-asp-path-1");
        }

        // Tombstone the document (just now — it will be "new")
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_TRASH + "('" + tumbler + "')");
        }

        // Purge with a very long older_than (e.g. 30 days) — the recent tombstone must NOT be purged
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_PURGE + "('30 days'::interval)");
        }

        // Document must still exist with deleted_at set (tombstoned but not purged)
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + tumbler + "'");
            assertThat(rs.next())
                .as("tombstoned-but-new doc must still exist (age filter: 30 days, tombstone just set)")
                .isTrue();
            assertThat(rs.getTimestamp("deleted_at"))
                .as("deleted_at must still be set (doc tombstoned, not purged by age filter)")
                .isNotNull();

            // Children must also still be intact (cascade did not fire — doc not yet purged)
            assertThat(countManifest(su, TENANT_A, tumbler))
                .as("manifest count must be == 1 (doc not purged — age filter held)")
                .isEqualTo(1);
            assertThat(countAspects(su, TENANT_A, tumbler))
                .as("aspect count must be == 1 (doc not purged — age filter held)")
                .isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 7 — live_chunks view
    //
    // EXPECTED RED: nexus.live_chunks view absent until P1.2.
    //
    // Properties tested:
    //   (a) A chunk whose only referencing manifest doc is tombstoned is ABSENT from live_chunks.
    //   (b) A shared chunk (live doc B also references it) IS PRESENT in live_chunks.
    //   (c) live_chunks exposes NO deleted_at column (consumers never see it).
    //
    // Uses a fresh two-doc fixture independent of GROUP 4.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void liveChunks_view_excludesTombstonedChunks_includesSharedChunk() throws Exception {
        // RED until P1.2 creates nexus.live_chunks view.
        String tumblerX = "sd-lc-doc-x";
        String tumblerY = "sd-lc-doc-y";
        String chashOrphan = validChash("lc-orphan-chunk");  // only in tombstoned doc X
        String chashLive   = validChash("lc-live-chunk");    // in both X (tombstoned) and Y (live)

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLLECTION_B);

            insertCatalogDocument(su, TENANT_A, tumblerX);
            insertManifestRow(su, TENANT_A, tumblerX, 0, chashOrphan);
            insertManifestRow(su, TENANT_A, tumblerX, 1, chashLive);

            insertCatalogDocument(su, TENANT_A, tumblerY);
            insertManifestRow(su, TENANT_A, tumblerY, 0, chashLive);

            insertChunk384(su, TENANT_A, COLLECTION_B, chashOrphan, "orphan chunk text");
            insertChunk384(su, TENANT_A, COLLECTION_B, chashLive,   "live shared chunk text");
        }

        // CONTROL: both chunks present before tombstone
        try (Connection su = pg.createConnection("")) {
            assertThat(countChunks384(su, TENANT_A, COLLECTION_B))
                .as("CONTROL: 2 chunk rows must be present before tombstone")
                .isEqualTo(2);
        }

        // Tombstone doc X
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_TRASH + "('" + tumblerX + "')");
        }

        // (a) Orphan chunk (only X references it; X is tombstoned) must be ABSENT from live_chunks
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) FROM " + VIEW_LIVE_CHUNKS +
                " WHERE chash = '" + chashOrphan + "'");
            rs.next();
            assertThat(rs.getInt(1))
                .as("orphan chunk (only tombstoned doc X references it) must be ABSENT from live_chunks")
                .isEqualTo(0);
        }

        // (b) Shared chunk (live doc Y still references it) must be PRESENT in live_chunks
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) FROM " + VIEW_LIVE_CHUNKS +
                " WHERE chash = '" + chashLive + "'");
            rs.next();
            assertThat(rs.getInt(1))
                .as("shared chunk (live doc Y references it) must be PRESENT in live_chunks")
                .isEqualTo(1);
        }

        // (c) live_chunks must NOT expose a deleted_at column (consumers never see it)
        try (Connection su = pg.createConnection("")) {
            ResultSet colRs = su.createStatement().executeQuery(
                "SELECT COUNT(*) FROM information_schema.columns " +
                "WHERE table_schema = 'nexus' " +
                "  AND table_name   = 'live_chunks' " +
                "  AND column_name  = 'deleted_at'");
            colRs.next();
            assertThat(colRs.getInt(1))
                .as("live_chunks view must NOT expose a deleted_at column " +
                    "(Decision 6: single enforcement point — consumers never see it)")
                .isEqualTo(0);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — RLS isolation on trash/restore
    //
    // EXPECTED RED: nexus.document_trash function absent until P1.2.
    //
    // Pinned contract: calling document_trash(tumbler) via svc-role with GUC=A while
    // targeting tenant-B's tumbler affects 0 rows (RLS filters the UPDATE silently).
    // The function does NOT raise — it returns normally with 0 rows affected.
    // This is the standard "where-filtered UPDATE returns 0 rows" SQL contract.
    //
    // Verified by: calling trash on B's tumbler via GUC=A, then confirming B's
    // document still has deleted_at IS NULL.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void rlsIsolation_trash_tenantA_cannotTombstoneTenantB_document() throws Exception {
        // RED until P1.2 adds nexus.document_trash(text).
        String tumblerTenantB = "sd-rls-doc-b-1";

        // Fixture: insert doc owned by TENANT_B
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_B, tumblerTenantB);
        }

        // As TENANT_A (svc role, GUC=A): call document_trash targeting TENANT_B's tumbler.
        // Pinned contract: 0 rows affected (FORCE RLS silently filters the UPDATE).
        // Function MUST NOT raise an error — it executes and affects nothing.
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            // This call should succeed (no exception) but affect 0 rows.
            svc.createStatement().execute(
                "SELECT " + FN_TRASH + "('" + tumblerTenantB + "')");
        }

        // Verify: TENANT_B's document is NOT tombstoned (deleted_at IS NULL)
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_B + "' AND tumbler = '" + tumblerTenantB + "'");
            assertThat(rs.next())
                .as("TENANT_B's document must still exist after cross-tenant trash attempt")
                .isTrue();
            assertThat(rs.getTimestamp("deleted_at"))
                .as("TENANT_B's document.deleted_at must remain NULL after cross-tenant trash attempt " +
                    "(RLS contract: 0 rows affected — svc role under GUC=A cannot tombstone B's docs)")
                .isNull();
        }
    }

    @Test @Order(81)
    void rlsIsolation_restore_tenantA_cannotRestoreTenantB_document() throws Exception {
        // RED until P1.2 adds nexus.document_restore(text).
        // Mirror of @Order(80): TENANT_A svc-role trying to restore TENANT_B's (already live) doc.
        // Pinned contract: 0 rows affected (FORCE RLS filters silently).
        String tumblerTenantB = "sd-rls-doc-b-2";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCatalogDocument(su, TENANT_B, tumblerTenantB);
        }

        // Attempt restore on TENANT_B's doc via GUC=A — must silently affect 0 rows
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            svc.createStatement().execute(
                "SELECT " + FN_RESTORE + "('" + tumblerTenantB + "')");
        }

        // Verify: TENANT_B's document is unaffected (deleted_at still NULL)
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents " +
                "WHERE tenant_id = '" + TENANT_B + "' AND tumbler = '" + tumblerTenantB + "'");
            assertThat(rs.next())
                .as("TENANT_B's document must still exist after cross-tenant restore attempt")
                .isTrue();
            assertThat(rs.getTimestamp("deleted_at"))
                .as("TENANT_B's document.deleted_at must remain NULL after cross-tenant restore attempt")
                .isNull();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Insert a minimal catalog_documents row. Idempotent via ON CONFLICT DO NOTHING.
     * catalog_documents PK: (tenant_id, tumbler); title NOT NULL.
     */
    private static void insertCatalogDocument(Connection su, String tenantId, String tumbler)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    /**
     * Insert a catalog_collections row. Required by fk-002 NOT VALID FKs for new chunk inserts.
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
     * Insert a catalog_document_chunks manifest row.
     * chash MUST be exactly 32 hex characters (catalog-002-hygiene CHECK, NOT VALID).
     * PK: (tenant_id, doc_id, position). Idempotent via ON CONFLICT DO NOTHING.
     * doc_id is the tumbler of the parent catalog_documents row (fk-001 FK).
     */
    private static void insertManifestRow(Connection su, String tenantId, String docId,
                                           int position, String chash) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", '" + chash + "') " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    /**
     * Insert a document_aspects row referencing a catalog_documents tumbler.
     * doc_id must match an existing catalog_documents(tenant_id, tumbler) row (fk-001 FK ON DELETE CASCADE).
     * Unique on (tenant_id, collection, source_path).
     */
    private static void insertAspectRow(Connection su, String tenantId, String tumbler,
                                         String collection, String sourcePath) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.document_aspects " +
            "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, doc_id) " +
            "VALUES ('" + tenantId + "', '" + collection + "', '" + sourcePath + "', NOW(), 'v1', 'docling', '" + tumbler + "') " +
            "ON CONFLICT (tenant_id, collection, source_path) DO NOTHING");
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
            "'" + chunkText + "', " + vectorLiteral(384) + "::vector) " +
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
     * Count document_aspects rows for (tenantId, docId).
     */
    private static int countAspects(Connection conn, String tenantId, String tumbler)
            throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(
            "SELECT COUNT(*) FROM nexus.document_aspects " +
            "WHERE tenant_id = '" + tenantId + "' AND doc_id = '" + tumbler + "'");
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
     * Matches the pattern from CollectionRegistryFkTest.vectorLiteral().
     */
    private static String vectorLiteral(int dim) {
        return IntStream.range(0, dim)
                        .mapToObj(i -> "0.1")
                        .collect(Collectors.joining(",", "'[", "]'"));
    }

    /**
     * Return a valid 32-character hex chash deterministically derived from {@code seed}.
     * Matches the pattern from CollectionRegistryFkTest.validChash().
     */
    private static String validChash(String seed) {
        String hex = (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
        return hex;
    }
}
