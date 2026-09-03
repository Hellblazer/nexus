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
}
