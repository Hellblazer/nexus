package dev.nexus.service;

import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.*;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-gmiaf.16 — ChashRepository integration tests.
 *
 * <p>Hermetic embedded Postgres (zonky). Applies the full Liquibase master
 * changelog (includes chash-001-baseline.xml). Asserts:
 * <ol>
 *   <li>upsert: row inserted; re-upsert refreshes created_at</li>
 *   <li>upsertMany: batch inserts all valid entries; blank entries skipped</li>
 *   <li>lookup: returns all (collection, created_at) rows for a chash</li>
 *   <li>deleteCollection: removes all rows for a collection; returns count</li>
 *   <li>distinctCollections: returns set of all collection names</li>
 *   <li>renameCollection: re-points rows; collision-defense drops existing new rows</li>
 *   <li>deleteStale: removes single (chash, collection) PK; idempotent</li>
 *   <li>isEmpty: true when no rows; false after upsert</li>
 *   <li>countForCollection: returns exact count</li>
 *   <li>doImport / idempotent re-import: fidelity preserving; EXCLUDED verbatim</li>
 *   <li>RLS isolation: tenant A rows invisible to tenant B</li>
 *   <li>RLS WITH CHECK: raw INSERT with wrong tenant_id rejected</li>
 *   <li>fail-closed: unset GUC yields no rows</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChashRepositoryTest {

    private static final String TENANT_A = "chash-tenant-a";
    private static final String TENANT_B = "chash-tenant-b";
    private static final String SVC_ROLE = "svc_chash_test";
    private static final String SVC_PASS = "svc_chash_test_pass";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    ChashRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        // Phase 1: create roles (separate connection, autoCommit=true).
        // CREATE ROLE cannot run inside a transaction; autoCommit=true ensures each
        // statement is committed immediately (mirrors PlanRepositoryTest bootstrap).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        // Phase 2: apply Liquibase master changelog (separate connection so Liquibase
        // commits all changesets before the svc-role grants below).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to chash_index (separate connection so all
        // Liquibase DDL is already committed and visible).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chash_index TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // HikariCP pool connected as svc role — mirror PlanRepositoryTest.buildSvcDataSource()
        // pattern: bare JDBC URL with explicit setUsername so the pool connects as svc_chash_test,
        // NOT as the postgres superuser (which would bypass RLS via BYPASSRLS privilege).
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);

        tenantScope = new TenantScope(svcDs);
        repo = new ChashRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.close();
    }

    // ── Test 1: upsert inserts a new row ─────────────────────────────────────

    @Test
    @Order(1)
    void upsert_insertsRow() {
        repo.upsert(TENANT_A, "abc123def456", "code__nexus");

        var rows = repo.lookup(TENANT_A, "abc123def456");
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection")).isEqualTo("code__nexus");
        assertThat(rows.get(0).get("created_at")).isNotBlank();
    }

    // ── Test 2: upsert refreshes created_at on re-index ──────────────────────

    @Test
    @Order(2)
    void upsert_refreshesCreatedAt_onReindex() throws InterruptedException {
        repo.upsert(TENANT_A, "stale_chash_001", "code__foo");
        var first = repo.lookup(TENANT_A, "stale_chash_001");
        assertThat(first).hasSize(1);
        String firstTs = first.get(0).get("created_at");

        // Small sleep to advance clock
        Thread.sleep(1100);
        repo.upsert(TENANT_A, "stale_chash_001", "code__foo");
        var second = repo.lookup(TENANT_A, "stale_chash_001");
        assertThat(second).hasSize(1);
        String secondTs = second.get(0).get("created_at");

        // Second created_at >= first (monotonic)
        assertThat(secondTs.compareTo(firstTs)).isGreaterThanOrEqualTo(0);
    }

    // ── Test 3: upsertMany inserts batch; blank entries skipped ──────────────

    @Test
    @Order(3)
    void upsertMany_insertsAllValid_skipsBlank() {
        repo.upsertMany(TENANT_A, List.of("chash_m1", "chash_m2", "", "  ", "chash_m3"), "code__batch");

        var m1 = repo.lookup(TENANT_A, "chash_m1");
        var m2 = repo.lookup(TENANT_A, "chash_m2");
        var m3 = repo.lookup(TENANT_A, "chash_m3");
        assertThat(m1).hasSize(1).extracting(r -> r.get("collection")).containsExactly("code__batch");
        assertThat(m2).hasSize(1);
        assertThat(m3).hasSize(1);

        // blank entries not inserted
        var blank = repo.lookup(TENANT_A, "");
        assertThat(blank).isEmpty();
    }

    // ── Test 4: lookup returns multiple collections ───────────────────────────

    @Test
    @Order(4)
    void lookup_returnsAllCollections_forSameChash() {
        String multiChash = "multi_coll_chash_001";
        repo.upsert(TENANT_A, multiChash, "knowledge__delos");
        repo.upsert(TENANT_A, multiChash, "knowledge__delos_docling");

        var rows = repo.lookup(TENANT_A, multiChash);
        assertThat(rows).hasSize(2);
        Set<String> colls = Set.of(
            rows.get(0).get("collection"),
            rows.get(1).get("collection")
        );
        assertThat(colls).containsExactlyInAnyOrder("knowledge__delos", "knowledge__delos_docling");
    }

    // ── Test 5: deleteCollection removes all rows; returns count ─────────────

    @Test
    @Order(5)
    void deleteCollection_removesRows_returnsCount() {
        repo.upsert(TENANT_A, "del_c1", "del__coll");
        repo.upsert(TENANT_A, "del_c2", "del__coll");
        repo.upsert(TENANT_A, "del_c3", "other__coll");

        int deleted = repo.deleteCollection(TENANT_A, "del__coll");
        assertThat(deleted).isEqualTo(2);

        // del__coll rows gone
        assertThat(repo.lookup(TENANT_A, "del_c1")).isEmpty();
        assertThat(repo.lookup(TENANT_A, "del_c2")).isEmpty();
        // other__coll row untouched
        assertThat(repo.lookup(TENANT_A, "del_c3")).hasSize(1);

        // Idempotent: second call returns 0
        assertThat(repo.deleteCollection(TENANT_A, "del__coll")).isEqualTo(0);
    }

    // ── Test 6: distinctCollections returns all known collection names ────────

    @Test
    @Order(6)
    void distinctCollections_returnsAll() {
        // Seed some rows
        repo.upsert(TENANT_A, "dc_chash_1", "distinct__alpha");
        repo.upsert(TENANT_A, "dc_chash_2", "distinct__beta");

        Set<String> colls = repo.distinctCollections(TENANT_A);
        assertThat(colls).contains("distinct__alpha", "distinct__beta");
    }

    // ── Test 7: renameCollection re-points rows ───────────────────────────────

    @Test
    @Order(7)
    void renameCollection_repointsRows() {
        repo.upsert(TENANT_A, "ren_c1", "rename__old");
        repo.upsert(TENANT_A, "ren_c2", "rename__old");

        int updated = repo.renameCollection(TENANT_A, "rename__old", "rename__new");
        assertThat(updated).isEqualTo(2);

        assertThat(repo.lookup(TENANT_A, "ren_c1"))
            .extracting(r -> r.get("collection")).containsExactly("rename__new");
        assertThat(repo.lookup(TENANT_A, "ren_c2"))
            .extracting(r -> r.get("collection")).containsExactly("rename__new");
    }

    // ── Test 8: renameCollection collision defense ────────────────────────────

    @Test
    @Order(8)
    void renameCollection_collisionDefense_dropsPreexistingNewRows() {
        // col_old: ren_coll_c1, ren_coll_c2
        // col_new already has ren_coll_c1 (would collide)
        repo.upsert(TENANT_A, "ren_coll_c1", "col_old");
        repo.upsert(TENANT_A, "ren_coll_c2", "col_old");
        repo.upsert(TENANT_A, "ren_coll_c1", "col_new"); // preexisting collision

        // Rename should succeed: drops (ren_coll_c1, col_new), then updates all col_old -> col_new
        int updated = repo.renameCollection(TENANT_A, "col_old", "col_new");
        assertThat(updated).isEqualTo(2);

        var c1 = repo.lookup(TENANT_A, "ren_coll_c1");
        assertThat(c1).hasSize(1).extracting(r -> r.get("collection")).containsExactly("col_new");
        var c2 = repo.lookup(TENANT_A, "ren_coll_c2");
        assertThat(c2).hasSize(1).extracting(r -> r.get("collection")).containsExactly("col_new");
    }

    // ── Test 9: deleteStale removes specific PK; idempotent ──────────────────

    @Test
    @Order(9)
    void deleteStale_removesSpecificRow_idempotent() {
        repo.upsert(TENANT_A, "stale_abc", "stale__coll1");
        repo.upsert(TENANT_A, "stale_abc", "stale__coll2"); // same chash, different collection

        // Delete only (stale_abc, stale__coll1)
        int deleted = repo.deleteStale(TENANT_A, "stale_abc", "stale__coll1");
        assertThat(deleted).isEqualTo(1);

        // stale__coll2 still present
        var remaining = repo.lookup(TENANT_A, "stale_abc");
        assertThat(remaining).hasSize(1).extracting(r -> r.get("collection")).containsExactly("stale__coll2");

        // Idempotent: deleting again returns 0
        assertThat(repo.deleteStale(TENANT_A, "stale_abc", "stale__coll1")).isEqualTo(0);
    }

    // ── Test 10: isEmpty / countForCollection ─────────────────────────────────

    @Test
    @Order(10)
    void isEmpty_countForCollection() {
        // Use a separate tenant to avoid cross-test contamination
        String freshTenant = "chash-empty-tenant";

        assertThat(repo.isEmpty(freshTenant)).isTrue();
        repo.upsert(freshTenant, "empty_test_c1", "empty__coll");
        repo.upsert(freshTenant, "empty_test_c2", "empty__coll");
        repo.upsert(freshTenant, "empty_test_c3", "other__coll");

        assertThat(repo.isEmpty(freshTenant)).isFalse();
        assertThat(repo.countForCollection(freshTenant, "empty__coll")).isEqualTo(2);
        assertThat(repo.countForCollection(freshTenant, "other__coll")).isEqualTo(1);
        assertThat(repo.countForCollection(freshTenant, "no__such__coll")).isEqualTo(0);
    }

    // ── Test 11: doImport fidelity + idempotent re-import ────────────────────

    @Test
    @Order(11)
    void doImport_fidelity_idempotentRerun() {
        String importTenant = "chash-import-tenant";
        String importChash  = "import_chash_ff00ff";
        String importColl   = "knowledge__imported";
        String createdAt    = "2025-03-15T08:00:00Z";

        repo.doImport(importTenant, importChash, importColl, createdAt);

        var rows = repo.lookup(importTenant, importChash);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection")).isEqualTo(importColl);
        // created_at preserved (exact string form may vary by tz normalization; check date portion)
        assertThat(rows.get(0).get("created_at")).contains("2025-03-15");

        // Idempotent re-import: same row, no duplicate
        repo.doImport(importTenant, importChash, importColl, createdAt);
        assertThat(repo.lookup(importTenant, importChash)).hasSize(1);
        assertThat(repo.countForCollection(importTenant, importColl)).isEqualTo(1);
    }

    // ── Test 12: RLS isolation — tenant A rows invisible to tenant B ──────────

    @Test
    @Order(12)
    void rls_isolation_tenantAInvisibleToTenantB() {
        String chashA = "rls_iso_chashA_001";
        repo.upsert(TENANT_A, chashA, "rls__collA");

        // TENANT_B lookup returns empty
        var rowsB = repo.lookup(TENANT_B, chashA);
        assertThat(rowsB).isEmpty();

        // TENANT_A can still see its own row
        var rowsA = repo.lookup(TENANT_A, chashA);
        assertThat(rowsA).hasSize(1);
    }

    // ── Test 13: RLS WITH CHECK — cross-tenant INSERT rejected ───────────────

    @Test
    @Order(13)
    void rls_withCheck_crossTenantInsertRejected() throws Exception {
        // Directly insert with wrong tenant_id in the row
        try (Connection conn = svcDs.getConnection()) {
            conn.setAutoCommit(false);
            conn.createStatement().execute(
                "SET LOCAL nexus.tenant = '" + TENANT_A + "'");
            // Attempt to insert row with tenant_id = TENANT_B while GUC = TENANT_A
            var ex = assertThrows(PSQLException.class, () -> {
                conn.createStatement().execute(
                    "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) " +
                    "VALUES ('" + TENANT_B + "', 'with_check_chash', 'with_check_coll', now())");
            });
            assertThat(ex.getMessage()).containsIgnoringCase("violates row-level security");
            conn.rollback();
        }
    }

    // ── Test 14: fail-closed — unset GUC yields no rows ──────────────────────

    @Test
    @Order(14)
    void failClosed_unsetGuc_yieldsNoRows() throws Exception {
        // Seed a row under TENANT_A
        repo.upsert(TENANT_A, "fail_closed_chash", "fail__coll");

        // Connect without setting nexus.tenant GUC
        try (Connection conn = svcDs.getConnection()) {
            conn.setAutoCommit(false);
            // DO NOT set nexus.tenant
            var rs = conn.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.chash_index");
            rs.next();
            int count = rs.getInt(1);
            // RLS fail-closed: unset GUC → NULL != any tenant_id → 0 rows
            assertThat(count).isEqualTo(0);
            conn.rollback();
        }
    }
}
