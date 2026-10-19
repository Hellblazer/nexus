/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.CloseableResultQuery;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record1;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.math.BigDecimal;

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
            assertThatThrownBy(() -> ctx.select(DSL.function("pg_sleep", Object.class, DSL.inline(30))).fetch())
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
        return currentSetting(ctx, "statement_timeout");
    }

    private static String currentSetting(DSLContext ctx, String name) {
        return ctx.select(DSL.function("current_setting", String.class, DSL.val(name)))
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
            String before = currentSetting(ctx, "plan_cache_mode");
            assertThat(before).isEqualTo("auto");
            PgSession.setSearchPlanCacheMode(ctx);
            assertThat(currentSetting(ctx, "plan_cache_mode")).isEqualTo("force_custom_plan");
            c.rollback();
            assertThat(currentSetting(ctx, "plan_cache_mode")).isEqualTo(before);
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

    private static final Field<String> RELNAME = DSL.field(DSL.name("relname"), String.class);
    private static final Table<?> PG_CLASS = DSL.table(DSL.name("pg_catalog", "pg_class"));
    private static final Table<?> PG_PREPARED_STATEMENTS = DSL.table(DSL.name("pg_prepared_statements"));
    private static final Field<String> STATEMENT = DSL.field(DSL.name("statement"), String.class);
    private static final Field<Long> GENERIC_PLANS = DSL.field(DSL.name("generic_plans"), Long.class);
    private static final Field<Long> CUSTOM_PLANS = DSL.field(DSL.name("custom_plans"), Long.class);

    /** {generic_plans, custom_plans} for one statement executed 8x on one connection. */
    private long[] planCounts(String mode) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setLocal(ctx, "plan_cache_mode", mode);
            // keepStatement(true) makes jOOQ hold ONE JDBC PreparedStatement across the 8 fetches (jOOQ's
            // default is prepare-execute-close each time, which would not be the same object): pgjdbc
            // switches to a named server-side statement at the 5th execution of it and Postgres tracks its
            // plans. The statement is found in pg_prepared_statements by the exact text jOOQ rendered, with
            // pgjdbc's ? -> $1 placeholder rewrite applied.
            String sent;
            try (CloseableResultQuery<Record1<Integer>> q = ctx.selectCount().from(PG_CLASS).where(RELNAME.eq("pg_class")).keepStatement(true)) {
                sent = ctx.render(q).replace("?", "$1");
                for (int i = 0; i < 8; i++) {
                    q.fetch();
                }
            }
            var counts = ctx.select(DSL.coalesce(DSL.sum(GENERIC_PLANS), BigDecimal.ZERO),
                                    DSL.coalesce(DSL.sum(CUSTOM_PLANS), BigDecimal.ZERO))
                .from(PG_PREPARED_STATEMENTS)
                .where(STATEMENT.eq(sent))
                .fetchOne();
            long[] out = {counts.value1().longValue(), counts.value2().longValue()};
            c.rollback();
            return out;
        }
    }
}
