// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.sql.Connection;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * Waits for sessions to queue behind a lock a test holds on its own connection (nexus-u5m5g.9).
 *
 * <p>The connection that holds the lock is inside one long transaction, and PostgreSQL caches
 * {@code pg_stat_activity}'s backend-status snapshot for the length of the reader's transaction: a poll of the view
 * from that connection returns the state of its first read until the transaction ends or
 * {@code pg_stat_clear_snapshot()} is called. A wait that polled the view without clearing it saw "one blocked" for
 * the whole bound whenever a second session queued after the first read, and its failure dump showed the same stale
 * single row. So every read here clears the snapshot first. {@code pg_locks} is computed live and carries no such
 * cache, so the wait on a specific queued lock reads it instead.
 *
 * <p>The bound is progress-based, not a short fixed poll: the wait ends at once when a commit it watches has finished
 * (it was meant to be queued) and otherwise gives up only after {@link #BOUND_SECONDS}, with every non-idle backend and
 * every lock request that is not granted in the failure message.
 */
final class PgLockWait {

    /** Longest a wait runs. A queued session shows within milliseconds; this only has to outlast a starved CI box. */
    static final long BOUND_SECONDS = 120;

    private PgLockWait() {}

    /** Drops this connection's cached backend-status snapshot so the next read of {@code pg_stat_activity} is current. */
    static void clearSnapshot(DSLContext ctx) {
        ctx.select(DSL.function("pg_stat_clear_snapshot", Object.class)).fetch();
    }

    /** Waits until {@code n} other backends of this database wait on a lock; fails early if a watched commit ends first. */
    static void awaitBlocked(Connection su, int n, Future<?>... commits) throws Exception {
        DSLContext ctx = DSL.using(su, org.jooq.SQLDialect.POSTGRES);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(BOUND_SECONDS);
        while (true) {
            int blocked = PgContainerHelper.backendsBlockedOnLocks(ctx);   // drops the snapshot first
            if (blocked >= n) return;
            failIfEnded(ctx, "waiting for " + n + " blocked backend(s), " + blocked + " so far", commits);
            if (System.nanoTime() > deadline) {
                throw new AssertionError("fewer than " + n + " commits blocked on the held lock after " + BOUND_SECONDS
                    + " s: " + blocked + "\n" + dump(ctx, commits));
            }
            Thread.sleep(25);
        }
    }

    /**
     * Waits until some backend has requested an advisory lock it has not been granted. The reconcile commit's owner lock
     * is the only advisory lock a test of it holds, so this is the second run of an owner queued on the first
     * ({@code wait_event_type = 'Lock'} with {@code wait_event = 'advisory'} in {@code pg_stat_activity}).
     */
    static void awaitAdvisoryWaiter(Connection su, Future<?>... commits) throws Exception {
        DSLContext ctx = DSL.using(su, org.jooq.SQLDialect.POSTGRES);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(BOUND_SECONDS);
        while (true) {
            if (advisoryWaiters(ctx) > 0) return;
            failIfEnded(ctx, "waiting for a backend queued on the owner's advisory lock", commits);
            if (System.nanoTime() > deadline) {
                throw new AssertionError("no backend queued on the advisory lock after " + BOUND_SECONDS + " s\n"
                    + dump(ctx, commits));
            }
            Thread.sleep(25);
        }
    }

    /** How many lock requests are advisory and not granted, read live from {@code pg_locks}. */
    static int advisoryWaiters(DSLContext ctx) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("locktype"), String.class).eq("advisory")
                .and(DSL.field(DSL.name("granted"), Boolean.class).eq(false)));
    }

    private static void failIfEnded(DSLContext ctx, String waitingFor, Future<?>[] commits) {
        for (int i = 0; i < commits.length; i++) {
            if (commits[i].isDone()) {
                String how;
                try {
                    commits[i].get(0, TimeUnit.SECONDS);
                    how = "returned";
                } catch (Exception e) {
                    how = "threw " + (e.getCause() != null ? e.getCause() : e);
                }
                throw new AssertionError("commit #" + i + " ended while " + waitingFor + ": it " + how + "\n"
                    + dump(ctx, commits));
            }
        }
    }

    /** The commits' states, every non-idle backend, and every lock request not granted, for a failure message. */
    static String dump(DSLContext ctx, Future<?>... commits) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < commits.length; i++) sb.append("commit #").append(i).append(commits[i].isDone() ? " done" : " running").append('\n');
        clearSnapshot(ctx);
        sb.append("non-idle backends:\n").append(ctx.select(DSL.field(DSL.name("pid"), Integer.class),
                DSL.field(DSL.name("state"), String.class), DSL.field(DSL.name("wait_event_type"), String.class),
                DSL.field(DSL.name("wait_event"), String.class),
                DSL.function("pg_blocking_pids", Object.class, DSL.field(DSL.name("pid"), Integer.class)).cast(String.class).as("blocked_by"),
                DSL.left(DSL.field(DSL.name("query"), String.class), 90).as("query"))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("pid"), Integer.class).ne(DSL.function("pg_backend_pid", Integer.class))
                .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class)))
                .and(DSL.field(DSL.name("state"), String.class).ne("idle")))
            .fetch().format());
        sb.append("lock requests not granted:\n").append(ctx.select(DSL.field(DSL.name("pid"), Integer.class),
                DSL.field(DSL.name("locktype"), String.class), DSL.field(DSL.name("mode"), String.class),
                DSL.field(DSL.name("classid"), Long.class), DSL.field(DSL.name("objid"), Long.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
            .where(DSL.field(DSL.name("granted"), Boolean.class).eq(false))
            .fetch().format());
        return sb.toString();
    }
}
