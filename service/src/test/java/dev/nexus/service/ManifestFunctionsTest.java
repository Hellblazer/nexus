// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.DimTables;
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
 * RDR-156 bead nexus-70r3c.8 — originally a TDD-RED suite for P2 manifest
 * functions (Decision 3): {@code nexus.manifest_orphans(dim int)},
 * {@code nexus.manifest_backfill()}, and {@code nexus.document_text(doc_id
 * text)}.
 *
 * <p><strong>RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15:</strong> the
 * {@code manifest_orphans}/{@code manifest_backfill} coverage (formerly
 * GROUP 1-6b) was DELETED here — the manifest-chunk FK (catalog-029) makes
 * the dangling state those functions detected unreachable, and
 * catalog-030-retire-manifest-verify.xml drops both SQL functions outright.
 * This file's remaining scope is {@code nexus.document_text(doc_id text)}
 * only — ordered manifest⋈chunk_text reconstruction, tombstone-aware
 * (tombstoned doc returns empty set) — which Decision item 4 does not name
 * and which stays live.
 *
 * <p><strong>Remaining coverage:</strong>
 * <ul>
 *   <li>GROUP 7 (document_text ordering)</li>
 *   <li>GROUP 8 (document_text tombstone-aware)</li>
 *   <li>GROUP 9 (document_text manifest gap contract)</li>
 *   <li>GROUP 10 (SECURITY INVOKER / grants — document_text only)</li>
 *   <li>GROUP 11 (RLS isolation — svc role sees only its tenant)</li>
 *   <li>GROUP 11b (document_text missing GUC returns empty set)</li>
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
    // FN_ORPHANS ("nexus.manifest_orphans") and FN_BACKFILL ("nexus.manifest_
    // backfill") REMOVED here (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
    // both SQL functions are DROPPED (catalog-030-retire-manifest-verify.xml);
    // the manifest-chunk FK makes the dangling state they detected/fixed
    // unreachable by construction. See this file's class javadoc for the full
    // scope-down (this file's remaining coverage is nexus.document_text only).

    /**
     * Ordered manifest⋈chunk_text reconstruction for a document.
     * {@code nexus.document_text(doc_id text) RETURNS TABLE(position int, chunk_text text)}.
     * SECURITY INVOKER; tenant-scoped via nexus.tenant GUC.
     * Tombstone-aware: tombstoned doc returns empty set (never stale text).
     */
    private static final String FN_DOC_TEXT  = "nexus.document_text";

    // ── Test collections (must be registered in catalog_collections per fk-002) ─
    // Collection names follow the conformant shape: <type>__<owner>__<model>__<version>
    // The model segment (split_part(name, '__', 3)) must match the _MODEL_DIMS tokens
    // this file's fixtures route against the unified nexus.chunks table's
    // embedding_<dim> columns (RDR-191 Phase 4; formerly a chunks_<dim> table).
    // 384: model token = 'minilm-l6-v2-384' (maps embedding_384)
    // 1024: model token = 'voyage-context-3' (maps embedding_1024)
    private static final String COLLECTION_384  = "knowledge__mf-owner-a__minilm-l6-v2-384__v1";
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
                    "aspect_extraction_queue")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            // RDR-191 Phase 4: chunks_384/768/1024 unified into ONE nexus.chunks --
            // a single GRANT now covers what three did.
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON " + DimTables.CHUNKS_TABLE_NAME + " TO " + SVC_ROLE);
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
            for (String fn : List.of("document_text")) {
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
            for (String fn : List.of("document_text")) {
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
            for (String fn : List.of("document_text(text)")) {
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
    // GROUP 11b — document_text: missing GUC returns empty set
    //
    // EXPECTED RED: nexus.document_text function absent until catalog-004.
    //
    // Reader contract: when nexus.tenant GUC is NOT set, current_setting returns
    // NULL (via the missing_ok=true arg). The WHERE m.tenant_id = NULL predicate
    // matches nothing, so the function returns 0 rows intentionally.
    //
    // This diverges from purge_trash's RAISE pattern deliberately:
    // destructive operations raise on missing GUC; readers return nothing.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(111)
    void documentText_missingGuc_returnsEmptySet() throws Exception {
        // RED until catalog-004 adds nexus.document_text(text).
        //
        // Use a superuser connection with NO nexus.tenant GUC configured.
        // The function's WHERE m.tenant_id = current_setting('nexus.tenant', true)
        // evaluates to WHERE m.tenant_id = NULL — matches nothing.
        String anyDocId = "mf-doctext-doc-1"; // exists from GROUP 7, has chunks

        try (Connection su = pg.createConnection("")) {
            // Confirm GUC is NOT set (reset to default)
            su.createStatement().execute("RESET nexus.tenant");

            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + FN_DOC_TEXT + "('" + anyDocId + "')");
            rs.next();
            long count = rs.getLong(1);
            assertThat(count)
                .as("document_text with no nexus.tenant GUC must return 0 rows " +
                    "(reader contract: no GUC = no tenant = no rows; missing_ok=true returns NULL, " +
                    "WHERE tenant_id = NULL matches nothing — diverges from purge_trash RAISE deliberately)")
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
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for every catalog_document_chunks insert. This
        // file exercises manifest_orphans/document_text/manifest_backfill, whose
        // whole point is detecting/handling a chash with NO matching chunk -- a
        // state the FK now prevents in normal operation (these functions are named
        // in RDR-191 Decision item 4 as retiring in a LATER phase, out of this
        // bead's scope). Bypass locally: drop the constraint, insert, then re-add
        // it NOT VALID (catalog-029-0's exact shape) so it is live again
        // (unvalidated) for every subsequent statement in this container.
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks " +
            "  (tenant_id, doc_id, position, chash, collection) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", '" + chash + "', '" + collection + "') " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks " +
            "ADD CONSTRAINT fk_catalog_chunks_chunk " +
            "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) " +
            "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
    }

    /**
     * Insert a nexus.chunks row (RDR-191 unified; formerly chunks_384) with embedding_384
     * populated. Collection must be pre-registered (fk-002 NOT VALID FK).
     * chash MUST be exactly 32 hex characters. PK: (tenant_id, collection, chash).
     * Superuser insert bypasses FORCE RLS so direct fixture setup is possible.
     */
    private static void insertChunk384(Connection su, String tenantId, String collection,
                                        String chash, String chunkText) throws Exception {
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(384) + ") " +
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
     * Count nexus.chunks rows with embedding_384 populated for (tenantId, collection).
     */
    private static int countChunks384(Connection conn, String tenantId, String collection)
            throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(
            "SELECT COUNT(*) FROM " + DimTables.CHUNKS_TABLE_NAME + " " +
            "WHERE tenant_id = '" + tenantId + "' AND collection = '" + collection + "'" +
            " AND " + DimTables.embeddingColumn(384) + " IS NOT NULL");
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
