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
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-6pfgb.61, review round 1: the failure path of the short-page exact re-run. The re-run repairs a page that is
 * already usable, so when it fails (a statement timeout on a very large collection is the likely cause) the caller
 * must get the first page back and its transaction must still work: the re-run runs under a savepoint, and a
 * failed statement inside a PostgreSQL transaction otherwise leaves every later statement refused ("current
 * transaction is aborted"), which would turn a partial answer into an error one statement later.
 *
 * <p>Runs against a real transaction: the first fetch answers one row, the second (the re-run) sleeps past a 100 ms
 * statement bound that the re-run's own rebind sets, as a fan-out arm's remaining budget does in production.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ShortPageRerunFailureTest {

    PostgreSQLContainer<?> pg;

    private static Result<Record1<Integer>> oneRow(DSLContext ctx) {
        return ctx.select(DSL.inline(1).as("x")).fetch();
    }

    /** One row, produced only after the server has slept three seconds (the statement bound cuts it short). */
    private static Result<Record1<Integer>> slowRow(DSLContext ctx) {
        return ctx.select(DSL.inline(1).as("x"))
                  .where(DSL.function("pg_sleep", Object.class, DSL.inline(3)).isNotNull())
                  .fetch();
    }

    private static Result<Record1<Integer>> divisionByZero(DSLContext ctx) {
        return ctx.select(DSL.field("1/0", Integer.class)).fetch();
    }

    private static String sqlStateOf(Throwable t) {
        for (int depth = 0; t != null && depth < 32; t = t.getCause(), depth++) {
            if (t instanceof java.sql.SQLException sql && sql.getSQLState() != null) {
                return sql.getSQLState();
            }
        }
        return null;
    }

    private static Result<Record1<Integer>> threeRows(DSLContext ctx) {
        return ctx.select(DSL.inline(1).as("x"))
                  .unionAll(DSL.select(DSL.inline(2)))
                  .unionAll(DSL.select(DSL.inline(3)))
                  .fetch();
    }

    private static String setting(DSLContext ctx, String name) {
        Field<String> f = DSL.function("current_setting", String.class, DSL.inline(name));
        return ctx.select(f).fetchSingle().value1();
    }

    @BeforeAll
    void start() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    @Test
    void aRerunThatTimesOutOutsideAFanOutArm_returnsTheFirstPage_andLeavesTheTransactionUsable() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            // No rebind supplier: a single search, whose bound was set once for the whole transaction (as the env
            // bound is). Its re-run repairs a usable page, so the page in hand is the answer if the repair times out.
            PgSession.setLocal(ctx, "statement_timeout", "100");
            String timeoutBefore = setting(ctx, "statement_timeout");
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : slowRow(ctx);
            long before = PgVectorRepository.shortPageRerunCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, null, true, "t", "c");

            assertThat(page).as("the short page already in hand").hasSize(1);
            assertThat(calls[0]).as("the re-run was attempted").isEqualTo(2);
            assertThat(PgVectorRepository.shortPageRerunCount() - before).as("a failed re-run is not counted").isZero();
            // The transaction is not aborted: a following statement on the same context succeeds ...
            assertThat(oneRow(ctx)).hasSize(1);
            // ... and the re-run's SET LOCALs went back with the savepoint, so the caller's bound is its own.
            assertThat(setting(ctx, "statement_timeout")).isEqualTo(timeoutBefore);
            assertThat(setting(ctx, "enable_indexscan")).isEqualTo("on");
            c.commit();
        }
    }

    @Test
    void aRerunThatTimesOutInAFanOutArm_isNotSwallowed() throws Exception {
        // nexus-8ptg4: a fan-out arm passes a rebind supplier, and the bound it sets is the arm's remaining budget. A
        // re-run the bound cancels (SQLSTATE 57014) means the arm ran out of time: it must fail with the cancel so the
        // fan-out marks the collection timed out, not hand back a short page that looks like a complete answer.
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : slowRow(ctx);

            long deadlinesBefore = PgVectorRepository.shortPageRerunDeadlineCount();
            long countDeadlinesBefore = PgVectorRepository.shortPageCountDeadlineCount();
            Throwable thrown = org.assertj.core.api.Assertions.catchThrowable(
                () -> PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> 100, true, "t", "c"));

            assertThat(thrown).as("the arm's deadline cancel is not swallowed").isNotNull();
            assertThat(sqlStateOf(thrown)).as("the statement timeout, as a fan-out arm settles it").isEqualTo("57014");
            assertThat(calls[0]).as("the re-run was attempted").isEqualTo(2);
            assertThat(PgVectorRepository.shortPageRerunDeadlineCount() - deadlinesBefore).isEqualTo(1);
            assertThat(PgVectorRepository.shortPageCountDeadlineCount() - countDeadlinesBefore)
                .as("a re-run cancel is not counted as a count cancel").isZero();
            c.rollback();
        }
    }

    @Test
    void aRebindThatRefusesTheRerun_propagatesAsTheBudgetShape() throws Exception {
        // The rebind supplier throws when the arm's budget is already spent (armBound). That refusal is the deadline
        // itself, in its typed shape; the fan-out maps it to the request or fan-out failure. Swallowing it returned
        // the short page as if the budget had held.
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            Supplier<Result<Record1<Integer>>> fetch = () -> oneRow(ctx);

            assertThatThrownBy(() -> PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> {
                throw new PgVectorRepository.FanoutBudgetSpentException("spent");
            }, true, "t", "c")).isInstanceOf(PgVectorRepository.FanoutBudgetSpentException.class);
            assertThatThrownBy(() -> PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> {
                throw new RequestDeadlineExceededException("spent", 1);
            }, true, "t", "c")).isInstanceOf(RequestDeadlineExceededException.class);
            c.rollback();
        }
    }

    @Test
    void aRerunThatFailsForAnotherReason_inAFanOutArm_stillReturnsTheFirstPage() throws Exception {
        // Only the deadline is the arm's failure. A re-run that fails some other way is still a failed repair of a
        // usable page.
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : divisionByZero(ctx);

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> 5_000, true, "t", "c");

            assertThat(page).hasSize(1);
            assertThat(oneRow(ctx)).as("the transaction is usable").hasSize(1);
            c.commit();
        }
    }

    @Test
    void aShortPageTheManifestCountShowsComplete_isNotRerun() throws Exception {
        // nexus-8ptg4: the count is at most the rows already on the page, so nothing qualifying is missing.
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            int[] capSeen = {-1};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long reruns = PgVectorRepository.shortPageRerunCount();
            long skips = PgVectorRepository.shortPageSkipCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, null, true, "t", "c",
                cap -> {
                    capSeen[0] = cap;
                    return 1;
                });

            assertThat(page).as("the page in hand").hasSize(1);
            assertThat(calls[0]).as("no second fetch").isEqualTo(1);
            assertThat(capSeen[0]).as("the count is bounded at the page plus one").isEqualTo(2);
            assertThat(PgVectorRepository.shortPageRerunCount() - reruns).isZero();
            assertThat(PgVectorRepository.shortPageSkipCount() - skips).isEqualTo(1);
            assertThat(oneRow(ctx)).as("the transaction is usable").hasSize(1);
            c.commit();
        }
    }

    @Test
    void aShortPageTheManifestCountShowsIncomplete_isRerun() throws Exception {
        // The count is one above the page: a qualifying chunk is not on it (the scan cap cut the walk short).
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long reruns = PgVectorRepository.shortPageRerunCount();
            long skips = PgVectorRepository.shortPageSkipCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, null, true, "t", "c",
                cap -> cap);

            assertThat(page).hasSize(3);
            assertThat(PgVectorRepository.shortPageRerunCount() - reruns).isEqualTo(1);
            assertThat(PgVectorRepository.shortPageSkipCount() - skips).isZero();
            c.commit();
        }
    }

    @Test
    void aCountThatFails_leavesTheRerunToRun() throws Exception {
        // An unknown count is not a reason to skip a repair: the failed statement is rolled back to the savepoint and
        // the page is re-run.
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, null, true, "t", "c",
                cap -> divisionByZero(ctx).size());

            assertThat(page).as("the exact page, not the short one").hasSize(3);
            assertThat(oneRow(ctx)).as("the transaction is usable").hasSize(1);
            c.commit();
        }
    }

    @Test
    void aCountThatRunsIntoAFanOutArmsBound_isNotSwallowed() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);

            long countDeadlinesBefore = PgVectorRepository.shortPageCountDeadlineCount();
            long rerunDeadlinesBefore = PgVectorRepository.shortPageRerunDeadlineCount();
            Throwable thrown = org.assertj.core.api.Assertions.catchThrowable(
                () -> PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> 100, true, "t", "c",
                    cap -> slowRow(ctx).size()));

            assertThat(sqlStateOf(thrown)).isEqualTo("57014");
            assertThat(calls[0]).as("no re-run after the arm's deadline").isEqualTo(1);
            assertThat(PgVectorRepository.shortPageCountDeadlineCount() - countDeadlinesBefore)
                .as("the count met the bound").isEqualTo(1);
            assertThat(PgVectorRepository.shortPageRerunDeadlineCount() - rerunDeadlinesBefore)
                .as("a count cancel is not counted as a re-run cancel").isZero();
            c.rollback();
        }
    }

    @Test
    void aCountThatFailsAndWhoseRollbackFailsToo_surfacesTheRollbackFailureCarryingTheCountsOwn() throws Exception {
        // nexus-8ptg4 review: when the count fails and the rollback of its savepoint fails as well, the count's own
        // failure must not vanish behind the rollback's. Closing the connection inside the count makes both fail.
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            Supplier<Result<Record1<Integer>>> fetch = () -> oneRow(ctx);
            IllegalStateException countFailure = new IllegalStateException("the count's own failure");

            Throwable thrown = org.assertj.core.api.Assertions.catchThrowable(
                () -> PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, null, true, "t", "c", cap -> {
                    try {
                        c.close();
                    } catch (java.sql.SQLException e) {
                        throw new IllegalStateException(e);
                    }
                    throw countFailure;
                }));

            assertThat(thrown).as("the rollback failure escapes").isNotNull().isNotSameAs(countFailure);
            assertThat(thrown.getSuppressed()).as("the count's own failure is not lost").contains(countFailure);
        }
    }

    @Test
    void aRerunThatSucceeds_returnsTheLargerExactPage() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : threeRows(ctx);
            long before = PgVectorRepository.shortPageRerunCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> 5_000, true, "t", "c");

            assertThat(page).hasSize(3);
            assertThat(PgVectorRepository.shortPageRerunCount() - before).isEqualTo(1);
            c.commit();
        }
    }
}
