// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * {@link PgLockWait} (nexus-u5m5g.9): a second session that queues on a lock after the holder's connection has already
 * read {@code pg_stat_activity} is seen, and a commit that ends while it should be queued fails the wait at once with the
 * lock state. The queued sessions here are plain connections on a plain table, so nothing but the wait's own reads can
 * refresh the holder's cached view of the activity table, and each queued session opens its connection after the holder's
 * first read: before the fix the second wait never saw its second session.
 */
class PgLockWaitTest extends CatalogReconcileTestBase {

    private static final Table<?> T = DSL.table(DSL.name("lockwait_t"));
    private static final Field<Integer> ID = DSL.field(DSL.name("id"), Integer.class);

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_lockwait", "svc_lockwait_pass");
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.createTable(T).column(ID, SQLDataType.INTEGER.notNull()).primaryKey(ID).execute();
            ctx.insertInto(T, ID).values(1).execute();
        }
        connect("svc_lockwait", "svc_lockwait_pass");
    }

    /** A session that waits for the row lock the test's connection holds; it is released when that transaction ends. */
    private Future<?> queuedOnRow(java.util.concurrent.ExecutorService queue) {
        return queue.submit(() -> {
            try (Connection c = pg.createConnection("")) {
                lockRow(c);
            }
            return null;
        });
    }

    private Connection holdRow() throws Exception {
        Connection su = pg.createConnection("");
        su.setAutoCommit(false);
        lockRow(su);
        return su;
    }

    /** Takes, or queues for, the row lock on the table's one row. */
    private static void lockRow(Connection c) {
        DSL.using(c, SQLDialect.POSTGRES).select(ID).from(T).where(ID.eq(1)).forUpdate().fetch();
    }

    @Test
    void aSessionQueuedAfterTheFirstRead_isSeen() throws Exception {
        var queue = java.util.concurrent.Executors.newFixedThreadPool(3);
        try (Connection su = holdRow()) {
            Future<?> first = queuedOnRow(queue);
            PgLockWait.awaitBlocked(su, 1, first);
            Future<?> second = queuedOnRow(queue);                 // queues after the holder's connection has read the view
            PgLockWait.awaitBlocked(su, 2, first, second);
            assertThat(first.isDone()).isFalse();
            assertThat(second.isDone()).isFalse();
            su.rollback();
            first.get(60, TimeUnit.SECONDS);
            second.get(60, TimeUnit.SECONDS);
        } finally {
            queue.shutdownNow();
        }
    }

    @Test
    void aWatchedCommitThatEndsInsteadOfQueuing_failsTheWaitAtOnce_withTheLockState() throws Exception {
        var queue = java.util.concurrent.Executors.newFixedThreadPool(1);
        try (Connection su = holdRow()) {
            Future<?> neverQueues = queue.submit(() -> "done");   // returns without touching the held row
            neverQueues.get(10, TimeUnit.SECONDS);
            assertThatThrownBy(() -> PgLockWait.awaitBlocked(su, 1, neverQueues))
                .isInstanceOf(AssertionError.class)
                .hasMessageContaining("commit #0 ended while waiting for 1 blocked backend(s)")
                .hasMessageContaining("it returned")
                .hasMessageContaining("non-idle backends")
                .hasMessageContaining("lock requests not granted");
            su.rollback();
        } finally {
            queue.shutdownNow();
        }
    }
}
