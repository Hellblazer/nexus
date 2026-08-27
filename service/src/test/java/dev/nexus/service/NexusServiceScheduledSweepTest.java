package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-lgiqw — the {@code t1-ttl-sweep} loop's WIRING, as distinct from the three
 * methods it calls.
 *
 * <p>WHY THIS CLASS EXISTS. Before it, nothing exercised the loop at all. Every arm
 * had its own unit test ({@code ScratchRepository.sweepTenant}, {@code
 * TokenStore.sweepExpiredSessions}, {@code TokenStore.sweepExpiredDataTokens}) and
 * every one of them passed, while nothing proved the scheduler ever invoked any of
 * them — deleting an arm outright would have left the entire suite green. That is
 * the same defect class the reaper's own design notes are about: a check that
 * cannot fail for the thing it covers.
 *
 * <p>It also pins the aggregate counter. {@code total_data_tokens_deleted} is the
 * breadcrumb that makes "accrual changed shape" diagnosable after the fact
 * (steady state ~80 per 6h cycle) — nothing alerts on it, so it is not a
 * detector, but a counter that is never asserted on can silently stop being
 * wired at all. {@link NexusService#runScheduledSweep} therefore returns the
 * counts and these tests read them.
 *
 * <p>The scheduler itself is NOT exercised here — its initial delay is six hours,
 * so a test that waited for a natural cycle would not be a test. The extraction is
 * what makes the body reachable; what remains untested is the single
 * {@code scheduleAtFixedRate} call, which is now three lines of straight-line code
 * with nothing to get wrong except the interval constants.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceScheduledSweepTest {

    private static final String ROOT_TOKEN = "scheduled-sweep-test-root-token-9f31c7";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);

        // Port 0: dynamic allocation, never a hardcoded test port.
        service = new NexusService(0, ROOT_TOKEN, ds);
    }

    @AfterAll
    void stopAll() {
        if (service != null) {
            try {
                service.stop();
            } catch (Exception ignored) {
                // never started; nothing to tear down
            }
        }
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private void insertDataToken(String tenant, String label, Instant expiresAt) throws Exception {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, scope, expires_at) "
                 + "VALUES (?, ?, ?, 'data', ?)")) {
            ps.setString(1, TokenHashing.sha256Hex(tenant + ":" + label + ":" + expiresAt));
            ps.setString(2, tenant);
            ps.setString(3, label);
            ps.setObject(4, OffsetDateTime.ofInstant(expiresAt, ZoneOffset.UTC));
            ps.executeUpdate();
        }
    }

    /**
     * Remove a tenant's rows. Load-bearing for isolation, not tidiness:
     * {@link NexusService#runScheduledSweep} sweeps EVERY tenant, so a sweepable row
     * left behind by one test becomes another test's deleted-count. Any test that
     * deliberately leaves an eligible row must clean it up.
     */
    private void deleteTokens(String tenant) throws Exception {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "DELETE FROM nexus.service_tokens WHERE tenant_id = ?")) {
            ps.setString(1, tenant);
            ps.executeUpdate();
        }
    }

    private boolean tokenExists(String tenant, String label) throws Exception {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT 1 FROM nexus.service_tokens WHERE tenant_id = ? AND label = ?")) {
            ps.setString(1, tenant);
            ps.setString(2, label);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next();
            }
        }
    }

    @Test
    void runScheduledSweep_reapsExpiredDataTokens_andReportsTheCount() throws Exception {
        String tenant = "sched-sweep-" + System.nanoTime();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);

        insertDataToken(tenant, "long-expired", now.toInstant().minus(Duration.ofDays(30)));
        insertDataToken(tenant, "inside-grace", now.toInstant().minus(Duration.ofDays(2)));

        NexusService.SweepCounts counts = service.runScheduledSweep(now);

        assertThat(counts.dataTokens())
            .as("the reaper arm must actually be invoked by the sweep cycle -- this is the "
                + "wiring assertion; the arm's own behaviour is TokenStoreDataTokenSweepTest")
            .isEqualTo(1);
        assertThat(tokenExists(tenant, "long-expired")).isFalse();
        assertThat(tokenExists(tenant, "inside-grace"))
            .as("the production grace window must be applied by the CALLER, not defaulted "
                + "away -- a cycle that passed no grace would delete this too")
            .isTrue();

        assertThat(counts.tenants())
            .as("the loop enumerates tenants from service_tokens itself, so a tenant holding "
                + "sweepable rows is always in the list")
            .isGreaterThanOrEqualTo(2); // the default tenant plus ours
    }

    @Test
    void runScheduledSweep_reportsZeroWhenNothingIsEligible() throws Exception {
        String tenant = "sched-sweep-empty-" + System.nanoTime();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertDataToken(tenant, "still-live", now.toInstant().plusSeconds(300));

        NexusService.SweepCounts counts = service.runScheduledSweep(now);

        assertThat(counts.dataTokens())
            .as("a cycle that swept nothing reports zero -- 'ran, found nothing' must be "
                + "distinguishable from 'did not run', which is the whole value of the "
                + "counter given nothing alerts on it")
            .isEqualTo(0);
        assertThat(tokenExists(tenant, "still-live")).isTrue();
    }

    @Test
    @Timeout(value = 60, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void runScheduledSweep_survivesABlockedArm_ratherThanStallingTheCycle() throws Exception {
        String tenant = "sched-sweep-blocked-" + System.nanoTime();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertDataToken(tenant, "long-expired", now.toInstant().minus(Duration.ofDays(30)));

        // SHARE MODE blocks the arms' DELETEs while still permitting the cycle's
        // tenant-enumeration SELECT, so this isolates the per-arm bound.
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            try (Statement st = blocker.createStatement()) {
                st.execute("LOCK TABLE nexus.service_tokens IN SHARE MODE");
            }

            long startedAt = System.nanoTime();
            NexusService.SweepCounts counts =
                service.runScheduledSweep(now, Duration.ofMillis(500));
            long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000;

            assertThat(counts.dataTokens())
                .as("the blocked arm contributes nothing and is logged, rather than "
                    + "propagating out and killing the cycle")
                .isEqualTo(0);
            assertThat(elapsedMs)
                .as("THE property this whole bound exists for: one blocked statement must "
                    + "not stall the shared single-threaded cycle. Unbounded, this never "
                    + "returns -- silently, on a daemon thread, with no alarm")
                .isLessThan(30_000);

            blocker.rollback();
        }

        assertThat(tokenExists(tenant, "long-expired"))
            .as("nothing was abandoned; the next cycle picks it up")
            .isTrue();

        // Required, not housekeeping: this row is deliberately still sweepable, and
        // runScheduledSweep is fleet-wide, so leaving it would show up as an extra
        // deletion in whichever test runs next.
        deleteTokens(tenant);
    }

    /**
     * nexus-4tosp. The sibling test above proves a blocked arm does not STALL the
     * cycle, and its own assertion message names the hazard exactly: "silently, on
     * a daemon thread, with no alarm". That was written about the UNBOUNDED case.
     * The BOUNDED case has the same property: the timeout is caught, logged at
     * WARN, and the scheduler re-fires in six hours, forever, making no progress,
     * on a service whose /health stays green.
     *
     * <p>So "survives a blocked arm" and "reaps nothing, permanently" were
     * indistinguishable from outside the logs. This asserts the VALUE of the
     * counter in both directions -- it must climb while the arm keeps failing
     * and reset the moment one succeeds -- rather than asserting the absence of
     * a stall, which is what let this ship. Delete the counter and this test
     * fails; it cannot pass vacuously.
     *
     * <p>SCOPE, so this test is not mistaken for more than it is: the counter is
     * DIAGNOSIS, not detection. It sees "installed but never reaping". It is
     * BLIND to "scheduler never fired" -- no cycles means no failures to count,
     * so it reads zero, which looks healthy. Detection is the absence of the
     * unconditional t1_scheduled_sweep_complete heartbeat, alarmed over a window
     * longer than one period, which no unit test can assert because it lives in
     * the deployment's metric filter. Do not extend this test to claim it.
     */
    @Test
    @Timeout(value = 90, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void runScheduledSweep_repeatedArmFailure_climbsACounter_andResetsOnRecovery() throws Exception {
        String tenant = "sched-sweep-stall-" + System.nanoTime();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertDataToken(tenant, "long-expired", now.toInstant().minus(Duration.ofDays(30)));

        // try/finally, NOT a trailing deleteTokens: this row is deliberately
        // sweepable and runScheduledSweep is fleet-wide, so a mid-assert failure
        // that skipped cleanup would surface as a phantom extra deletion in
        // whichever test ran next -- turning one red into two and pointing the
        // second one at innocent code. Measured: proving this test non-vacuous
        // by mutation did exactly that to
        // runScheduledSweep_reapsExpiredDataTokens_andReportsTheCount.
        try {
        assertThat(service.consecutiveDataSweepFailures())
            .as("precondition: a service that has never failed reports zero")
            .isEqualTo(0);
        assertThat(service.dataSweepStalled())
            .as("precondition: not stalled before anything has failed")
            .isFalse();

        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            try (Statement st = blocker.createStatement()) {
                st.execute("LOCK TABLE nexus.service_tokens IN SHARE MODE");
            }

            for (int cycle = 1; cycle <= NexusService.DATA_SWEEP_STALL_CYCLES; cycle++) {
                service.runScheduledSweep(now, Duration.ofMillis(500));
                assertThat(service.consecutiveDataSweepFailures())
                    .as("cycle %s failed to reap anything; the counter must record that, "
                        + "because the log line alone is what nobody reads", cycle)
                    .isEqualTo(cycle);
            }

            assertThat(service.dataSweepStalled())
                .as("at %s consecutive failed cycles the arm is not 'surviving', it is "
                    + "not reaping -- and the difference must be recoverable from state, "
                    + "not only inferable by someone already reading WARN lines",
                    NexusService.DATA_SWEEP_STALL_CYCLES)
                .isTrue();

            blocker.rollback();
        }

        // Recovery must clear it, or the flag latches and becomes noise that gets
        // blessed reflexively -- the failure mode on the other side of this fix.
        service.runScheduledSweep(now, Duration.ofMillis(500));
        assertThat(service.consecutiveDataSweepFailures())
            .as("one successful cycle resets the counter")
            .isEqualTo(0);
        assertThat(service.dataSweepStalled())
            .as("and clears the stalled state; a latched flag is a wolf-crier")
            .isFalse();
        } finally {
            deleteTokens(tenant);
        }
    }

    @Test
    @Timeout(value = 60, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void runScheduledSweep_boundsTenantEnumerationToo() throws Exception {
        // ACCESS EXCLUSIVE blocks even a plain SELECT, so it stalls the cycle at
        // listKnownTenants() -- BEFORE any arm runs, where no per-arm bound can
        // reach it. This is the hole the per-arm bound alone would have left.
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);

        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            try (Statement st = blocker.createStatement()) {
                st.execute("LOCK TABLE nexus.service_tokens IN ACCESS EXCLUSIVE MODE");
            }

            long startedAt = System.nanoTime();
            org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                service.runScheduledSweep(now, Duration.ofMillis(500)))
                .as("enumeration is bounded, so the cycle fails fast and the scheduler's "
                    + "own catch logs t1_scheduled_sweep_failed")
                .isInstanceOf(Exception.class);
            long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000;

            assertThat(elapsedMs).isLessThan(30_000);
            blocker.rollback();
        }
    }
}
