// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.DeadlockRetry;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): a writer that touches several collections reads their registry rows {@code FOR KEY SHARE} in ONE
 * statement ordered by {@code collection_id} ({@code CollectionRegistry#requireAllForWrite}), the order a retiring verb locks its rows
 * in. Read one after another in the request's order, a remap batch whose source has the HIGHER id and whose target the lower would hold
 * the key share of the source and wait for the target's, against a verb that holds the target and wants the source: a cycle.
 *
 * <p>The verb here is a hand session: it holds the LOWER row {@code FOR UPDATE}; the real {@code POST /v1/remap/record_batch} arrives and
 * waits; the verb then takes the HIGHER row {@code FOR UPDATE}, which must go through at once (the writer holds nothing on it) and
 * commits; the writer then completes. Both orders of source and target against the ids are run. Each asserts the writer answered 200, the
 * verb saw no 40P01, and {@code DeadlockRetry} absorbed no retry. The negative control that shows such an assertion can fail is
 * {@code WriterLockOrderNegativeControlTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MultiCollectionWritersLockOrderTest {

    CollectionRouteFixture fx;
    ExecutorService pool;
    int n;
    private final ConcurrentLinkedQueue<String> retries = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    @BeforeEach
    void listenForRetries() {
        retries.clear();
        DeadlockRetry.setRetryListenerForTests(retries::add);
    }

    @AfterEach
    void noDeadlockWasAbsorbed() {
        DeadlockRetry.setRetryListenerForTests(null);
        assertThat(retries).as("the id order prevents the cycle: DeadlockRetry absorbed none").isEmpty();
    }

    private int waitingRows() {
        return fx.asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private void awaitWaiting(Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingRows() < 1) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": no lock request ever showed waiting in pg_locks (racer done: " + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private void lockRow(Connection c, String name) {
        DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).forUpdate().fetch();
    }

    private void remapBatchAgainstAVerbHoldingTheLowerRow(boolean sourceIsTheHigherId) throws Exception {
        String lo = label("w1-multi-lo-" + (n++));
        fx.register(lo);
        String hi = label("w1-multi-hi-" + (n++));
        fx.register(hi);
        assertThat(fx.idOf(lo)).as("setup: lo was registered first").isLessThan(fx.idOf(hi));
        String source = sourceIsTheHigherId ? hi : lo;
        String target = sourceIsTheHigherId ? lo : hi;
        String oldId = "w1-multi-old-" + (n++);
        boolean[] verbGot40P01 = new boolean[1];
        Future<Resp> writer;
        try (Connection verb = fx.pg.createConnection("")) {
            verb.setAutoCommit(false);
            lockRow(verb, lo);   // the verb holds the lower row, as a retiring verb does first
            writer = pool.submit(() -> fx.post("/v1/remap/record_batch", "{\"source_collection\":" + json(source) + ",\"entries\":[{\"old_id\":"
                + json(oldId) + ",\"new_chash\":\"" + Chash.ofText("w1-multi-" + oldId).toHex() + "\",\"target_collection\":" + json(target)
                + ",\"provenance\":\"w1\"}]}"));
            try {
                awaitWaiting(writer, "the remap batch waits for the lower row");
                try {
                    lockRow(verb, hi);   // the higher row: goes through at once unless the writer holds it (a cycle)
                } catch (DataAccessException e) {
                    if ("40P01".equals(e.sqlState())) {
                        verbGot40P01[0] = true;
                    } else {
                        throw e;
                    }
                }
            } finally {
                verb.rollback();
            }
        }
        Resp r = writer.get(120, TimeUnit.SECONDS);
        assertThat(verbGot40P01[0]).as("the verb took the higher row while the writer waited: no deadlock was detected on its side").isFalse();
        assertThat(r.status()).as("the writer was not made the deadlock victim: %s", r.body()).isEqualTo(200);
        int recorded = fx.asSuperuser(ctx -> ctx.fetchCount(CHASH_REMAP, CHASH_REMAP.SOURCE_COLLECTION.eq(source).and(CHASH_REMAP.OLD_ID.eq(oldId))));
        assertThat(recorded).as("the batch was recorded").isEqualTo(1);
    }

    @Timeout(180)
    @Test
    void aRemapBatchWhoseSourceHasTheHigherId_readsBothRowsInIdOrder_andMeetsAVerbWithoutACycle() throws Exception {
        remapBatchAgainstAVerbHoldingTheLowerRow(true);
    }

    @Timeout(180)
    @Test
    void aRemapBatchWhoseSourceHasTheLowerId_readsBothRowsInIdOrder_andMeetsAVerbWithoutACycle() throws Exception {
        remapBatchAgainstAVerbHoldingTheLowerRow(false);
    }

    /**
     * A document batch that names one collection by ID and another by LABEL (the review's mixed batch): its registry rows are read in ONE
     * statement in id order. The batch here names the HIGHER row by id and the LOWER by label. The code this replaced read the ids first (the
     * labels only came with the per-document check, or in a run of their own when there were two), so it took the key share of the higher row
     * and then waited for the lower one, against a verb that holds the lower and wants the higher: a cycle (the verb's wait for the higher row
     * is behind the writer's share, the writer's wait for the lower is behind the verb). Read in id order the batch waits for the lower row
     * first, holding nothing, and the verb gets the higher one.
     */
    @Timeout(180)
    @Test
    void aDocumentBatchNamingTheHigherRowByIdAndTheLowerByLabel_readsBothRowsInIdOrder_andMeetsAVerbWithoutACycle() throws Exception {
        String lo = label("w1-mixed-lo-" + (n++));
        fx.register(lo);
        String hi = label("w1-mixed-hi-" + (n++));
        fx.register(hi);
        long hiId = fx.idOf(hi);
        assertThat(fx.idOf(lo)).as("setup: lo was registered first").isLessThan(hiId);
        String seq = "p35a-mixed-" + (n++);
        boolean[] verbGot40P01 = new boolean[1];
        Future<Resp> writer;
        try (Connection verb = fx.pg.createConnection("")) {
            verb.setAutoCommit(false);
            lockRow(verb, lo);   // the verb holds the lower row first
            writer = pool.submit(() -> fx.post("/v1/catalog/doc/register_many", "{\"owner_prefix\":\"" + seq + "\",\"docs\":["
                + "{\"title\":\"" + seq + "-a\",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\",\"file_path\":\"" + seq + "-a.md\",\"physical_collection_id\":" + hiId + "},"
                + "{\"title\":\"" + seq + "-b\",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\",\"file_path\":\"" + seq + "-b.md\",\"physical_collection\":" + json(lo) + "}]}"));
            try {
                awaitWaiting(writer, "the batch waits for the lower row the verb holds");
                try {
                    lockRow(verb, hi);   // the higher row: goes through at once unless the writer holds it (a cycle)
                } catch (DataAccessException e) {
                    if ("40P01".equals(e.sqlState())) {
                        verbGot40P01[0] = true;
                    } else {
                        throw e;
                    }
                }
            } finally {
                verb.rollback();
            }
        }
        Resp r = writer.get(120, TimeUnit.SECONDS);
        assertThat(verbGot40P01[0]).as("the verb took the higher row while the batch waited: no deadlock was detected on its side").isFalse();
        assertThat(r.status()).as("the batch was not made the deadlock victim: %s", r.body()).isEqualTo(200);
    }
}
