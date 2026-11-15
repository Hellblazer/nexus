// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record1;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.function.IntUnaryOperator;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;

/**
 * nexus-h7kw2: the short page's manifest count runs under its own time budget. A count the budget cancels answers
 * "unknown" and the page re-runs, in a single search and in a fan-out arm with room left; the bound in force before
 * the count is back afterwards; and an arm whose own remaining bound is the smaller one still fails with its deadline.
 *
 * <p>The count here is a stand-in that sleeps three seconds on the server, as the 13 s unanalyzed shape measured in
 * {@link ShortPageCountMeasurementTest} does in production; the budget is pinned at 100 ms.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ShortPageCountBudgetTest {

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    /** A fixed network-bound margin, so the network timeout a bound implies is known: bound + 5 s. */
    private static final int MARGIN_MS = 5_000;

    @BeforeEach
    void pinMargin() {
        PgSession.setNetworkBoundMarginMsForTests(MARGIN_MS);
    }

    @AfterEach
    void resetBudget() {
        ShortPageCountBudget.overrideBudgetMsForTests(0);
        PgSession.setNetworkBoundMarginMsForTests(-1);
    }

    private static Result<Record1<Integer>> oneRow(DSLContext ctx) {
        return ctx.select(DSL.inline(1).as("x")).fetch();
    }

    private static Result<Record1<Integer>> noRows(DSLContext ctx) {
        return ctx.select(DSL.inline(1).as("x")).where(DSL.falseCondition()).fetch();
    }

    private static Result<Record1<Integer>> threeRows(DSLContext ctx) {
        return ctx.select(DSL.inline(1).as("x"))
                  .unionAll(DSL.select(DSL.inline(2)))
                  .unionAll(DSL.select(DSL.inline(3)))
                  .fetch();
    }

    /** A count that sleeps three seconds on the server, then claims more chunks than any page here holds. */
    private static IntUnaryOperator slowCount(DSLContext ctx) {
        return cap -> {
            ctx.select(DSL.function("pg_sleep", Object.class, DSL.inline(3))).fetch();
            return Integer.MAX_VALUE - 1;
        };
    }

    private static String setting(DSLContext ctx, String name) {
        Field<String> f = DSL.function("current_setting", String.class, DSL.inline(name));
        return ctx.select(f).fetchSingle().value1();
    }

    private static String sqlStateOf(Throwable t) {
        for (int depth = 0; t != null && depth < 32; t = t.getCause(), depth++) {
            if (t instanceof java.sql.SQLException sql && sql.getSQLState() != null) {
                return sql.getSQLState();
            }
        }
        return null;
    }

    @Test
    void aSlowCountInASingleSearch_spendsItsBudget_andThePageReRuns_underTheCallersOwnBound() throws Exception {
        ShortPageCountBudget.overrideBudgetMsForTests(100);
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setLocal(ctx, "statement_timeout", "30000");
            String before = setting(ctx, "statement_timeout");
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long spent = ShortPageCountBudget.spentCount();
            long reruns = PgVectorRepository.shortPageRerunCount();
            long start = System.nanoTime();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(
                ctx, 5, fetch, null, true, "t", "c", slowCount(ctx));

            long ms = (System.nanoTime() - start) / 1_000_000;
            assertThat(page).as("the re-run's page").hasSize(3);
            assertThat(ms).as("the count stopped at its budget, not after its 3 s").isLessThan(2_000);
            assertThat(ShortPageCountBudget.spentCount() - spent).isEqualTo(1);
            assertThat(PgVectorRepository.shortPageRerunCount() - reruns).isEqualTo(1);
            assertThat(setting(ctx, "statement_timeout")).as("the caller's bound is back").isEqualTo(before);
            // A savepoint rollback puts the GUC back but not the socket bound; only restore does that.
            assertThat(c.getNetworkTimeout()).as("the caller's network bound is back").isEqualTo(30_000 + MARGIN_MS);
            c.commit();
        }
    }

    @Test
    void aSlowCountOnAnEmptyPage_spendsItsBudget_andThePageReRuns() throws Exception {
        ShortPageCountBudget.overrideBudgetMsForTests(100);
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            String before = setting(ctx, "statement_timeout");
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? noRows(ctx) : threeRows(ctx);
            long spent = ShortPageCountBudget.spentCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(
                ctx, 5, fetch, null, true, "t", "c", slowCount(ctx));

            assertThat(page).as("an unknown count does not read as an empty manifest").hasSize(3);
            assertThat(ShortPageCountBudget.spentCount() - spent).isEqualTo(1);
            assertThat(setting(ctx, "statement_timeout")).isEqualTo(before);
            assertThat(c.getNetworkTimeout()).as("no bound before, so no network bound after").isZero();
            c.commit();
        }
    }

    @Test
    void aSlowCountInAnArmWithRoomLeft_isTheBudgetsCancel_notTheArmsDeadline() throws Exception {
        ShortPageCountBudget.overrideBudgetMsForTests(100);
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long countDeadlines = PgVectorRepository.shortPageCountDeadlineCount();
            long spent = ShortPageCountBudget.spentCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(
                ctx, 5, fetch, () -> 10_000, true, "t", "c", slowCount(ctx));

            assertThat(page).hasSize(3);
            assertThat(PgVectorRepository.shortPageCountDeadlineCount() - countDeadlines).isZero();
            assertThat(ShortPageCountBudget.spentCount() - spent).isEqualTo(1);
            c.rollback();
        }
    }

    @Test
    void aSlowCountInAnArmWhoseBoundIsSmaller_failsWithTheArmsDeadline() throws Exception {
        ShortPageCountBudget.overrideBudgetMsForTests(5_000);
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long countDeadlines = PgVectorRepository.shortPageCountDeadlineCount();
            long spent = ShortPageCountBudget.spentCount();

            Throwable thrown = catchThrowable(() -> PgVectorRepository.exactOnUnderReturn(
                ctx, 5, fetch, () -> 100, true, "t", "c", slowCount(ctx)));

            assertThat(sqlStateOf(thrown)).isEqualTo("57014");
            assertThat(PgVectorRepository.shortPageCountDeadlineCount() - countDeadlines).isEqualTo(1);
            assertThat(ShortPageCountBudget.spentCount() - spent).isZero();
            c.rollback();
        }
    }

    @Test
    void aFastCount_putsTheCallersBoundBack() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setLocal(ctx, "statement_timeout", "30000");
            Supplier<Result<Record1<Integer>>> fetch = () -> oneRow(ctx);

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(
                ctx, 5, fetch, null, true, "t", "c", cap -> 1);

            assertThat(page).as("the count says the page is complete").hasSize(1);
            assertThat(setting(ctx, "statement_timeout")).isEqualTo("30s");
            assertThat(c.getNetworkTimeout()).isEqualTo(30_000 + MARGIN_MS);
            c.commit();
        }
    }

    @Test
    void aFastCount_withNoBoundBefore_leavesNone() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            assertThat(setting(ctx, "statement_timeout")).isEqualTo("0");
            Supplier<Result<Record1<Integer>>> fetch = () -> oneRow(ctx);

            PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, null, true, "t", "c", cap -> 1);

            assertThat(setting(ctx, "statement_timeout")).isEqualTo("0");
            assertThat(c.getNetworkTimeout()).isZero();
            c.commit();
        }
    }

    @Test
    void anArmBoundEqualToTheBudget_isTheArmsDeadline() throws Exception {
        // A tie is read as the arm's bound: its cancel propagates, never absorbed as "unknown".
        ShortPageCountBudget.overrideBudgetMsForTests(100);
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long countDeadlines = PgVectorRepository.shortPageCountDeadlineCount();
            long spent = ShortPageCountBudget.spentCount();

            Throwable thrown = catchThrowable(() -> PgVectorRepository.exactOnUnderReturn(
                ctx, 5, fetch, () -> 100, true, "t", "c", slowCount(ctx)));

            assertThat(sqlStateOf(thrown)).isEqualTo("57014");
            assertThat(PgVectorRepository.shortPageCountDeadlineCount() - countDeadlines).isEqualTo(1);
            assertThat(ShortPageCountBudget.spentCount() - spent).isZero();
            c.rollback();
        }
    }
}
