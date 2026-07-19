package dev.nexus.service;

import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
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

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    ChashRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    /** Deterministic canonical chash per seed (RDR-180: full 32-byte
     *  digest; the pre-flip pad-to-32-chars fixture shape died with the
     *  length-only contract). */
    private static dev.nexus.service.db.Chash ch(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed);
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Phase 1: create roles (separate connection, autoCommit=true).
        // CREATE ROLE cannot run inside a transaction; autoCommit=true ensures each
        // statement is committed immediately (mirrors PlanRepositoryTest bootstrap).
        try (Connection su = pg.createConnection("")) {
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
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to chash_index (separate connection so all
        // Liquibase DDL is already committed and visible).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chash_index TO " + SVC_ROLE);
            // RDR-156 P0.2: ChashRepository.upsert now auto-stubs catalog_collections.
            su.createStatement().execute("GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // HikariCP pool connected as svc role — mirror PlanRepositoryTest.buildSvcDataSource()
        // pattern: bare JDBC URL with explicit setUsername so the pool connects as svc_chash_test,
        // NOT as the postgres superuser (which would bypass RLS via BYPASSRLS privilege).
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
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
        if (pg != null) pg.stop();
    }

    // ── Test 1: upsert inserts a new row ─────────────────────────────────────

    @Test
    @Order(1)
    void upsert_insertsRow() {
        repo.upsert(TENANT_A, ch("abc123def456"), "code__nexus");

        var rows = repo.lookup(TENANT_A, ch("abc123def456"));
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection")).isEqualTo("code__nexus");
        assertThat(rows.get(0).get("created_at")).isNotBlank();
    }

    // ── Test 2: upsert refreshes created_at on re-index ──────────────────────

    @Test
    @Order(2)
    void upsert_refreshesCreatedAt_onReindex() throws InterruptedException {
        repo.upsert(TENANT_A, ch("stale_chash_001"), "code__foo");
        var first = repo.lookup(TENANT_A, ch("stale_chash_001"));
        assertThat(first).hasSize(1);
        String firstTs = first.get(0).get("created_at");

        // Small sleep to advance clock
        Thread.sleep(1100);
        repo.upsert(TENANT_A, ch("stale_chash_001"), "code__foo");
        var second = repo.lookup(TENANT_A, ch("stale_chash_001"));
        assertThat(second).hasSize(1);
        String secondTs = second.get(0).get("created_at");

        // Second created_at >= first (monotonic)
        assertThat(secondTs.compareTo(firstTs)).isGreaterThanOrEqualTo(0);
    }

    // ── Test 3: upsertMany inserts batch; blank entries skipped ──────────────

    @Test
    @Order(3)
    void upsertMany_insertsAllValid_skipsBlank() {
        // null entries (the pre-flip blank-string shape) are skipped
        repo.upsertMany(TENANT_A,
                java.util.Arrays.asList(ch("chash_m1"), ch("chash_m2"), null, ch("chash_m3")),
                "code__batch");

        var m1 = repo.lookup(TENANT_A, ch("chash_m1"));
        var m2 = repo.lookup(TENANT_A, ch("chash_m2"));
        var m3 = repo.lookup(TENANT_A, ch("chash_m3"));
        assertThat(m1).hasSize(1).extracting(r -> r.get("collection")).containsExactly("code__batch");
        assertThat(m2).hasSize(1);
        assertThat(m3).hasSize(1);

    }

    // ── Test 3b: upsertMany tolerates in-batch duplicate chashes ─────────────

    @Test
    @Order(3)
    void upsertMany_dedupsInBatchDuplicates() {
        // nexus-85z0y: a multi-VALUES INSERT .. ON CONFLICT DO UPDATE raises
        // "cannot affect row a second time" when one statement repeats a
        // conflict key. Real files emit duplicate chunk text, so a batch
        // like [dup, dup, dup2] must succeed, not 500.
        repo.upsertMany(TENANT_A,
                List.of(ch("chash_dup_a"), ch("chash_dup_a"), ch("chash_dup_b"),
                        ch("chash_dup_a"), ch("chash_dup_b")),
                "code__dupbatch");

        var a = repo.lookup(TENANT_A, ch("chash_dup_a"));
        var b = repo.lookup(TENANT_A, ch("chash_dup_b"));
        assertThat(a).hasSize(1).extracting(r -> r.get("collection")).containsExactly("code__dupbatch");
        assertThat(b).hasSize(1).extracting(r -> r.get("collection")).containsExactly("code__dupbatch");
    }

    // ── Test 4: lookup returns multiple collections ───────────────────────────

    @Test
    @Order(4)
    void lookup_returnsAllCollections_forSameChash() {
        var multiChash = ch("multi_coll_chash_001");
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
        repo.upsert(TENANT_A, ch("del_c1"), "del__coll");
        repo.upsert(TENANT_A, ch("del_c2"), "del__coll");
        repo.upsert(TENANT_A, ch("del_c3"), "other__coll");

        int deleted = repo.deleteCollection(TENANT_A, "del__coll");
        assertThat(deleted).isEqualTo(2);

        // del__coll rows gone
        assertThat(repo.lookup(TENANT_A, ch("del_c1"))).isEmpty();
        assertThat(repo.lookup(TENANT_A, ch("del_c2"))).isEmpty();
        // other__coll row untouched
        assertThat(repo.lookup(TENANT_A, ch("del_c3"))).hasSize(1);

        // Idempotent: second call returns 0
        assertThat(repo.deleteCollection(TENANT_A, "del__coll")).isEqualTo(0);
    }

    // ── Test 6: distinctCollections returns all known collection names ────────

    @Test
    @Order(6)
    void distinctCollections_returnsAll() {
        // Seed some rows
        repo.upsert(TENANT_A, ch("dc_chash_1"), "distinct__alpha");
        repo.upsert(TENANT_A, ch("dc_chash_2"), "distinct__beta");

        Set<String> colls = repo.distinctCollections(TENANT_A);
        assertThat(colls).contains("distinct__alpha", "distinct__beta");
    }

    // ── Test 7: renameCollection re-points rows ───────────────────────────────

    @Test
    @Order(7)
    void renameCollection_repointsRows() {
        repo.upsert(TENANT_A, ch("ren_c1"), "rename__old");
        repo.upsert(TENANT_A, ch("ren_c2"), "rename__old");

        int updated = repo.renameCollection(TENANT_A, "rename__old", "rename__new");
        assertThat(updated).isEqualTo(2);

        assertThat(repo.lookup(TENANT_A, ch("ren_c1")))
            .extracting(r -> r.get("collection")).containsExactly("rename__new");
        assertThat(repo.lookup(TENANT_A, ch("ren_c2")))
            .extracting(r -> r.get("collection")).containsExactly("rename__new");
    }

    // ── Test 8: renameCollection collision defense ────────────────────────────

    @Test
    @Order(8)
    void renameCollection_collisionDefense_dropsPreexistingNewRows() {
        // col_old: ren_coll_c1, ren_coll_c2
        // col_new already has ren_coll_c1 (would collide)
        repo.upsert(TENANT_A, ch("ren_coll_c1"), "col_old");
        repo.upsert(TENANT_A, ch("ren_coll_c2"), "col_old");
        repo.upsert(TENANT_A, ch("ren_coll_c1"), "col_new"); // preexisting collision

        // Rename should succeed: drops (ren_coll_c1, col_new), then updates all col_old -> col_new
        int updated = repo.renameCollection(TENANT_A, "col_old", "col_new");
        assertThat(updated).isEqualTo(2);

        var c1 = repo.lookup(TENANT_A, ch("ren_coll_c1"));
        assertThat(c1).hasSize(1).extracting(r -> r.get("collection")).containsExactly("col_new");
        var c2 = repo.lookup(TENANT_A, ch("ren_coll_c2"));
        assertThat(c2).hasSize(1).extracting(r -> r.get("collection")).containsExactly("col_new");
    }

    // ── Test 9: deleteStale removes specific PK; idempotent ──────────────────

    @Test
    @Order(9)
    void deleteStale_removesSpecificRow_idempotent() {
        repo.upsert(TENANT_A, ch("stale_abc"), "stale__coll1");
        repo.upsert(TENANT_A, ch("stale_abc"), "stale__coll2"); // same chash, different collection

        // Delete only (stale_abc, stale__coll1)
        int deleted = repo.deleteStale(TENANT_A, ch("stale_abc"), "stale__coll1");
        assertThat(deleted).isEqualTo(1);

        // stale__coll2 still present
        var remaining = repo.lookup(TENANT_A, ch("stale_abc"));
        assertThat(remaining).hasSize(1).extracting(r -> r.get("collection")).containsExactly("stale__coll2");

        // Idempotent: deleting again returns 0
        assertThat(repo.deleteStale(TENANT_A, ch("stale_abc"), "stale__coll1")).isEqualTo(0);
    }

    // ── Test 10: isEmpty / countForCollection ─────────────────────────────────

    @Test
    @Order(10)
    void isEmpty_countForCollection() {
        // Use a separate tenant to avoid cross-test contamination
        String freshTenant = "chash-empty-tenant";

        assertThat(repo.isEmpty(freshTenant)).isTrue();
        repo.upsert(freshTenant, ch("empty_test_c1"), "empty__coll");
        repo.upsert(freshTenant, ch("empty_test_c2"), "empty__coll");
        repo.upsert(freshTenant, ch("empty_test_c3"), "other__coll");

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
        var importChash  = ch("import_chash_ff00ff");
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

    // ── Tests 19-23: doImportBatch — ONE multi-row INSERT per request ─────────
    // nexus-1usso: handleImport looped repo.doImport per row (200 rows = 600+
    // sequential PG round-trips per HTTP request ≈ 0.9s server-side = the
    // measured 1-batch/s (~34KB/s) migration ceiling). The batch method lands
    // the whole request in one multi-row INSERT ... ON CONFLICT.

    @Test
    @Order(19)
    void doImportBatch_insertsAll_acrossCollections() {
        String t = "chash-batch-tenant";
        int n = repo.doImportBatch(t, List.of(
                new ChashRepository.ImportRow(ch("batch_c1"), "code__batch_a", "2025-01-01T00:00:00Z"),
                new ChashRepository.ImportRow(ch("batch_c2"), "code__batch_a", "2025-01-02T00:00:00Z"),
                new ChashRepository.ImportRow(ch("batch_c3"), "docs__batch_b", "2025-01-03T00:00:00Z")));
        assertThat(n).isEqualTo(3);
        assertThat(repo.countForCollection(t, "code__batch_a")).isEqualTo(2);
        assertThat(repo.countForCollection(t, "docs__batch_b")).isEqualTo(1);
        // created_at fidelity preserved (not clobbered to now)
        assertThat((String) repo.lookup(t, ch("batch_c1")).get(0).get("created_at"))
                .contains("2025-01-01");
    }

    @Test
    @Order(20)
    void doImportBatch_idempotentRerun_noDuplicates() {
        String t = "chash-batch-tenant2";
        var rows = List.of(new ChashRepository.ImportRow(
                ch("bat2_c1"), "code__batch2", "2025-02-01T00:00:00Z"));
        repo.doImportBatch(t, rows);
        repo.doImportBatch(t, rows);
        assertThat(repo.countForCollection(t, "code__batch2")).isEqualTo(1);
    }

    @Test
    @Order(21)
    void doImportBatch_intraBatchDuplicate_lastWins_noError() {
        // A single multi-row INSERT ... ON CONFLICT cannot touch the same row
        // twice (PG: "cannot affect row a second time") — the repo must dedupe
        // within the batch, last occurrence winning.
        String t = "chash-batch-tenant3";
        int n = repo.doImportBatch(t, List.of(
                new ChashRepository.ImportRow(ch("dup_c"), "code__dup", "2025-03-01T00:00:00Z"),
                new ChashRepository.ImportRow(ch("dup_c"), "code__dup", "2025-03-09T00:00:00Z")));
        assertThat(n).isEqualTo(1);
        var got = repo.lookup(t, ch("dup_c"));
        assertThat(got).hasSize(1);
        assertThat((String) got.get(0).get("created_at")).contains("2025-03-09");
    }

    @Test
    @Order(22)
    void doImportBatch_badCreatedAt_fallsBackToNow_rowStillLands() {
        String t = "chash-batch-tenant4";
        int n = repo.doImportBatch(t, List.of(
                new ChashRepository.ImportRow(ch("badts_c"), "code__badts", "not-a-timestamp")));
        assertThat(n).isEqualTo(1);
        assertThat(repo.lookup(t, ch("badts_c"))).hasSize(1);
    }

    @Test
    @Order(23)
    void doImportBatch_emptyAndNull_returnZero() {
        assertThat(repo.doImportBatch("chash-batch-tenant5", List.of())).isZero();
        assertThat(repo.doImportBatch("chash-batch-tenant5", null)).isZero();
    }

    // ── Test 18: ensureCollectionRegistered rejects blank collection ─────────────

    @Test
    @Order(18)
    void upsert_blankCollection_throwsIllegalArgument() {
        // ChashRepository.ensureCollectionRegistered throws IllegalArgumentException
        // for blank physical_collection rather than silently skipping registration
        // (which would let the subsequent FK-constrained INSERT fail with a cryptic
        // FK violation instead of a clear caller-error message).
        // upsert() is the most direct public entry point that reaches ensureCollectionRegistered;
        // upsert() itself also guards blank collection, so the IAE surfaces from there.
        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class, () ->
            repo.upsert(TENANT_A, ch("some_valid_chash"), ""));
        assertThat(ex.getMessage())
            .as("blank physical_collection must produce a clear IllegalArgumentException")
            .contains("must not be");

        // Also verify for whitespace-only value
        IllegalArgumentException exWs = assertThrows(IllegalArgumentException.class, () ->
            repo.upsert(TENANT_A, ch("some_valid_chash"), "  "));
        assertThat(exWs.getMessage())
            .as("whitespace-only physical_collection must also be rejected")
            .contains("must not be");
    }

    // ── Test 12: RLS isolation — tenant A rows invisible to tenant B ──────────

    @Test
    @Order(12)
    void rls_isolation_tenantAInvisibleToTenantB() {
        var chashA = ch("rls_iso_chashA_001");
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
                    "VALUES ('" + TENANT_B + "', decode('deadbeef', 'hex'), 'with_check_coll', now())");
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
        repo.upsert(TENANT_A, ch("fail_closed_chash"), "fail__coll");

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

    // ── Test 15: registeredChashesForCollection ───────────────────────────────

    @Test
    @Order(15)
    void registeredChashesForCollection_returnsFullHexSet() {
        // RDR-180 (nexus-jxizy.7): the natural chunk id is the FULL digest —
        // the pre-flip [:32] prefix compensation is retired; the set carries
        // 64-hex renderings of the stored 32-byte keys.
        repo.upsert(TENANT_A, ch("short_ch_001"),  "reg__coll");
        repo.upsert(TENANT_A, ch("other_ch_001"), "other__coll");

        Set<String> result = repo.registeredChashesForCollection(TENANT_A, "reg__coll");
        assertThat(result).contains(ch("short_ch_001").toHex());
        assertThat(result.iterator().next()).hasSize(64);
        // other__coll chash must NOT appear.
        assertThat(result).doesNotContain(ch("other_ch_001").toHex());
    }

    @Test
    @Order(16)
    void registeredChashesForCollection_unknownCollection_returnsEmpty() {
        Set<String> result = repo.registeredChashesForCollection(TENANT_A, "no__such__coll");
        assertThat(result).isEmpty();
    }

    @Test
    @Order(17)
    void registeredChashesForCollection_rlsIsolated() {
        // Seed under TENANT_A; TENANT_B must see empty
        repo.upsert(TENANT_A, ch("rls_reg_chash_001"), "rls__reg__coll");

        Set<String> resultA = repo.registeredChashesForCollection(TENANT_A, "rls__reg__coll");
        assertThat(resultA).contains(ch("rls_reg_chash_001").toHex());

        Set<String> resultB = repo.registeredChashesForCollection(TENANT_B, "rls__reg__coll");
        assertThat(resultB).isEmpty();
    }
}
