// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.jooq.types.YearToSecond;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.HexFormat;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.LIVE_CHUNKS;
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
 *       Then sweeps orphaned chunk rows: a row in {@code nexus.chunks} is
 *       removable only when NO live (deleted_at IS NULL) manifest row references its
 *       chash for the same tenant — anti-join against catalog_document_chunks ⋈
 *       catalog_documents WHERE deleted_at IS NULL. Returns count of documents purged.</li>
 *   <li>View {@code nexus.live_chunks}: SECURITY INVOKER anti-join — excludes chunk rows
 *       whose only referencing manifest rows belong to tombstoned documents. Consumers
 *       never see a {@code deleted_at} column from this view. Selects from the unified
 *       {@code nexus.chunks} table (RDR-191 Phase 4; formerly a three-way UNION ALL over
 *       chunks_384/768/1024 — tests here pin the 384-dim fixture case).</li>
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
 *   <li><em>live_chunks view</em>: selects from the unified {@code nexus.chunks} table
 *       (dim=384 fixture case, at minimum); exposes chunk columns but NOT {@code deleted_at};
 *       a chunk whose only manifest
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
 *   <li>fk-002 (post-P0, RDR-191 Phase 5 pending -- NOT yet on the unified nexus.chunks
 *       as of this bead, see vectors-004-unify-chunks.xml's own S3 note):
 *       nexus.chunks(tenant_id,collection) → catalog_collections NOT VALID
 *       ON DELETE RESTRICT, eventually. Fixtures register the collection first regardless,
 *       forward-compatible with whenever that FK ships.
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

    // Function / view identities (nexus-cbo4a batch 10): document_trash/document_restore/
    // purge_trash are called via the generated jOOQ Routines (typed DSL), and live_chunks
    // via the generated LIVE_CHUNKS Table -- the FN_*/VIEW_* string constants that used to
    // back raw `SELECT nexus.xxx(...)` calls are retired along with those calls.

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
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
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
            PgCatalogProbes.ColumnInfo rs = PgCatalogProbes.columnInfo(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "catalog_documents", "deleted_at");
            assertThat(rs)
                .as("catalog_documents.deleted_at column must exist (P1.2 adds `deleted_at timestamptz NULL`)")
                .isNotNull();
            assertThat(rs.dataType())
                .as("catalog_documents.deleted_at must be 'timestamp with time zone'")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.isNullable())
                .as("catalog_documents.deleted_at must be nullable (tombstone = set, live = NULL)")
                .isEqualTo("YES");
        }
    }

    @Test @Order(11)
    void catalogLinks_deletedAt_isTimestamptzNullable() throws Exception {
        // RED until P1.2 adds `deleted_at timestamptz NULL` to catalog_links.
        try (Connection su = pg.createConnection("")) {
            PgCatalogProbes.ColumnInfo rs = PgCatalogProbes.columnInfo(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "catalog_links", "deleted_at");
            assertThat(rs)
                .as("catalog_links.deleted_at column must exist (P1.2 adds `deleted_at timestamptz NULL`)")
                .isNotNull();
            assertThat(rs.dataType())
                .as("catalog_links.deleted_at must be 'timestamp with time zone'")
                .isEqualTo("timestamp with time zone");
            assertThat(rs.isNullable())
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, tumbler);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "knowledge__sd-tomb__v1");

            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires a
            // matching nexus.chunks row for every manifest insert below.
            insertChunk384(ctx, TENANT_A, "knowledge__sd-tomb__v1", chashBytes("tomb-chunk-0"), "tomb chunk 0");
            insertChunk384(ctx, TENANT_A, "knowledge__sd-tomb__v1", chashBytes("tomb-chunk-1"), "tomb chunk 1");

            // 2 manifest rows — post-P0: needs 32-char chash
            insertManifestRow(ctx, TENANT_A, tumbler, 0, chashBytes("tomb-chunk-0"), "knowledge__sd-tomb__v1");
            insertManifestRow(ctx, TENANT_A, tumbler, 1, chashBytes("tomb-chunk-1"), "knowledge__sd-tomb__v1");

            // 1 document_aspects row (fk-001 ON DELETE CASCADE target)
            insertAspectRow(ctx, TENANT_A, tumbler, "knowledge__sd-asp__v1", "sd-aspect-path-1");
        }

        // CONTROL: confirm fixture rows present
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(countManifest(ctx, TENANT_A, tumbler))
                .as("CONTROL: 2 manifest rows must be present before tombstone")
                .isEqualTo(2);
            assertThat(countAspects(ctx, TENANT_A, tumbler))
                .as("CONTROL: 1 aspect row must be present before tombstone")
                .isEqualTo(1);
        }

        // Call document_trash via svc role with GUC set.
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumbler);
        }

        // Post-tombstone assertions (green after P1.2 lands):
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // deleted_at must be set
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .fetchOptional();
            assertThat(row.isPresent()).as("document row must still exist after tombstone").isTrue();
            assertThat(row.get().value1())
                .as("deleted_at must be set (not NULL) after document_trash")
                .isNotNull();

            // CASCADE chains must NOT have fired — children stay intact
            assertThat(countManifest(ctx, TENANT_A, tumbler))
                .as("manifest count must be == 2 after tombstone (CASCADE did not fire)")
                .isEqualTo(2);
            assertThat(countAspects(ctx, TENANT_A, tumbler))
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
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, tumbler);
        }

        // Tombstone first (also triggers RED if trash absent — acceptable; restore test
        // is the primary target; both are RED for the same reason: function absent).
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumbler);
        }

        // Now restore
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentRestore(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumbler);
        }

        // After restore: deleted_at IS NULL; document appears in live-path SELECT
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .fetchOptional();
            assertThat(row.isPresent()).as("document row must still exist after restore").isTrue();
            assertThat(row.get().value1())
                .as("deleted_at must be NULL after document_restore (document is live again)")
                .isNull();

            // Live-path query: SELECT WHERE deleted_at IS NULL
            int liveCount = ctx.selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A))
                .and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .fetchOne(0, int.class);
            assertThat(liveCount)
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
        byte[] chashA      = chashBytes("purge-only-a");     // referenced by A only
        byte[] chashShared = chashBytes("purge-shared");     // referenced by A and B

        // Fixture setup via superuser (bypasses FORCE RLS + fk-002 check needs su for collection)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            // Register collection so fk-002 NOT VALID FK is satisfied for new inserts
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION_A);

            // Insert the actual chunk rows into nexus.chunks (embedding_384) FIRST —
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires a
            // matching nexus.chunks row before any manifest row can reference it.
            insertChunk384(ctx, TENANT_A, COLLECTION_A, chashA,      "text for chunk A only");
            insertChunk384(ctx, TENANT_A, COLLECTION_A, chashShared, "shared chunk text");

            // Doc A (will be tombstoned)
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, tumblerA);
            insertManifestRow(ctx, TENANT_A, tumblerA, 0, chashA, COLLECTION_A);
            insertManifestRow(ctx, TENANT_A, tumblerA, 1, chashShared, COLLECTION_A);

            // Doc B (live — keeps chash_shared alive)
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, tumblerB);
            insertManifestRow(ctx, TENANT_A, tumblerB, 0, chashShared, COLLECTION_A);
        }

        // CONTROL: verify fixture
        try (Connection su = pg.createConnection("")) {
            assertThat(countChunks384(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, COLLECTION_A))
                .as("CONTROL: 2 chunk rows must exist before tombstone+purge")
                .isEqualTo(2);
        }

        // Tombstone doc A via svc role
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumblerA);
        }

        // Purge (older_than = 0 seconds: tombstone is always older than "now - 0s")
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.purgeTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(),
                YearToSecond.valueOf(Duration.ofSeconds(0)));
        }

        // Post-purge assertions
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Doc A must be gone (physically deleted by purge)
            int countA = ctx.selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumblerA))
                .fetchOne(0, int.class);
            assertThat(countA)
                .as("doc A must be physically deleted by purge_trash")
                .isEqualTo(0);

            // Doc B must still be live
            int countB = ctx.selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumblerB))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .fetchOne(0, int.class);
            assertThat(countB)
                .as("doc B must still be live after purge")
                .isEqualTo(1);

            // Only 1 chunk row must survive: chash_shared (referenced by live doc B)
            assertThat(countChunks384(ctx, TENANT_A, COLLECTION_A))
                .as("chunk count must be == 1 after purge: chash_shared survives (live doc B), chash_A swept")
                .isEqualTo(1);

            // The surviving chunk must be chash_shared
            byte[] survivingChash = ctx.select(CHUNKS.CHASH).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.COLLECTION.eq(COLLECTION_A))
                .fetchOne(CHUNKS.CHASH);
            assertThat(survivingChash)
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Confirm GUC is not set (returns '' or null for missing)
            String guc = ctx.select(DSL.function("current_setting", String.class,
                    DSL.val("nexus.tenant"), DSL.val(true)))
                .fetchOne(0, String.class);
            assertThat(guc == null || guc.isEmpty())
                .as("CONTROL: nexus.tenant GUC must be unset on fresh superuser connection")
                .isTrue();

            // Call purge_trash — must RAISE with a message mentioning "tenant" (the GUC guard).
            DataAccessException ex = assertThrows(DataAccessException.class, () ->
                Routines.purgeTrash(ctx.configuration(), YearToSecond.valueOf(Duration.ofHours(1)))
            );
            assertThat(ex.getMessage().toLowerCase())
                .as("purge_trash must raise an error mentioning 'tenant' when GUC is unset " +
                    "(Decision 6: cross-tenant purge must be impossible via unscoped call).")
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, tumbler);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "knowledge__sd-age__v1");
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires a
            // matching nexus.chunks row before the manifest insert below.
            insertChunk384(ctx, TENANT_A, "knowledge__sd-age__v1", chashBytes("age-filter-chunk0"), "age filter chunk 0");
            insertManifestRow(ctx, TENANT_A, tumbler, 0, chashBytes("age-filter-chunk0"), "knowledge__sd-age__v1");
            insertAspectRow(ctx, TENANT_A, tumbler, "knowledge__sd-age__v1", "sd-age-asp-path-1");
        }

        // Tombstone the document (just now — it will be "new")
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumbler);
        }

        // Purge with a very long older_than (e.g. 30 days) — the recent tombstone must NOT be purged
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.purgeTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(),
                YearToSecond.valueOf(Duration.ofDays(30)));
        }

        // Document must still exist with deleted_at set (tombstoned but not purged)
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .fetchOptional();
            assertThat(row.isPresent())
                .as("tombstoned-but-new doc must still exist (age filter: 30 days, tombstone just set)")
                .isTrue();
            assertThat(row.get().value1())
                .as("deleted_at must still be set (doc tombstoned, not purged by age filter)")
                .isNotNull();

            // Children must also still be intact (cascade did not fire — doc not yet purged)
            assertThat(countManifest(ctx, TENANT_A, tumbler))
                .as("manifest count must be == 1 (doc not purged — age filter held)")
                .isEqualTo(1);
            assertThat(countAspects(ctx, TENANT_A, tumbler))
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
        byte[] chashOrphan = chashBytes("lc-orphan-chunk");  // only in tombstoned doc X
        byte[] chashLive   = chashBytes("lc-live-chunk");    // in both X (tombstoned) and Y (live)

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION_B);

            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires a
            // matching nexus.chunks row before any manifest row can reference it.
            insertChunk384(ctx, TENANT_A, COLLECTION_B, chashOrphan, "orphan chunk text");
            insertChunk384(ctx, TENANT_A, COLLECTION_B, chashLive,   "live shared chunk text");

            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, tumblerX);
            insertManifestRow(ctx, TENANT_A, tumblerX, 0, chashOrphan, COLLECTION_B);
            insertManifestRow(ctx, TENANT_A, tumblerX, 1, chashLive, COLLECTION_B);

            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, tumblerY);
            insertManifestRow(ctx, TENANT_A, tumblerY, 0, chashLive, COLLECTION_B);
        }

        // CONTROL: both chunks present before tombstone
        try (Connection su = pg.createConnection("")) {
            assertThat(countChunks384(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, COLLECTION_B))
                .as("CONTROL: 2 chunk rows must be present before tombstone")
                .isEqualTo(2);
        }

        // Tombstone doc X
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumblerX);
        }

        // (a) Orphan chunk (only X references it; X is tombstoned) must be ABSENT from live_chunks
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            int count = DSL.using(svc, SQLDialect.POSTGRES).selectCount().from(LIVE_CHUNKS)
                .where(LIVE_CHUNKS.CHASH.eq(chashOrphan))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("orphan chunk (only tombstoned doc X references it) must be ABSENT from live_chunks")
                .isEqualTo(0);
        }

        // (b) Shared chunk (live doc Y still references it) must be PRESENT in live_chunks
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            int count = DSL.using(svc, SQLDialect.POSTGRES).selectCount().from(LIVE_CHUNKS)
                .where(LIVE_CHUNKS.CHASH.eq(chashLive))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("shared chunk (live doc Y references it) must be PRESENT in live_chunks")
                .isEqualTo(1);
        }

        // (c) live_chunks must NOT expose a deleted_at column (consumers never see it)
        try (Connection su = pg.createConnection("")) {
            assertThat(PgCatalogProbes.columnExists(
                    DSL.using(su, SQLDialect.POSTGRES), "nexus", "live_chunks", "deleted_at"))
                .as("live_chunks view must NOT expose a deleted_at column " +
                    "(Decision 6: single enforcement point — consumers never see it)")
                .isFalse();
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
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), TENANT_B, tumblerTenantB);
        }

        // As TENANT_A (svc role, GUC=A): call document_trash targeting TENANT_B's tumbler.
        // Pinned contract: 0 rows affected (FORCE RLS silently filters the UPDATE).
        // Function MUST NOT raise an error — it executes and affects nothing.
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            // This call should succeed (no exception) but affect 0 rows.
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumblerTenantB);
        }

        // Verify: TENANT_B's document is NOT tombstoned (deleted_at IS NULL)
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_B)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumblerTenantB))
                .fetchOptional();
            assertThat(row.isPresent())
                .as("TENANT_B's document must still exist after cross-tenant trash attempt")
                .isTrue();
            assertThat(row.get().value1())
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
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), TENANT_B, tumblerTenantB);
        }

        // Attempt restore on TENANT_B's doc via GUC=A — must silently affect 0 rows
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentRestore(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumblerTenantB);
        }

        // Verify: TENANT_B's document is unaffected (deleted_at still NULL)
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_B)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumblerTenantB))
                .fetchOptional();
            assertThat(row.isPresent())
                .as("TENANT_B's document must still exist after cross-tenant restore attempt")
                .isTrue();
            assertThat(row.get().value1())
                .as("TENANT_B's document.deleted_at must remain NULL after cross-tenant restore attempt")
                .isNull();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — Manifest-less chunk safety (CRITICAL-1: CRE finding, 2026-06-11)
    //
    // MCP store_put / nx store put writes chunks WITHOUT writing manifest rows
    // (catalog_store_hook registers catalog_documents but does NOT insert
    // catalog_document_chunks). These "manifest-less" chunks MUST NOT be swept
    // as orphans by purge_trash AND must appear in live_chunks.
    //
    // Manifest-less-is-live contract (until RDR-145):
    //   purge_trash may sweep a chunk ONLY IF EXISTS(manifest row) AND NOT EXISTS
    //   (live manifest row). A chunk with NO manifest rows must survive.
    //   live_chunks: visible if NOT EXISTS(manifest) OR EXISTS(live manifest).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void purgeTrash_manifestlessChunk_survives() throws Exception {
        // Arrange: insert a chunk_384 row with NO catalog_document_chunks manifest row.
        // This simulates an MCP store_put / nx store put note — no associated catalog doc.
        byte[] manifestlessChash = chashBytes("manifestless9090");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION_A);
            insertChunk384(ctx, TENANT_A, COLLECTION_A, manifestlessChash, "manifest-less note chunk");
        }

        // Verify the chunk exists and has NO manifest rows (precondition)
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            int chunkCount = ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.CHASH.eq(manifestlessChash))
                .fetchOne(0, int.class);
            assertThat(chunkCount)
                .as("precondition: manifest-less chunk must exist before purge")
                .isEqualTo(1);

            int manifestCount = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(manifestlessChash))
                .fetchOne(0, int.class);
            assertThat(manifestCount)
                .as("precondition: no manifest rows for this chash")
                .isEqualTo(0);
        }

        // Act: purge_trash with 0-second interval (would sweep anything eligible)
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.purgeTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(),
                YearToSecond.valueOf(Duration.ofSeconds(0)));
        }

        // Assert: the manifest-less chunk MUST still exist (exact == 1)
        try (Connection su = pg.createConnection("")) {
            int chunkCount = DSL.using(su, SQLDialect.POSTGRES).selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT_A)).and(CHUNKS.CHASH.eq(manifestlessChash))
                .fetchOne(0, int.class);
            assertThat(chunkCount)
                .as("manifest-less chunk must survive purge_trash " +
                    "(no manifest rows → not eligible for orphan sweep)")
                .isEqualTo(1);
        }
    }

    @Test @Order(91)
    void liveChunks_includesManifestlessChunk() throws Exception {
        // Arrange: insert a fresh manifest-less chunk (no catalog_document_chunks row).
        byte[] manifestlessChash = chashBytes("manifestless9191");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION_A);
            insertChunk384(ctx, TENANT_A, COLLECTION_A, manifestlessChash, "manifest-less live_chunks note");
        }

        // Assert: the chunk appears in live_chunks (NOT EXISTS(manifest) → visible)
        // The svc role reads via GUC-scoped RLS; live_chunks is SECURITY INVOKER.
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            int count = DSL.using(svc, SQLDialect.POSTGRES).selectCount().from(LIVE_CHUNKS)
                .where(LIVE_CHUNKS.TENANT_ID.eq(TENANT_A)).and(LIVE_CHUNKS.CHASH.eq(manifestlessChash))
                .fetchOne(0, int.class);
            assertThat(count)
                .as("manifest-less chunk must appear in live_chunks " +
                    "(NOT EXISTS(manifest row) → always live)")
                .isEqualTo(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 10 — Double-trash idempotency (SIG finding, 2026-06-11)
    //
    // document_trash must have AND deleted_at IS NULL so a second call does NOT
    // reset the deleted_at timestamp. Without the guard the purge age clock
    // would restart on every re-trash call.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(92)
    void doubleTrash_doesNotResetDeletedAt() throws Exception {
        final String tumbler = "sd-owner-a.92";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, tumbler);
        }

        // First trash — sets deleted_at
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumbler);
        }

        // Capture the timestamp after the FIRST trash call
        OffsetDateTime firstDeletedAt;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .fetchOptional();
            assertThat(row.isPresent()).as("document must exist after first trash").isTrue();
            firstDeletedAt = row.get().value1();
            assertThat(firstDeletedAt)
                .as("deleted_at must be non-null after first trash")
                .isNotNull();
        }

        // Second trash — must NOT change deleted_at (AND deleted_at IS NULL guard)
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            Routines.documentTrash(DSL.using(svc, SQLDialect.POSTGRES).configuration(), tumbler);
        }

        // Assert: deleted_at timestamp unchanged after second call (exact same value)
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var row = ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .fetchOptional();
            assertThat(row.isPresent()).as("document must still exist after second trash").isTrue();
            assertThat(row.get().value1())
                .as("deleted_at must not be reset by a second document_trash call " +
                    "(AND deleted_at IS NULL guard must prevent clock reset)")
                .isEqualTo(firstDeletedAt);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 11 — registerDocument / updateDocument tombstone contracts (HIGH)
    //
    // registerDocument must treat a tombstoned source_uri as expired — the
    // idempotency check filters deleted_at IS NULL so a re-registration
    // allocates a NEW tumbler rather than returning the old tombstoned one.
    //
    // updateDocument must refuse to update a tombstoned doc (returns 0).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(93)
    void registerDocument_tombstonedSourceUri_allocatesNewTumbler() throws Exception {
        // Build a CatalogRepository backed by the superuser DataSource so the owner
        // upsert inside registerDocument succeeds. The tombstone logic under test lives
        // in the WHERE clause of the idempotency SELECT (deleted_at IS NULL filter) —
        // this is application-layer logic, not an RLS contract; superuser is appropriate.
        try (var suDs = PgContainerHelper.superuserDataSource(pg)) {
            var repo = new dev.nexus.service.db.CatalogRepository(
                new dev.nexus.service.db.TenantScope(suDs));

            final String ownerPrefix  = "sd-owner-reg93";
            final String srcUri       = "file:///tmp/rdr156-p1-reg93-test.md";

            // Step 1: register a document — gets tumbler sd-owner-reg93.1
            String firstTumbler = repo.registerDocument(TENANT_A, ownerPrefix, java.util.Map.of(
                "title",      "Reg93 Doc",
                "source_uri", srcUri,
                "content_type", "rdr",
                "corpus",     "rdr"
            ));
            assertThat(firstTumbler)
                .as("first registration must succeed and return a tumbler")
                .isNotNull()
                .startsWith(ownerPrefix + ".");

            // Step 2: verify idempotency — same source_uri returns same tumbler (LIVE doc)
            String idempotentTumbler = repo.registerDocument(TENANT_A, ownerPrefix, java.util.Map.of(
                "title",      "Reg93 Doc (repeat)",
                "source_uri", srcUri,
                "content_type", "rdr",
                "corpus",     "rdr"
            ));
            assertThat(idempotentTumbler)
                .as("re-registration of a live source_uri must return the SAME existing tumbler")
                .isEqualTo(firstTumbler);

            // Step 3: tombstone the document via Java deleteDocument (uses DSL.currentOffsetDateTime())
            int tombstoned = repo.deleteDocument(TENANT_A, firstTumbler);
            assertThat(tombstoned)
                .as("deleteDocument must affect exactly 1 row")
                .isEqualTo(1);

            // Step 4: re-register same source_uri — tombstone is NOT live, must allocate NEW tumbler
            String newTumbler = repo.registerDocument(TENANT_A, ownerPrefix, java.util.Map.of(
                "title",      "Reg93 Doc (re-registered after tombstone)",
                "source_uri", srcUri,
                "content_type", "rdr",
                "corpus",     "rdr"
            ));
            assertThat(newTumbler)
                .as("re-registration after tombstone must allocate a NEW tumbler, " +
                    "not return the tombstoned one (idempotency check filters deleted_at IS NULL)")
                .isNotEqualTo(firstTumbler);
            assertThat(newTumbler)
                .as("new tumbler must still be under the same owner prefix")
                .startsWith(ownerPrefix + ".");
        }
    }

    @Test @Order(94)
    void updateDocument_tombstonedDoc_returnsZero() throws Exception {
        try (var suDs = PgContainerHelper.superuserDataSource(pg)) {
            var repo = new dev.nexus.service.db.CatalogRepository(
                new dev.nexus.service.db.TenantScope(suDs));

            final String ownerPrefix = "sd-owner-upd94";
            final String srcUri      = "file:///tmp/rdr156-p1-upd94-test.md";

            // Register a document
            String tumbler = repo.registerDocument(TENANT_A, ownerPrefix, java.util.Map.of(
                "title",      "Upd94 Doc",
                "source_uri", srcUri,
                "content_type", "rdr",
                "corpus",     "rdr"
            ));

            // Verify updateDocument works on a live doc (returns 1)
            int liveUpdate = repo.updateDocument(TENANT_A, tumbler, java.util.Map.of("title", "Upd94 Updated"));
            assertThat(liveUpdate)
                .as("updateDocument on a live doc must return 1")
                .isEqualTo(1);

            // Tombstone the document
            int tombstoned = repo.deleteDocument(TENANT_A, tumbler);
            assertThat(tombstoned).as("deleteDocument must return 1").isEqualTo(1);

            // Act: updateDocument on tombstoned doc
            int deadUpdate = repo.updateDocument(TENANT_A, tumbler, java.util.Map.of("title", "Should Not Apply"));
            assertThat(deadUpdate)
                .as("updateDocument on a tombstoned doc must return 0 (AND deleted_at IS NULL guard)")
                .isEqualTo(0);

            // Verify the title was NOT changed (tombstone is intact; title is pre-tombstone value)
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                var row = ctx.select(CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                    .fetchOptional();
                assertThat(row.isPresent()).as("tombstoned doc row must still exist").isTrue();
                assertThat(row.get().value1())
                    .as("title must remain at pre-tombstone value; update on dead doc must not apply")
                    .isEqualTo("Upd94 Updated");
                assertThat(row.get().value2())
                    .as("deleted_at must remain non-null (doc is still tombstoned)")
                    .isNotNull();
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Insert a catalog_document_chunks manifest row.
     * PK: (tenant_id, doc_id, position). Idempotent via ON CONFLICT DO NOTHING.
     * doc_id is the tumbler of the parent catalog_documents row (fk-001 FK).
     */
    private static void insertManifestRow(DSLContext ctx, String tenantId, String docId,
                                           int position, byte[] chash, String collection) {
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(tenantId, docId, position, chash, collection)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Insert a document_aspects row referencing a catalog_documents tumbler.
     * doc_id must match an existing catalog_documents(tenant_id, tumbler) row (fk-001 FK ON DELETE CASCADE).
     * Unique on (tenant_id, collection, source_path).
     */
    private static void insertAspectRow(DSLContext ctx, String tenantId, String tumbler,
                                         String collection, String sourcePath) {
        // RDR-164 P1a: register the collection (document_aspects_collection_fk).
        PgContainerHelper.insertCollection(ctx, tenantId, collection);
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): source_uri is NOT NULL now too.
        ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
            .values(tenantId, collection, sourcePath, OffsetDateTime.now(), "v1", "docling", tumbler,
                "file:///" + sourcePath)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Insert a nexus.chunks row with embedding_384 populated (RDR-191 unified;
     * formerly a chunks_384 row). Collection must be pre-registered (fk-002 NOT VALID
     * FK). PK: (tenant_id, collection, chash). Superuser insert bypasses FORCE RLS
     * so direct fixture setup is possible.
     */
    private static void insertChunk384(DSLContext ctx, String tenantId, String collection,
                                        byte[] chash, String chunkText) {
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
            .values(tenantId, collection, chash, chunkText, vector(384))
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Count catalog_document_chunks rows for (tenantId, docId).
     */
    private static int countManifest(DSLContext ctx, String tenantId, String docId) {
        return ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenantId)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
            .fetchOne(0, int.class);
    }

    /**
     * Count document_aspects rows for (tenantId, docId).
     */
    private static int countAspects(DSLContext ctx, String tenantId, String tumbler) {
        return ctx.selectCount().from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenantId)).and(DOCUMENT_ASPECTS.DOC_ID.eq(tumbler))
            .fetchOne(0, int.class);
    }

    /**
     * Count nexus.chunks rows with embedding_384 populated for (tenantId, collection).
     */
    private static int countChunks384(DSLContext ctx, String tenantId, String collection) {
        return ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenantId)).and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.EMBEDDING_384.isNotNull())
            .fetchOne(0, int.class);
    }

    /** A pgvector value with every one of {@code dim} components equal to {@code 0.1}. */
    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    /**
     * Full 64-lowercase-hex chash deterministically derived from a seed (RDR-180:
     * chunks_&lt;dim&gt;/manifest columns are bytea(32) now, CHECK octet_length=32 —
     * the pre-flip 32-char TEXT scheme is retired).
     */
    private static String validChash(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    /** {@link #validChash}'s genuine hex-decoded bytes -- matches the pre-conversion
     *  raw SQL's {@code decode(chash, 'hex')} calls exactly (as opposed to storing the
     *  hex STRING's own ASCII bytes via bytea escape-format input). */
    private static byte[] chashBytes(String seed) {
        return HexFormat.of().parseHex(validChash(seed));
    }
}
