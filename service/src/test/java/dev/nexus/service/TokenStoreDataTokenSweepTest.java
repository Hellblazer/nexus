package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.SweepBounds;
import dev.nexus.service.db.TokenStore;
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
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.sql.Types;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-lgiqw — {@link TokenStore#sweepExpiredDataTokens(String, Instant, Duration)},
 * the reaper for {@code scope='data'} rows left behind by the JIT mint path.
 *
 * <p>Measured 2026-08-25 on the live estate: 14,308 {@code scope=data} rows, of which
 * 14,307 already expired, accruing ~313/day. The producer is the EDGE, not the client
 * — {@code EngineDataTokenCache.getOrMint} writes a row per (tenant, TTL window)
 * whenever JIT is on, and nothing ever deleted one. Steady state after this ships is
 * ~80 rows per 6h cycle, which is why the sweep is a single unbatched DELETE with no
 * statement timeout, exactly like its sibling {@code sweepExpiredSessions}.
 *
 * <p>THE LOAD-BEARING TEST HERE IS {@link #sweepExpiredDataTokens_neverTouchesANonDataScope}.
 * root / tenant / mint / mint-locked are long-lived operator artifacts; {@code
 * mint-locked} in particular is the production credential provisioned 2026-08-16,
 * which carries {@code expires_at IS NULL} precisely so it cannot age out. Two of
 * those scopes would be excluded by the NULL comparison alone, but the safety of a
 * production credential must not rest on NULL semantics — hence an explicit scope
 * filter, and a test that fails if someone deletes it.
 *
 * <p>Hermetic: Testcontainers Postgres + the real Liquibase chain, {@link TokenStore}
 * exercised directly (no HTTP, no scheduler), mirroring {@code
 * TokenStoreSessionSweepTest}. {@code service_tokens} carries no RLS (see the
 * baseline changeset's header), so the connecting role is the plain superuser as in
 * every other direct {@link TokenStore} test in this package.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TokenStoreDataTokenSweepTest {

    /** The production grace window; see SWEEP_DATA_TOKEN_GRACE_DAYS in NexusService. */
    private static final Duration GRACE = Duration.ofDays(7);

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TokenStore store;

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
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(pg.getUsername());
        config.setPassword(pg.getPassword());
        config.setMaximumPoolSize(4);
        ds = new HikariDataSource(config);
        store = new TokenStore(ds, Clock.systemUTC());
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** @param expiresAt may be null, which is how root and mint-locked rows are stored. */
    private void insertToken(String tenant, String label, String scope, Instant expiresAt)
            throws Exception {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, scope, expires_at) "
                 + "VALUES (?, ?, ?, ?, ?)")) {
            ps.setString(1, TokenHashing.sha256Hex(tenant + ":" + label + ":" + scope + ":" + expiresAt));
            ps.setString(2, tenant);
            ps.setString(3, label);
            ps.setString(4, scope);
            if (expiresAt == null) {
                ps.setNull(5, Types.TIMESTAMP_WITH_TIMEZONE);
            } else {
                ps.setObject(5, OffsetDateTime.ofInstant(expiresAt, ZoneOffset.UTC));
            }
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

    // ----------------------------------------------------------------------
    // the grace window
    // ----------------------------------------------------------------------

    @Test
    void sweepExpiredDataTokens_deletesRowsPastGrace_keepsRowsInsideIt() throws Exception {
        String tenant = "sweep-data-" + System.nanoTime();
        Instant now = Instant.now();

        insertToken(tenant, "long-expired", "data", now.minus(Duration.ofDays(30)));
        insertToken(tenant, "just-past-grace", "data", now.minus(Duration.ofDays(7)).minusSeconds(60));
        insertToken(tenant, "inside-grace", "data", now.minus(Duration.ofDays(3)));
        insertToken(tenant, "not-yet-expired", "data", now.plusSeconds(300));

        int deleted = store.sweepExpiredDataTokens(tenant, now, GRACE);

        assertThat(deleted)
            .as("only rows more than the grace window past expiry are eligible")
            .isEqualTo(2);
        assertThat(tokenExists(tenant, "long-expired")).isFalse();
        assertThat(tokenExists(tenant, "just-past-grace")).isFalse();
        assertThat(tokenExists(tenant, "inside-grace"))
            .as("a row expired 3 days ago is still inside the 7-day window: the CloudWatch "
                + "mint log must outlive the row, and that log is the only other record")
            .isTrue();
        assertThat(tokenExists(tenant, "not-yet-expired"))
            .as("a live token must never be swept")
            .isTrue();
    }

    @Test
    void sweepExpiredDataTokens_rowExactlyAtTheGraceBoundary_survives() throws Exception {
        String tenant = "sweep-data-boundary-" + System.nanoTime();
        Instant now = Instant.now();
        // expires_at == cutoff exactly. The predicate is strictly-less-than, so this
        // row is retained; pinned so a later `<=` is a deliberate change, not a drift.
        insertToken(tenant, "exactly-at-cutoff", "data", now.minus(GRACE));

        int deleted = store.sweepExpiredDataTokens(tenant, now, GRACE);

        assertThat(deleted).isEqualTo(0);
        assertThat(tokenExists(tenant, "exactly-at-cutoff")).isTrue();
    }

    // ----------------------------------------------------------------------
    // the scope guard -- the reason this test class exists
    // ----------------------------------------------------------------------

    @ParameterizedTest
    @ValueSource(strings = {"root", "tenant", "mint", "mint-locked"})
    void sweepExpiredDataTokens_neverTouchesANonDataScope(String scope) throws Exception {
        String tenant = "sweep-data-scope-" + System.nanoTime();
        Instant now = Instant.now();
        // Expired far past the grace window: the ONLY thing standing between this row
        // and deletion is the scope filter. If someone removes that filter, this fails.
        insertToken(tenant, "operator-artifact", scope, now.minus(Duration.ofDays(365)));
        insertToken(tenant, "sweepable", "data", now.minus(Duration.ofDays(365)));

        int deleted = store.sweepExpiredDataTokens(tenant, now, GRACE);

        assertThat(deleted)
            .as("only the data row is eligible; the %s row is an operator artifact", scope)
            .isEqualTo(1);
        assertThat(tokenExists(tenant, "operator-artifact"))
            .as("scope=%s must survive the reaper even when long expired -- root, tenant, "
                + "mint and mint-locked are long-lived credentials, and mint-locked is the "
                + "production credential provisioned 2026-08-16", scope)
            .isTrue();
        assertThat(tokenExists(tenant, "sweepable")).isFalse();
    }

    @Test
    void sweepExpiredDataTokens_nullExpiryNeverQualifies() throws Exception {
        String tenant = "sweep-data-null-" + System.nanoTime();
        Instant now = Instant.now();
        // How root and mint-locked are actually stored: no expiry, so they cannot age
        // out. NULL < cutoff is NULL, never true -- but the scope filter above is what
        // we RELY on. This pins the second, independent reason they are safe.
        insertToken(tenant, "never-expires", "mint-locked", null);
        insertToken(tenant, "data-no-expiry", "data", null);

        int deleted = store.sweepExpiredDataTokens(tenant, now, GRACE);

        assertThat(deleted).isEqualTo(0);
        assertThat(tokenExists(tenant, "never-expires")).isTrue();
        assertThat(tokenExists(tenant, "data-no-expiry"))
            .as("even a data row with no expiry has no cutoff to be past, so it is not swept")
            .isTrue();
    }

    // ----------------------------------------------------------------------
    // isolation, non-vacuity, degenerate input
    // ----------------------------------------------------------------------

    @Test
    void sweepExpiredDataTokens_doesNotAffectOtherTenants() throws Exception {
        String tenantA = "sweep-data-a-" + System.nanoTime();
        String tenantB = "sweep-data-b-" + System.nanoTime();
        Instant now = Instant.now();

        insertToken(tenantA, "a-expired", "data", now.minus(Duration.ofDays(30)));
        insertToken(tenantB, "b-expired", "data", now.minus(Duration.ofDays(30)));

        int deleted = store.sweepExpiredDataTokens(tenantA, now, GRACE);

        assertThat(deleted).isEqualTo(1);
        assertThat(tokenExists(tenantA, "a-expired")).isFalse();
        assertThat(tokenExists(tenantB, "b-expired"))
            .as("a per-tenant sweep must never reach across tenants even though "
                + "service_tokens carries no RLS")
            .isTrue();
    }

    @Test
    void sweepExpiredDataTokens_nothingEligible_returnsZero_leavesTableUntouched() throws Exception {
        String tenant = "sweep-data-empty-" + System.nanoTime();
        Instant now = Instant.now();
        insertToken(tenant, "still-live", "data", now.plusSeconds(300));

        int deleted = store.sweepExpiredDataTokens(tenant, now, GRACE);

        assertThat(deleted)
            .as("a cycle that swept nothing must report zero, not be indistinguishable "
                + "from a cycle that did not run")
            .isEqualTo(0);
        assertThat(tokenExists(tenant, "still-live")).isTrue();
    }

    @Test
    void sweepExpiredDataTokens_blankOrNullTenant_isNoOp() {
        assertThat(store.sweepExpiredDataTokens(null, Instant.now(), GRACE)).isEqualTo(0);
        assertThat(store.sweepExpiredDataTokens("", Instant.now(), GRACE)).isEqualTo(0);
        assertThat(store.sweepExpiredDataTokens("   ", Instant.now(), GRACE)).isEqualTo(0);
    }

    @Test
    void sweepExpiredDataTokens_nullGrace_isRefused() throws Exception {
        String tenant = "sweep-data-nograce-" + System.nanoTime();
        insertToken(tenant, "long-expired", "data", Instant.now().minus(Duration.ofDays(365)));

        // A null grace must not silently degrade to "no grace at all", which would
        // delete rows the CloudWatch mint log has not yet outlived.
        assertThat(store.sweepExpiredDataTokens(tenant, Instant.now(), null)).isEqualTo(0);
        assertThat(tokenExists(tenant, "long-expired")).isTrue();
    }

    // ----------------------------------------------------------------------
    // the statement bound -- proving the ceiling exists, not just that it is set
    // ----------------------------------------------------------------------

    @Test
    @Timeout(value = 60, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void sweepExpiredDataTokens_bounded_abortsInsteadOfBlockingForever() throws Exception {
        String tenant = "sweep-data-blocked-" + System.nanoTime();
        Instant now = Instant.now();
        insertToken(tenant, "long-expired", "data", now.minus(Duration.ofDays(30)));

        // SHARE MODE conflicts with the ROW EXCLUSIVE a DELETE takes, but NOT with
        // the ACCESS SHARE a SELECT takes -- so this blocks the sweep's delete and
        // nothing else. Held open in an uncommitted transaction on its own
        // connection: without a statement_timeout the sweep would wait forever.
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            try (Statement st = blocker.createStatement()) {
                st.execute("LOCK TABLE nexus.service_tokens IN SHARE MODE");
            }

            long startedAt = System.nanoTime();
            assertThatThrownBy(() ->
                store.sweepExpiredDataTokens(tenant, now, GRACE, Duration.ofMillis(500)))
                .as("a blocked DELETE must be cancelled by statement_timeout, not waited on")
                .hasRootCauseInstanceOf(org.postgresql.util.PSQLException.class);
            long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000;

            assertThat(elapsedMs)
                .as("it must abort near its 500ms ceiling; without the bound this call "
                    + "never returns at all and the only symptom is a hung sweep thread")
                .isLessThan(15_000);

            blocker.rollback();
        }

        assertThat(tokenExists(tenant, "long-expired"))
            .as("an aborted sweep abandons nothing -- the row waits for the next cycle, "
                + "which is what makes cancelling it a safe trade")
            .isTrue();
    }

    @Test
    void sweepExpiredDataTokens_unbounded_isStillTheDefault() throws Exception {
        // The three-arg overload keeps the pre-bound behaviour, so callers outside
        // the scheduled task are unaffected. Asserted on the happy path only: the
        // blocking case cannot be tested here, because it would not terminate.
        String tenant = "sweep-data-unbounded-" + System.nanoTime();
        Instant now = Instant.now();
        insertToken(tenant, "long-expired", "data", now.minus(Duration.ofDays(30)));

        assertThat(store.sweepExpiredDataTokens(tenant, now, GRACE)).isEqualTo(1);
    }

    @Test
    void applyStatementTimeout_actuallySetsTheSetting() {
        // Direct, fast, and falsifiable in the way the blocked-DELETE tests are not:
        // if applyStatementTimeout ever becomes a no-op, those tests stop FAILING and
        // start HANGING, which is a worse signal than red. This one just reads the
        // setting back and goes red in milliseconds.
        String inside = org.jooq.impl.DSL.using(ds, org.jooq.SQLDialect.POSTGRES)
            .transactionResult(cfg -> {
                org.jooq.DSLContext tx = org.jooq.impl.DSL.using(cfg);
                SweepBounds.applyStatementTimeout(tx, Duration.ofMillis(1234));
                return tx.fetchValue("select current_setting('statement_timeout')").toString();
            });
        assertThat(inside)
            .as("the bound must actually reach the session, not merely be passed around")
            .isEqualTo("1234ms");
    }

    @Test
    void applyStatementTimeout_isTransactionLocal_andDoesNotLeakToTheNextBorrower() {
        // is_local=true is load-bearing: these connections go back to a shared pool,
        // and a session-level timeout set here would silently bound unrelated
        // request-path work for whoever borrows the connection next.
        org.jooq.impl.DSL.using(ds, org.jooq.SQLDialect.POSTGRES).transaction(cfg ->
            SweepBounds.applyStatementTimeout(org.jooq.impl.DSL.using(cfg), Duration.ofMillis(1234)));

        String after = org.jooq.impl.DSL.using(ds, org.jooq.SQLDialect.POSTGRES)
            .fetchValue("select current_setting('statement_timeout')").toString();
        assertThat(after)
            .as("the timeout must not survive its transaction")
            .isNotEqualTo("1234ms");
    }
}
