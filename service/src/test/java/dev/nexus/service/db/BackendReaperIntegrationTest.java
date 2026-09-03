/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-g17tf -- {@link BackendReaper#terminateOwnBackends} against a REAL
 * Postgres: a pooled connection running a statement that genuinely runs
 * long is terminated (the client sees SQLSTATE 57P01, admin_shutdown) and is
 * ABSENT from {@code pg_stat_activity} afterwards, which is the simulated
 * container-stop acceptance on the bead. A peer's backend under a different
 * application_name survives untouched.
 *
 * <p>The pool AND the reaper run as the production {@code nexus_svc} role
 * (NOSUPERUSER, NOBYPASSRLS): {@code pg_terminate_backend} on a same-role
 * backend needs no superuser, and this is the proof (code-review-expert,
 * 2026-09-02). Production connects to Postgres directly; a transaction-mode
 * pooler that strips {@code application_name} is NOT covered here and would
 * surface at shutdown as {@code backend_reaper_matched_nothing}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class BackendReaperIntegrationTest {

    static final String ADMIN_SHUTDOWN = "57P01";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void ownLongRunningBackendIsTerminatedAndGoneFromPgStatActivity() throws Exception {
        String own = BackendReaper.newApplicationName("test");
        String peer = BackendReaper.newApplicationName("test");
        assertThat(own).isNotEqualTo(peer).startsWith("nexus-service/test/");

        try (HikariDataSource ownPool = pool(own);
             HikariDataSource peerPool = pool(peer);
             Connection peerConn = peerPool.getConnection();
             Connection ownConn = ownPool.getConnection()) {

            // A statement that genuinely runs long, started before the reaper fires.
            CompletableFuture<String> outcome = CompletableFuture.supplyAsync(() -> {
                try (Statement st = ownConn.createStatement()) {
                    st.execute("SELECT pg_sleep(60)");
                    return "completed";
                } catch (SQLException e) {
                    return e.getSQLState();
                }
            });
            int ownPid = awaitActive(own);
            assertThat(countBackends(peer)).isEqualTo(1);

            int terminated = BackendReaper.terminateOwnBackends(
                pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD,
                own, ownPool.getHikariPoolMXBean().getActiveConnections());
            assertThat(terminated).isEqualTo(1);

            // The client observed the termination, not a completed 60s sleep.
            assertThat(outcome.get(10, TimeUnit.SECONDS)).isEqualTo(ADMIN_SHUTDOWN);
            // The terminated backend is gone. (HikariCP refills its slot with a
            // NEW backend under the same application_name, which is why the
            // assertion is on the pid, not the count; in production ds.close()
            // follows the reaper and no refill happens.)
            awaitGone(ownPid);
            assertThat(backendExists(ownPid)).isFalse();
            // The peer under its own application_name is untouched.
            assertThat(countBackends(peer)).isEqualTo(1);
            assertThat(peerConn.isValid(2)).isTrue();
        }
    }

    @Test
    void unreachableDatabaseIsReportedNotThrown() {
        int rc = BackendReaper.terminateOwnBackends(
            "jdbc:postgresql://127.0.0.1:1/nowhere", "x", "x", "nexus-service/test/dead");
        assertThat(rc).isEqualTo(-1);
    }

    private HikariDataSource pool(String applicationName) {
        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(1);
        cfg.addDataSourceProperty("ApplicationName", applicationName);
        return new HikariDataSource(cfg);
    }

    private int countBackends(String applicationName) throws SQLException {
        try (Connection c = pg.createConnection("");
             PreparedStatement ps = c.prepareStatement(
                 "SELECT count(*) FROM pg_stat_activity WHERE application_name = ?")) {
            ps.setString(1, applicationName);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getInt(1);
            }
        }
    }

    /** Wait for the pg_sleep backend to show as active; returns its pid. */
    private int awaitActive(String applicationName) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline) {
            try (Connection c = pg.createConnection("");
                 PreparedStatement ps = c.prepareStatement(
                     "SELECT pid FROM pg_stat_activity WHERE application_name = ?"
                     + " AND state = 'active'")) {
                ps.setString(1, applicationName);
                try (ResultSet rs = ps.executeQuery()) {
                    if (rs.next()) {
                        return rs.getInt(1);
                    }
                }
            }
            Thread.sleep(50);
        }
        throw new AssertionError("own backend never became active");
    }

    private boolean backendExists(int pid) throws SQLException {
        try (Connection c = pg.createConnection("");
             PreparedStatement ps = c.prepareStatement(
                 "SELECT 1 FROM pg_stat_activity WHERE pid = ?")) {
            ps.setInt(1, pid);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next();
            }
        }
    }

    private void awaitGone(int pid) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline && backendExists(pid)) {
            Thread.sleep(50);
        }
    }
}
