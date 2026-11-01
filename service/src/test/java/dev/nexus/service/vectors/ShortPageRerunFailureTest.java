// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
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
    void aRerunThatTimesOut_returnsTheFirstPage_andLeavesTheTransactionUsable() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            String timeoutBefore = setting(ctx, "statement_timeout");
            int[] calls = {0};
            Supplier<Result<Record1<Integer>>> fetch = () -> calls[0]++ == 0 ? oneRow(ctx) : slowRow(ctx);
            long before = PgVectorRepository.shortPageRerunCount();

            Result<Record1<Integer>> page = PgVectorRepository.exactOnUnderReturn(ctx, 5, fetch, () -> 100, true, "t", "c");

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
