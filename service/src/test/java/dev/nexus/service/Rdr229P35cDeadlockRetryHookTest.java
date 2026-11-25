// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.TOPIC_LINKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.db.TaxonomyRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * The negative control for the {@link DeadlockRetry#lastRetriedContext()} assertions of the P3.5c racing tests (linda, 2026-11-23).
 * {@code DeadlockRetry} absorbs a 40P01 and the loser ends in a clean answer, so a racing test that asserts only statuses or row counts
 * cannot see a deadlock; the P3.5c tests assert that the hook does not name their own transaction. That assertion is only worth
 * something if the hook can fail, so this test takes two row locks in the WRONG order on purpose against a wrapped repository path
 * ({@code TaxonomyRepository#importBatch}, which locks {@code topic_links} rows in key order inside one statement) and requires the hook
 * to name that path's context.
 *
 * <p>The cycle: an outside transaction holds the row (t2,t3); the import locks (t1,t2) and waits on (t2,t3); the outside transaction
 * then asks for (t1,t2). The import has waited longest, so its deadlock check runs first and it is the victim; {@code DeadlockRetry}
 * runs it again once the outside transaction has finished.
 */
class Rdr229P35cDeadlockRetryHookTest extends AtomicWriteTestBase {

    private static final String CONTEXT = "taxonomy.importBatch.link";

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aWrongOrderLockWaitDeadlocksAWrappedPath_andTheHookNamesItsContext() throws Exception {
        String collection = "code__p35cdlk" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(collection);
        var taxonomy = new TaxonomyRepository(tenantScope);
        long t1 = taxonomy.insertTopic(TENANT, "dl-1", null, collection, 0, "2026-01-01T00:00:00Z", null);
        long t2 = taxonomy.insertTopic(TENANT, "dl-2", null, collection, 0, "2026-01-01T00:00:00Z", null);
        long t3 = taxonomy.insertTopic(TENANT, "dl-3", null, collection, 0, "2026-01-01T00:00:00Z", null);
        List<Map<String, Object>> rows = List.of(link(t1, t2), link(t2, t3));
        assertThat(taxonomy.importBatch(TENANT, "link", rows)).as("both link rows exist, so the import must lock them").isEqualTo(2);
        String before = DeadlockRetry.lastRetriedContext();

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try (Connection outside = pg.createConnection("")) {
            outside.setAutoCommit(false);
            DSLContext o = DSL.using(outside, SQLDialect.POSTGRES);
            assertThat(lock(o, t2, t3)).as("the outside transaction holds (t2,t3)").isEqualTo(1);

            CompletableFuture<Integer> importing = CompletableFuture.supplyAsync(() -> taxonomy.importBatch(TENANT, "link", rows), pool);
            waitUntilALockWaitOnTopicLinksExists();

            CompletableFuture<Integer> outsideSecond = CompletableFuture.supplyAsync(() -> lock(o, t1, t2), pool);
            assertThat(outsideSecond.get(60, TimeUnit.SECONDS)).as("the outside transaction got (t1,t2) once the victim rolled back")
                .isEqualTo(1);
            outside.commit();   // the victim's retry waits on the outside rows, so they are released before it is awaited
            assertThat(importing.get(60, TimeUnit.SECONDS)).as("the import ends in a clean answer, as every victim of a wrapped path does")
                .isEqualTo(2);
        } finally {
            pool.shutdownNow();
        }

        assertThat(DeadlockRetry.lastRetriedContext()).as("the hook can fail: a deadlock on a wrapped path names that path's context")
            .isEqualTo(CONTEXT).isNotEqualTo(before);
    }

    private static Map<String, Object> link(long from, long to) {
        return Map.of("from_topic_id", from, "to_topic_id", to, "link_count", 1, "link_types", "{}");
    }

    private static int lock(DSLContext o, long from, long to) {
        return o.update(TOPIC_LINKS).set(TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_COUNT.plus(1))
            .where(TOPIC_LINKS.TENANT_ID.eq(TENANT)).and(TOPIC_LINKS.FROM_TOPIC_ID.eq(from)).and(TOPIC_LINKS.TO_TOPIC_ID.eq(to)).execute();
    }

    /** Polls pg_stat_activity until a backend waits on a lock: the import is parked on the outside row, so the cycle can be closed. */
    private void waitUntilALockWaitOnTopicLinksExists() throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            while (System.nanoTime() < deadline) {
                int waiting = ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")),
                    DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
                        .and(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("%topic_links%")));
                if (waiting > 0) {
                    return;
                }
                Thread.sleep(50);
            }
        }
        throw new AssertionError("non-vacuity: the import never waited on the outside transaction's row");
    }
}
