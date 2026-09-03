/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-g17tf -- {@link PgSession#setSearchStatementTimeout} against a REAL
 * Postgres. Proven by CANCELLATION: a statement that genuinely runs past the
 * bound raises SQLSTATE 57014 ({@code query_canceled}). A fast statement
 * returning normally would not distinguish a working bound from a query that
 * was never slow, so the assertion is on the cancel, never on completion.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgSessionStatementTimeoutIntegrationTest {

    /** Postgres SQLSTATE for {@code query_canceled}, which statement_timeout raises. */
    static final String QUERY_CANCELED = "57014";

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
    void aStatementRunningPastTheBoundIsCancelledWith57014() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setSearchStatementTimeout(ctx, 200);
            assertThat(currentSetting(ctx)).isEqualTo("200ms");

            long started = System.nanoTime();
            assertThatThrownBy(() -> ctx.resultQuery("SELECT pg_sleep(30)").fetch())
                .isInstanceOf(DataAccessException.class)
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo(QUERY_CANCELED));
            long elapsedMs = (System.nanoTime() - started) / 1_000_000;
            // The cancel fired from the bound, not from a 30s sleep completing.
            assertThat(elapsedMs).isLessThan(10_000);
            c.rollback();
        }
    }

    @Test
    void boundIsTransactionLocalAndRevertsOnRollback() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            String before = currentSetting(ctx);
            PgSession.setSearchStatementTimeout(ctx, 30_000);
            assertThat(currentSetting(ctx)).isEqualTo("30s");
            c.rollback();
            assertThat(currentSetting(ctx)).isEqualTo(before);
        }
    }

    @Test
    void theEnvResolvedFormUsesTheServingBound() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setSearchStatementTimeout(ctx);
            // No NX_SEARCH_STATEMENT_TIMEOUT_MS in the test env: the 30s default lands.
            assertThat(currentSetting(ctx)).isEqualTo("30s");
            c.rollback();
        }
    }

    private static String currentSetting(DSLContext ctx) {
        return ctx.resultQuery("SELECT current_setting('statement_timeout')")
                  .fetchOne(0, String.class);
    }

    private static String sqlState(Throwable t) {
        for (Throwable cur = t; cur != null; cur = cur.getCause()) {
            if (cur instanceof SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return null;
    }

    /** nexus-6nkn3: the plan-cache mode lands for the transaction and reverts. */
    @Test
    void planCacheModeIsForcedCustomForTheTransactionAndRevertsOnRollback() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            String before = ctx.resultQuery("SELECT current_setting('plan_cache_mode')")
                               .fetchOne(0, String.class);
            assertThat(before).isEqualTo("auto");
            PgSession.setSearchPlanCacheMode(ctx);
            assertThat(ctx.resultQuery("SELECT current_setting('plan_cache_mode')")
                          .fetchOne(0, String.class)).isEqualTo("force_custom_plan");
            c.rollback();
            assertThat(ctx.resultQuery("SELECT current_setting('plan_cache_mode')")
                          .fetchOne(0, String.class)).isEqualTo(before);
        }
    }

    /**
     * nexus-6nkn3, the claim itself: the GUC governs the plan choice of a
     * pgjdbc SERVER-SIDE prepared statement executed past the promotion
     * threshold (prepareThreshold=5). Postgres counts each prepared
     * statement's plans in {@code pg_prepared_statements.generic_plans /
     * custom_plans}: under force_custom_plan the generic counter must stay
     * at zero; force_generic_plan is the control that proves the counter
     * moves, so a passing zero is not vacuous.
     */
    @Test
    void forceCustomPlanGovernsAServerSidePreparedStatementPastThePromotionThreshold()
            throws Exception {
        assertThat(planCounts("force_custom_plan")).satisfies(c -> {
            assertThat(c[0]).as("generic_plans under force_custom_plan").isZero();
            // pgjdbc runs the first four executions as UNNAMED statements (untracked)
            // and names the server-side statement from the fifth: only the
            // post-promotion executions appear here, and every one must be custom.
            assertThat(c[1]).as("custom_plans under force_custom_plan").isGreaterThanOrEqualTo(1);
        });
        assertThat(planCounts("force_generic_plan")[0])
            .as("generic_plans under force_generic_plan (control)")
            .isGreaterThan(0);
    }

    /** {generic_plans, custom_plans} for one statement executed 8x on one connection. */
    private long[] planCounts(String mode) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setLocal(ctx, "plan_cache_mode", mode);
            // Same PreparedStatement object, 8 executions: pgjdbc switches to a
            // named server-side statement at the 5th and Postgres tracks its plans.
            try (PreparedStatement ps = c.prepareStatement(
                     "SELECT count(*) FROM pg_class WHERE relname = ?")) {
                for (int i = 0; i < 8; i++) {
                    ps.setString(1, "pg_class");
                    try (ResultSet rs = ps.executeQuery()) {
                        rs.next();
                    }
                }
            }
            try (PreparedStatement q = c.prepareStatement(
                     "SELECT coalesce(sum(generic_plans),0), coalesce(sum(custom_plans),0)"
                     + " FROM pg_prepared_statements WHERE statement LIKE '%pg_class WHERE relname%'");
                 ResultSet rs = q.executeQuery()) {
                rs.next();
                long[] out = {rs.getLong(1), rs.getLong(2)};
                c.rollback();
                return out;
            }
        }
    }
}
