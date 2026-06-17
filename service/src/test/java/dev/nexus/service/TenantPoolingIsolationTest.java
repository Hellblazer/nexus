package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4pz86 (conexus RDR-001 consumer requirement): prove the engine's tenant
 * isolation is leak-safe when a single PostgreSQL <em>server</em> connection is reused
 * across transactions belonging to different tenants — the exact condition a
 * transaction-mode connection pooler (PgBouncer, as in Crunchy production) creates.
 *
 * <p><b>Why a {@code maximumPoolSize=1} HikariCP pool is a faithful test of the leak
 * property.</b> {@link TenantScope} stamps {@code nexus.tenant} with
 * {@code set_config(..., true)} (transaction-local / {@code SET LOCAL} semantics) and
 * commits before returning the connection to the pool. With the pool capped at ONE
 * connection, the second {@code withTenant} call necessarily borrows the SAME physical
 * server connection the first call just used — identical to a txn-mode pooler routing a
 * second client's transaction onto a reused server backend.
 *
 * <p><b>Sensitivity note (substantive-critic 2026-06-16).</b> {@code withTenant} re-stamps
 * the GUC at the start of EVERY transaction, so the cross-tenant isolation tests below
 * would pass even if the GUC were session-scoped (tenant B re-stamps to itself before its
 * SELECT). Those tests therefore prove RLS FILTERING holds under connection reuse (still
 * worth guarding), but NOT the transaction-local reset property. The test sensitive to
 * THAT property is {@link #tenantGucIsResetAfterTransactionCommit()}: it reads the GUC on a
 * raw, un-stamped borrow of the reused connection and asserts it is empty, so it fails if
 * the {@code set_config(..., true)} locality flag ever regresses to {@code false}.
 *
 * <p>Together these cover acceptance criterion (b) deterministically and hermetically (no
 * external pooler). Criterion (a) "operates correctly under a real transaction-mode
 * PgBouncer" is covered by {@code PgBouncerTenantIsolationTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantPoolingIsolationTest {

    private static final String TENANT_A = "pool-tenant-a";
    private static final String TENANT_B = "pool-tenant-b";
    private static final String PROJECT = "isolation-probe";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;          // capped at ONE connection — forces server-conn reuse
    MemoryRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        // grants-nexus-svc.xml fail-fasts if the role is absent; create it first.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='"
                + PgContainerHelper.SVC_USERNAME + "') THEN CREATE ROLE "
                + PgContainerHelper.SVC_USERNAME + " LOGIN PASSWORD '"
                + PgContainerHelper.SVC_PASSWORD + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (var lq = new Liquibase("db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                lq.update(new Contexts());
            }
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);   // NOSUPERUSER NOBYPASSRLS: RLS applies
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(1);                          // one server connection, reused
        cfg.setMinimumIdle(1);
        cfg.setAutoCommit(true);
        cfg.setConnectionInitSql("SET search_path TO nexus, t1, public");
        ds = new HikariDataSource(cfg);

        repo = new MemoryRepository(new TenantScope(ds));
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void cleanMemory() throws Exception {
        // PER_CLASS shares one container; reset the table so per-test row counts are
        // deterministic (the isolation property is independent of this, but the
        // positive-control counts are not).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("TRUNCATE nexus.memory");
        }
    }

    @Test
    void tenantGucIsResetAfterTransactionCommit() throws Exception {
        // THE tripwire actually sensitive to the transaction-local property (the
        // re-stamping isolation tests below are NOT — every withTenant call re-stamps
        // the GUC before its query, so a session-scoped GUC would still filter correctly
        // and pass them). Here we read the GUC on a RAW borrow with NO re-stamp:
        // after a withTenant txn commits and returns its connection to the size-1 pool,
        // the SAME server backend must carry NO lingering nexus.tenant. If set_config's
        // is_local arg were changed true->false, the GUC would persist here and fail.
        repo.upsert(TENANT_A, PROJECT, "g", "v", "", null, null, null);  // stamps + commits as A
        try (Connection raw = ds.getConnection();
             var st = raw.createStatement();
             var rs = st.executeQuery("SELECT current_setting('nexus.tenant', true) AS t")) {
            assertThat(rs.next()).isTrue();
            String guc = rs.getString("t");
            assertThat(guc)
                .as("nexus.tenant must be reset after the txn commits (transaction-local "
                    + "GUC); a session-scoped GUC would leak to the next pooled borrower")
                .isNullOrEmpty();
        }
    }

    @Test
    void tenantContextDoesNotBleedAcrossReusedConnection() {
        // Tenant A writes a row.
        long id = repo.upsert(TENANT_A, PROJECT, "secret", "tenant-A-only", "", null, null, null);
        assertThat(id).isPositive();

        // Tenant B, reusing the SAME pooled server connection (pool size 1), must see
        // NONE of tenant A's rows. A leaked session GUC would surface A's row here.
        assertThat(repo.findByProject(TENANT_B, PROJECT))
            .as("tenant B must not see tenant A's rows across a reused server connection")
            .isEmpty();

        // Positive control / non-vacuity: tenant A still sees its own row, so B's empty
        // result is real RLS isolation, not a mis-seeded fixture or a broken connection.
        assertThat(repo.findByProject(TENANT_A, PROJECT))
            .as("tenant A must see its own row (positive control)")
            .hasSize(1);
    }

    @Test
    void repeatedInterleavingDoesNotAccumulateLeak() {
        // Exercise many A/B handoffs on the single connection to catch any stamp that
        // survives only intermittently (e.g. reset depending on prior statement).
        for (int i = 0; i < 5; i++) {
            repo.upsert(TENANT_A, PROJECT, "row-" + i, "A-" + i, "", null, null, null);
            assertThat(repo.findByProject(TENANT_B, PROJECT))
                .as("iteration %s: tenant B must stay isolated", i)
                .isEmpty();
        }
        // Tenant A accumulated exactly its 5 rows (row-0..4; @BeforeEach truncated first);
        // tenant B still sees zero.
        assertThat(repo.findByProject(TENANT_B, PROJECT)).isEmpty();
        assertThat(repo.findByProject(TENANT_A, PROJECT)).hasSize(5);
    }
}
