/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.47 -- {@link PgSession#startupScanBudget} against a REAL pgvector Postgres:
 * it reads the role's effective {@code work_mem}, derives the multiplier that holds the
 * 16 MB budget, and {@link PgSession#setHnswScanBudget} lands both values as the extension's
 * GUCs for the transaction. (That the values revert with the transaction, and that the
 * extension honours them, is pinned by the per-path test and its no-leak case.)
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgSessionScanBudgetIntegrationTest {

    private static final long MB = 1024L * 1024L;

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
    }

    @AfterEach
    void reset() {
        PgSession.resetScanBudgetForTests();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void stockWorkMemGivesMultiplier4_andLargeWorkMemGivesMultiplier1() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            setSession(ctx, "work_mem", "4MB");
            PgSession.ScanBudget local = PgSession.startupScanBudget(ctx);
            assertThat(local.workMemBytes()).isEqualTo(4 * MB);
            assertThat(local.budgetBytes()).isEqualTo(16 * MB);
            assertThat(local.memMultiplier()).isEqualTo(4);
            assertThat(local.effectiveMemBytes()).isEqualTo(16 * MB);
            assertThat(local.maxScanTuples()).isEqualTo(200_000);

            PgSession.setHnswScanBudget(ctx);
            assertThat(setting(ctx, "hnsw.max_scan_tuples")).isEqualTo("200000");
            assertThat(setting(ctx, "hnsw.scan_mem_multiplier")).isEqualTo("4");
            c.rollback();

            // The cloud shape: work_mem already far above the budget.
            setSession(ctx, "work_mem", "384MB");
            PgSession.ScanBudget cloud = PgSession.startupScanBudget(ctx);
            assertThat(cloud.workMemBytes()).isEqualTo(384 * MB);
            assertThat(cloud.memMultiplier()).isEqualTo(1);
            assertThat(cloud.effectiveMemBytes()).isEqualTo(384 * MB);
            PgSession.setHnswScanBudget(ctx);
            assertThat(setting(ctx, "hnsw.scan_mem_multiplier")).isEqualTo("1");
            c.rollback();
        }
    }

    @Test
    void theFirstSearchResolvesTheBudgetLazilyWhenBootNeverRan() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            assertThat(PgSession.currentScanBudget()).isNull();
            PgSession.setHnswScanBudget(ctx);
            assertThat(PgSession.currentScanBudget()).isNotNull();
            assertThat(setting(ctx, "hnsw.scan_mem_multiplier")).isEqualTo("4");
            c.rollback();
        }
    }

    private static void setSession(DSLContext ctx, String guc, String value) {
        ctx.select(DSL.function("set_config", String.class, DSL.val(guc), DSL.val(value), DSL.inline(false)))
            .fetch();
    }

    private static String setting(DSLContext ctx, String guc) {
        return ctx.select(DSL.function("current_setting", String.class, DSL.val(guc))).fetchSingle().value1();
    }
}
