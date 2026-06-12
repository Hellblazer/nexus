// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 bead nexus-70r3c.11 — TDD-RED suite for P3 {@code nexus.collection_vector_stats}
 * (Decision 4).
 *
 * <p><strong>Scope (Decision 4):</strong> one view, under the RDR-154 {@code security_invoker}
 * standing rule:
 * <ul>
 *   <li>{@code nexus.collection_vector_stats} — per-collection chunk count, dim, and last
 *       write timestamp, aggregated across {@code chunks_384/768/1024}. Replaces the remote
 *       per-collection {@code count(*)} calls in doctor/status surfaces and the migration
 *       runbook's hand-psql counts.</li>
 * </ul>
 *
 * <p><strong>Pinned contract (catalog-005 must honor):</strong>
 * <ul>
 *   <li>Columns: exactly {@code (tenant_id, collection, dim, chunk_count, last_write)}.
 *       No {@code deleted_at}, no embedding payload — consumers never see tombstone
 *       mechanics (Decision 6 single enforcement point).</li>
 *   <li>Grain: one row per {@code (tenant_id, collection, dim)}. {@code chunk_count} is
 *       {@code bigint}, {@code last_write} is {@code max(created_at)} over LIVE chunks.</li>
 *   <li>TOMBSTONE-FILTERED: built on {@code nexus.live_chunks} semantics — a chunk whose
 *       only manifest rows point to tombstoned documents is NOT counted; manifest-less
 *       chunks (MCP note chunks) ARE counted (live by contract, SoftDeleteTest 90/91).</li>
 *   <li>{@code security_invoker = true} actually set on the view (pg_class reloptions),
 *       not just claimed — caller RLS provides tenant isolation.</li>
 *   <li>A collection with zero (live) chunk rows does NOT appear — the view is
 *       chunk-driven, matching the existing DISTINCT-based listCollections semantics.</li>
 * </ul>
 *
 * <p><strong>Non-vacuity guarantee:</strong> tombstone tests assert BOTH directions
 * (count drops on trash, returns on restore); RLS tests assert both GUC=A and GUC=B
 * views plus a superuser CONTROL proving foreign rows exist underneath.
 *
 * <p><strong>Expected RED/GREEN before catalog-005 lands:</strong>
 * <ul>
 *   <li>GROUP 1 (aggregate correctness + zero-chunk absence): RED — view absent</li>
 *   <li>GROUP 2 (column shape pinned): RED — view absent</li>
 *   <li>GROUP 3 (security_invoker reloption actually set): RED — view absent</li>
 *   <li>GROUP 4 (tombstone filter, both directions): RED — view absent</li>
 *   <li>GROUP 5 (cross-tenant RLS via svc role + GUC): RED — view absent</li>
 *   <li>GROUP 6 (parity with the raw count it replaces, and documented divergence
 *       under tombstones): RED — view absent</li>
 *   <li>All CONTROL paths (fixture inserts, raw-table counts): GREEN always</li>
 * </ul>
 *
 * <p>Mirror conventions from ManifestFunctionsTest / SoftDeleteTest: PgContainerHelper,
 * Liquibase master, PER_CLASS, {@link Order}, superuser fixtures, svc-role + GUC for RLS
 * tests, 32-char chashes, registered collections per fk-002, exact assertions
 * ({@code isEqualTo}, never {@code isGreaterThanOrEqualTo}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionVectorStatsTest {

    // ── Tenant IDs ────────────────────────────────────────────────────────────
    private static final String TENANT_A = "cvs-tenant-a";
    private static final String TENANT_B = "cvs-tenant-b";

    // ── Svc role (NOSUPERUSER, NOBYPASSRLS — subject to FORCE RLS) ───────────
    private static final String SVC_ROLE = "svc_cvs_test";
    private static final String SVC_PASS = "svc_cvs_test_pass";

    /**
     * Per-collection vector statistics view.
     * {@code nexus.collection_vector_stats(tenant_id, collection, dim, chunk_count, last_write)}.
     * SECURITY INVOKER; tombstone-filtered via live_chunks semantics.
     */
    private static final String VIEW_STATS = "nexus.collection_vector_stats";

    // ── Test collections (registered in catalog_collections per fk-002) ──────
    // Conformant shape <type>__<owner>__<model>__<version>; model token routes dim.
    private static final String COLL_A_384   = "knowledge__cvs-owner-a__minilm-l6-v2-384__v1";
    private static final String COLL_A_1024  = "knowledge__cvs-owner-a__voyage-context-3__v1";
    private static final String COLL_A_EMPTY = "knowledge__cvs-owner-a-empty__minilm-l6-v2-384__v1";
    private static final String COLL_A_TOMB  = "knowledge__cvs-owner-tomb__minilm-l6-v2-384__v1";
    private static final String COLL_B_384   = "knowledge__cvs-owner-b__minilm-l6-v2-384__v1";

    // ── Deterministic write timestamps (fixed clock — no now() in fixtures) ──
    private static final String T1 = "2026-01-01T00:00:01+00";
    private static final String T2 = "2026-01-01T00:00:02+00";
    private static final String T3 = "2026-01-01T00:00:03+00";
    private static final String T4 = "2026-02-01T00:00:04+00";
    private static final String T5 = "2026-02-01T00:00:05+00";

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
                    "catalog_document_chunks",
                    "chunks_384",
                    "chunks_768",
                    "chunks_1024")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
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
    // GROUP 1 — aggregate correctness (exact counts, dim, max(created_at))
    //
    // EXPECTED RED: nexus.collection_vector_stats view absent until catalog-005.
    //
    // Fixture (all manifest-less — live by contract, also pins manifest-less
    // inclusion in stats):
    //   COLL_A_384  : 3 chunks_384 rows, created_at T1 < T2 < T3
    //   COLL_A_1024 : 2 chunks_1024 rows, created_at T4 < T5
    //   COLL_A_EMPTY: registered in catalog_collections, ZERO chunk rows
    //
    // Assert (exact):
    //   (A, COLL_A_384)  → dim=384,  chunk_count=3, last_write=T3
    //   (A, COLL_A_1024) → dim=1024, chunk_count=2, last_write=T5
    //   COLL_A_EMPTY     → ABSENT (view is chunk-driven; zero-chunk collections
    //                      do not appear, matching DISTINCT-based listCollections)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void stats_aggregates_countDimLastWrite_exact() throws Exception {
        // RED until catalog-005 adds nexus.collection_vector_stats.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLL_A_384);
            insertCollection(su, TENANT_A, COLL_A_1024);
            insertCollection(su, TENANT_A, COLL_A_EMPTY);
            insertChunk(su, 384, TENANT_A, COLL_A_384, validChash("cvs-a384-c1"), "a384 one",   T1);
            insertChunk(su, 384, TENANT_A, COLL_A_384, validChash("cvs-a384-c2"), "a384 two",   T2);
            insertChunk(su, 384, TENANT_A, COLL_A_384, validChash("cvs-a384-c3"), "a384 three", T3);
            insertChunk(su, 1024, TENANT_A, COLL_A_1024, validChash("cvs-a1024-c1"), "a1024 one", T4);
            insertChunk(su, 1024, TENANT_A, COLL_A_1024, validChash("cvs-a1024-c2"), "a1024 two", T5);
        }

        // CONTROL: raw tables hold exactly what we inserted
        try (Connection su = pg.createConnection("")) {
            assertThat(rawChunkCount(su, 384, TENANT_A, COLL_A_384))
                .as("CONTROL: 3 chunks_384 rows must exist").isEqualTo(3);
            assertThat(rawChunkCount(su, 1024, TENANT_A, COLL_A_1024))
                .as("CONTROL: 2 chunks_1024 rows must exist").isEqualTo(2);
        }

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT dim, chunk_count, last_write FROM " + VIEW_STATS +
                " WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + COLL_A_384 + "'");
            assertThat(rs.next())
                .as("stats row for COLL_A_384 must exist").isTrue();
            assertThat(rs.getInt("dim"))
                .as("COLL_A_384 dim must be exactly 384").isEqualTo(384);
            assertThat(rs.getLong("chunk_count"))
                .as("COLL_A_384 chunk_count must be exactly 3").isEqualTo(3L);
            assertThat(rs.getObject("last_write", java.time.OffsetDateTime.class).toInstant())
                .as("COLL_A_384 last_write must be exactly max(created_at) = T3")
                .isEqualTo(java.time.OffsetDateTime.parse("2026-01-01T00:00:03+00:00").toInstant());
            assertThat(rs.next())
                .as("exactly ONE stats row per (tenant, collection, dim)").isFalse();
        }

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT dim, chunk_count, last_write FROM " + VIEW_STATS +
                " WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + COLL_A_1024 + "'");
            assertThat(rs.next())
                .as("stats row for COLL_A_1024 must exist").isTrue();
            assertThat(rs.getInt("dim"))
                .as("COLL_A_1024 dim must be exactly 1024").isEqualTo(1024);
            assertThat(rs.getLong("chunk_count"))
                .as("COLL_A_1024 chunk_count must be exactly 2").isEqualTo(2L);
            assertThat(rs.getObject("last_write", java.time.OffsetDateTime.class).toInstant())
                .as("COLL_A_1024 last_write must be exactly max(created_at) = T5")
                .isEqualTo(java.time.OffsetDateTime.parse("2026-02-01T00:00:05+00:00").toInstant());
            assertThat(rs.next())
                .as("exactly ONE stats row per (tenant, collection, dim)").isFalse();
        }

        // Zero-chunk registered collection must be ABSENT
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM " + VIEW_STATS +
                " WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + COLL_A_EMPTY + "'");
            rs.next();
            assertThat(rs.getLong(1))
                .as("registered-but-empty collection must NOT appear in stats " +
                    "(chunk-driven view, matching DISTINCT-based listCollections)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — column shape pinned
    //
    // EXPECTED RED: view absent until catalog-005.
    //
    // The view exposes EXACTLY (tenant_id, collection, dim, chunk_count, last_write).
    // No deleted_at (Decision 6: consumers never see tombstone mechanics), no
    // embedding/chunk_text payload (stats are cheap metadata, not data plane).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void stats_columnShape_pinnedExactly() throws Exception {
        // RED until catalog-005 adds nexus.collection_vector_stats.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT column_name FROM information_schema.columns " +
                " WHERE table_schema = 'nexus' AND table_name = 'collection_vector_stats' " +
                " ORDER BY ordinal_position");
            List<String> cols = new ArrayList<>();
            while (rs.next()) cols.add(rs.getString(1));
            assertThat(cols)
                .as("collection_vector_stats must expose EXACTLY " +
                    "(tenant_id, collection, dim, chunk_count, last_write) in order — " +
                    "no deleted_at, no data-plane payload")
                .containsExactly("tenant_id", "collection", "dim", "chunk_count", "last_write");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — security_invoker ACTUALLY set (not just claimed)
    //
    // EXPECTED RED: view absent until catalog-005.
    //
    // RDR-154 standing rule: every view ships WITH (security_invoker = true) so the
    // CALLER's RLS applies, never the definer's. Assert the reloption is physically
    // present in pg_class — a comment claiming it does not count.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void stats_securityInvoker_reloptionActuallySet() throws Exception {
        // RED until catalog-005 adds nexus.collection_vector_stats.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT reloptions FROM pg_class " +
                " WHERE oid = 'nexus.collection_vector_stats'::regclass");
            assertThat(rs.next())
                .as("collection_vector_stats must exist in pg_class").isTrue();
            java.sql.Array arr = rs.getArray(1);
            assertThat(arr).as("view must HAVE reloptions (security_invoker)").isNotNull();
            String[] opts = (String[]) arr.getArray();
            assertThat(opts)
                .as("security_invoker=true must be PHYSICALLY set on the view " +
                    "(RDR-154 standing rule — caller RLS, not definer)")
                .contains("security_invoker=true");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — tombstone filter (both directions, non-vacuous)
    //
    // EXPECTED RED: view absent until catalog-005.
    //
    // Fixture: COLL_A_TOMB with 2 chunks:
    //   - chunk_doc : manifest row → doc cvs-tomb-doc-1 (live)
    //   - chunk_note: manifest-less (MCP note — live by contract)
    //
    // Direction 1: both live → chunk_count=2.
    // Direction 2: tombstone the doc → chunk_count drops to exactly 1
    //              (chunk_doc excluded; manifest-less note chunk STAYS).
    // Direction 3: restore the doc → chunk_count returns to exactly 2.
    // A vacuous / non-filtering implementation cannot pass all three.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void stats_tombstoneFilter_dropsAndReturns_bothDirections() throws Exception {
        // RED until catalog-005 adds nexus.collection_vector_stats.
        String docId      = "cvs-tomb-doc-1";
        String chashDoc   = validChash("cvs-tomb-doc-chunk");
        String chashNote  = validChash("cvs-tomb-note-chunk");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_A, COLL_A_TOMB);
            insertCatalogDocument(su, TENANT_A, docId, COLL_A_TOMB);
            insertManifestRow(su, TENANT_A, docId, 0, chashDoc, COLL_A_TOMB);
            insertChunk(su, 384, TENANT_A, COLL_A_TOMB, chashDoc, "doc-backed chunk", T1);
            insertChunk(su, 384, TENANT_A, COLL_A_TOMB, chashNote, "manifest-less note chunk", T2);
        }

        // Direction 1: both live
        assertThat(statsCount(TENANT_A, COLL_A_TOMB))
            .as("both chunks live → chunk_count must be exactly 2")
            .isEqualTo(2L);

        // Direction 2: tombstone the doc — doc-backed chunk excluded, note chunk stays
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = now() " +
                " WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docId + "'");
        }
        assertThat(statsCount(TENANT_A, COLL_A_TOMB))
            .as("doc tombstoned → chunk_count must drop to exactly 1 " +
                "(doc-backed chunk excluded; manifest-less note chunk is LIVE by contract)")
            .isEqualTo(1L);

        // Direction 3: restore — count returns
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NULL " +
                " WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docId + "'");
        }
        assertThat(statsCount(TENANT_A, COLL_A_TOMB))
            .as("doc restored → chunk_count must return to exactly 2")
            .isEqualTo(2L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — cross-tenant RLS isolation (svc role + GUC, non-vacuous)
    //
    // EXPECTED RED: view absent until catalog-005.
    //
    // Fixture: TENANT_B collection with 2 chunks (superuser-inserted).
    // CONTROL: superuser sees BOTH tenants' rows in the view (proves the view
    //          itself does not filter tenants — RLS does, via security_invoker).
    // svc + GUC=A: sees NO tenant-B rows; sees its own tenant-A rows.
    // svc + GUC=B: sees ONLY the tenant-B row, count exact.
    // svc + no GUC: sees 0 rows (tenant_isolation matches nothing on NULL).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void stats_rlsIsolation_tenantSeesOnlyOwnCollections() throws Exception {
        // RED until catalog-005 adds nexus.collection_vector_stats.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertCollection(su, TENANT_B, COLL_B_384);
            insertChunk(su, 384, TENANT_B, COLL_B_384, validChash("cvs-b384-c1"), "b one", T4);
            insertChunk(su, 384, TENANT_B, COLL_B_384, validChash("cvs-b384-c2"), "b two", T5);
        }

        // CONTROL: superuser (bypasses RLS) sees both tenants underneath the view
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(DISTINCT tenant_id) FROM " + VIEW_STATS +
                " WHERE tenant_id IN ('" + TENANT_A + "', '" + TENANT_B + "')");
            rs.next();
            assertThat(rs.getLong(1))
                .as("CONTROL: superuser must see BOTH tenants' stats rows " +
                    "(proves foreign rows exist; the RLS assertions below are non-vacuous)")
                .isEqualTo(2L);
        }

        // svc + GUC=A: zero tenant-B rows, but tenant-A rows visible
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet rsB = svc.createStatement().executeQuery(
                "SELECT count(*) FROM " + VIEW_STATS +
                " WHERE tenant_id = '" + TENANT_B + "'");
            rsB.next();
            assertThat(rsB.getLong(1))
                .as("GUC=A must see ZERO tenant-B stats rows (caller RLS via security_invoker)")
                .isEqualTo(0L);
            ResultSet rsA = svc.createStatement().executeQuery(
                "SELECT count(*) FROM " + VIEW_STATS +
                " WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + COLL_A_384 + "'");
            rsA.next();
            assertThat(rsA.getLong(1))
                .as("GUC=A must see its OWN tenant-A stats row for COLL_A_384")
                .isEqualTo(1L);
        }

        // svc + GUC=B: exactly the one tenant-B collection row, exact count
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_B + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT collection, chunk_count FROM " + VIEW_STATS);
            assertThat(rs.next())
                .as("GUC=B must see exactly one stats row").isTrue();
            assertThat(rs.getString("collection"))
                .as("GUC=B's single visible collection must be COLL_B_384")
                .isEqualTo(COLL_B_384);
            assertThat(rs.getLong("chunk_count"))
                .as("COLL_B_384 chunk_count must be exactly 2").isEqualTo(2L);
            assertThat(rs.next())
                .as("GUC=B must see NO further rows").isFalse();
        }

        // svc + no GUC: zero rows
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("RESET nexus.tenant");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT count(*) FROM " + VIEW_STATS);
            rs.next();
            assertThat(rs.getLong(1))
                .as("svc with NO nexus.tenant GUC must see 0 stats rows " +
                    "(tenant_isolation matches nothing on NULL)")
                .isEqualTo(0L);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — parity with the raw count the view replaces
    //
    // EXPECTED RED: view absent until catalog-005.
    //
    // The view replaces GET /v1/vectors/count's SQL
    // (SELECT count(*) FROM chunks_<dim> WHERE collection = ?) on doctor/status
    // surfaces. For a TOMBSTONE-FREE collection the two must agree exactly.
    // Under tombstones they deliberately DIVERGE (view counts live chunks only) —
    // assert the divergence too, so the semantic change is pinned, not accidental.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void stats_parityWithRawCount_tombstoneFree_andPinnedDivergence() throws Exception {
        // RED until catalog-005 adds nexus.collection_vector_stats.
        // COLL_A_384 (GROUP 1 fixture) is tombstone-free: view == raw count.
        try (Connection su = pg.createConnection("")) {
            long raw = rawChunkCount(su, 384, TENANT_A, COLL_A_384);
            assertThat(raw).as("CONTROL: raw count must be 3").isEqualTo(3L);
            assertThat(statsCount(TENANT_A, COLL_A_384))
                .as("tombstone-free collection: view chunk_count must EQUAL the raw " +
                    "count(*) it replaces (GET /v1/vectors/count parity)")
                .isEqualTo(raw);
        }

        // COLL_A_TOMB with its doc tombstoned: view < raw, by exactly 1.
        String docId = "cvs-tomb-doc-1";
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = now() " +
                " WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docId + "'");
            long raw = rawChunkCount(su, 384, TENANT_A, COLL_A_TOMB);
            assertThat(raw)
                .as("CONTROL: raw count still sees both chunks (chunks tables have no tombstone)")
                .isEqualTo(2L);
            assertThat(statsCount(TENANT_A, COLL_A_TOMB))
                .as("tombstoned doc: view must count exactly 1 (live note chunk only) — " +
                    "the live-vs-raw divergence is the POINT of the view, pinned here")
                .isEqualTo(1L);
        } finally {
            // restore for any later-ordered assertions
            try (Connection su = pg.createConnection("")) {
                su.createStatement().execute(
                    "UPDATE nexus.catalog_documents SET deleted_at = NULL " +
                    " WHERE tenant_id = '" + TENANT_A + "' AND tumbler = '" + docId + "'");
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /** chunk_count for (tenant, collection) via the stats view, superuser connection. */
    private long statsCount(String tenantId, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT coalesce(sum(chunk_count), 0) FROM " + VIEW_STATS +
                " WHERE tenant_id = '" + tenantId + "' AND collection = '" + collection + "'");
            rs.next();
            return rs.getLong(1);
        }
    }

    /** Raw count(*) over chunks_<dim> — the SQL GET /v1/vectors/count uses today. */
    private static long rawChunkCount(Connection conn, int dim, String tenantId,
                                      String collection) throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(
            "SELECT count(*) FROM nexus.chunks_" + dim +
            " WHERE tenant_id = '" + tenantId + "' AND collection = '" + collection + "'");
        rs.next();
        return rs.getLong(1);
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
     * Insert a minimal catalog_documents row with a physical_collection.
     * Idempotent via ON CONFLICT DO NOTHING.
     */
    private static void insertCatalogDocument(Connection su, String tenantId, String tumbler,
                                               String physicalCollection) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, physical_collection) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "', '" +
            physicalCollection + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    /**
     * Insert a catalog_document_chunks manifest row WITH a collection value.
     * chash MUST be exactly 32 characters (catalog-002-hygiene CHECK).
     */
    private static void insertManifestRow(Connection su, String tenantId, String docId,
                                          int position, String chash, String collection)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks " +
            "  (tenant_id, doc_id, position, chash, collection) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", '" + chash + "', '" +
            collection + "') " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    /**
     * Insert a chunks_<dim> row with an EXPLICIT created_at (deterministic fixtures —
     * last_write assertions are exact, never now()-relative). Collection must be
     * pre-registered (fk-002). Superuser insert bypasses FORCE RLS.
     */
    private static void insertChunk(Connection su, int dim, String tenantId, String collection,
                                    String chash, String chunkText, String createdAt)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim +
            " (tenant_id, collection, chash, chunk_text, embedding, created_at) " +
            "VALUES ('" + tenantId + "', '" + collection + "', '" + chash + "', " +
            "'" + chunkText.replace("'", "''") + "', " + vectorLiteral(dim) + "::vector, " +
            "'" + createdAt + "'::timestamptz) " +
            "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    /**
     * Generate a pgvector literal string of {@code dim} uniform 0.1 components.
     */
    private static String vectorLiteral(int dim) {
        return IntStream.range(0, dim)
                        .mapToObj(i -> "0.1")
                        .collect(Collectors.joining(",", "'[", "]'"));
    }

    /**
     * Return a valid 32-character chash deterministically derived from {@code seed}.
     * Matches ManifestFunctionsTest.validChash() pattern.
     */
    private static String validChash(String seed) {
        return (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
    }
}
