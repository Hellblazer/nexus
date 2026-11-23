// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.OneRowRename;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.locks.LockSupport;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a, linda's "live at commit" ruling (bead nexus-p2m1x.23): every verb that RETIRES a registry row (the catalog rename's name
 * body and the collection supersede) takes, in this order, the rename lock shared, the old registry row {@code FOR UPDATE} (the
 * target's too when a rename revives or retires it, in {@code collection_id} order), and only then does any chunk, manifest or child-row
 * work. A writer holds {@code FOR KEY SHARE} on the registry row it writes under (a foreign key takes it, and the by-id read takes it
 * explicitly) and never the rename lock, and {@code FOR UPDATE} conflicts with {@code FOR KEY SHARE}.
 *
 * <p>(i) A writer holds its key share on X with a row written and not committed: the verb waits, and once the writer commits, its
 * work includes the writer's row. (ii) The reverse order: the verb holds X {@code FOR UPDATE} and is parked later in its own
 * transaction; a writer that then asks for X's key share waits, and when the verb commits it reads the RETIRED row, which is what the
 * engine answers 409 {@code collection_superseded} from, and writes nothing into it.
 *
 * <p>No timing is assumed: each step goes on only when the database shows the other side waiting (pg_stat_activity), and the verb is
 * parked by a lock the test holds (an advisory lock the rename takes next, or the row its supersede rewrites last).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RetiringVerbsLiveAtCommitTest {

    private static final String TENANT = "live-at-commit-tenant";
    private static final String SVC_ROLE = "svc_live_at_commit";
    private static final String SVC_PASS = "svc_live_at_commit_pass";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    ExecutorService pool;
    CatalogRepository repo;
    final AtomicInteger n = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        su(ctx -> {
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            return null;
        });
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(6);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        pool = Executors.newFixedThreadPool(3);
        repo = new CatalogRepository(new TenantScope(svcDs), Gate3.never(),
            new RenameGuard(() -> Missing.NAME_KEY_DROP_MOVE, OneRowRename.failClosed()));
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ---- helpers ------------------------------------------------------------------------------------------------------

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private String label(String stem) {
        return "knowledge__" + stem + "-" + n.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private String registered(String stem) {
        String name = label(stem);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, name);
            return null;
        });
        return name;
    }

    /** The by-id read of a writer: the registry row's {@code superseded_by}, read {@code FOR KEY SHARE}. */
    private static String keyShare(DSLContext ctx, String name) {
        return ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .forKeyShare().fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY);
    }

    /** A writer's row: a chunk filed under {@code collection} on the writer's own session. */
    private static void writeChunk(DSLContext ctx, String collection) {
        byte[] chash = HexFormat.of().parseHex(dev.nexus.service.db.Chash.ofText(collection + "/chunk").toHex());
        PgContainerHelper.insertChunk384(ctx, TENANT, collection, chash, VectorSeeds.vec(384, 0.1f));
    }

    private int chunksNamed(String collection) {
        return su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))));
    }

    private String supersededBy(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY));
    }

    /** How many sessions of this database wait for a lock. */
    private int waiters() {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")),
            DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
                .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class)))));
    }

    /** Waits (against a deadline, no assumption about speed) until {@code count} sessions wait for a lock; fails when {@code racer} ended. */
    private void awaitWaiters(int count, Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waiters() < count) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": " + count + " waiting session(s) never showed (racer done: " + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    /** The advisory lock a rename takes on a collection's sweep gate, as {@code CatalogRepository.acquireSweepGateShared} spells it. */
    private static void holdSweepGate(DSLContext ctx, String collection) {
        ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
            DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + collection)))).fetch();
    }

    // ---- (i) a writer holds its key share: the verb waits, and its work includes the writer's row -----------------------

    @Timeout(120)
    @Test
    void aRenameWaitsForAWriterInFlightOnItsSource_andTheRepointIncludesTheWritersRow() throws Exception {
        String x = registered("rn-i-x");
        String y = label("rn-i-y");
        Future<?> rename;
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            keyShare(wc, x);
            writeChunk(wc, x);   // the writer's row, not committed
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            try {
                awaitWaiters(1, rename, "the rename waits for the writer's key share on " + x);
                assertThat(rename.isDone()).as("the rename waits for the writer").isFalse();
            } finally {
                w.commit();
            }
        }
        rename.get(60, TimeUnit.SECONDS);

        assertThat(chunksNamed(y)).as("the repoint includes the writer's row").isEqualTo(1);
        assertThat(chunksNamed(x)).as("nothing is left under the retired label").isZero();
        assertThat(supersededBy(x)).as("the source is the tombstone of the target").isEqualTo(y);
    }

    @Timeout(120)
    @Test
    void aSupersedeWaitsForAWriterInFlightOnTheRetiringRow_andThenRetiresIt() throws Exception {
        String x = registered("sp-i-x");
        String y = registered("sp-i-y");
        Future<Integer> supersede;
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            keyShare(wc, x);
            writeChunk(wc, x);
            supersede = pool.submit(() -> repo.supersedeCollection(TENANT, x, y, null));
            try {
                awaitWaiters(1, supersede, "the supersede waits for the writer's key share on " + x);
                assertThat(supersede.isDone()).as("the supersede waits for the writer").isFalse();
            } finally {
                w.commit();
            }
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS)).isEqualTo(1);

        assertThat(supersededBy(x)).as("the row is retired once the writer is done").isEqualTo(y);
        assertThat(chunksNamed(x)).as("the writer's committed row is on the retired row, which the supersede never moves").isEqualTo(1);
    }

    // ---- (ii) the verb holds the row FOR UPDATE: a writer that arrives waits, then reads the retired row ----------------

    @Timeout(120)
    @Test
    void aWriterArrivingWhileARenameHoldsTheSourceWaits_thenReadsTheRetiredRow_andWritesNothingIntoIt() throws Exception {
        String x = registered("rn-ii-x");
        String y = label("rn-ii-y");
        Future<?> rename;
        Future<String> writer;
        try (Connection gate = pg.createConnection("")) {
            gate.setAutoCommit(false);
            holdSweepGate(DSL.using(gate, SQLDialect.POSTGRES), x);   // parks the rename on its next lock, after its row locks
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            try {
                awaitWaiters(1, rename, "the rename parks on the sweep gate of " + x);
                writer = pool.submit(writerSeeing(x));
                awaitWaiters(2, writer, "the writer waits for the key share on " + x);
                assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
                assertThat(rename.isDone()).as("guard: the rename is parked").isFalse();
            } finally {
                gate.rollback();
            }
        }
        rename.get(60, TimeUnit.SECONDS);

        assertThat(writer.get(60, TimeUnit.SECONDS)).as("the writer reads the retired row: the successor's label, the 409's input").isEqualTo(y);
        assertThat(chunksNamed(x)).as("nothing was written into the retired row").isZero();
    }

    @Timeout(120)
    @Test
    void aWriterArrivingWhileASupersedeHoldsTheRowWaits_thenReadsTheRetiredRow_andWritesNothingIntoIt() throws Exception {
        String x = registered("sp-ii-x");
        String y = registered("sp-ii-y");
        Future<Integer> supersede;
        Future<String> writer;
        try (Connection park = pg.createConnection("")) {
            park.setAutoCommit(false);
            // the successor's row held FOR UPDATE parks the supersede at its last write (the successor's owner re-derive), after X's lock
            DSL.using(park, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(y)).forUpdate().fetch();
            supersede = pool.submit(() -> repo.supersedeCollection(TENANT, x, y, null));
            try {
                awaitWaiters(1, supersede, "the supersede parks on the successor's row");
                writer = pool.submit(writerSeeing(x));
                awaitWaiters(2, writer, "the writer waits for the key share on " + x);
                assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
            } finally {
                park.rollback();
            }
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS)).isEqualTo(1);

        assertThat(writer.get(60, TimeUnit.SECONDS)).as("the writer reads the retired row").isEqualTo(y);
        assertThat(chunksNamed(x)).as("nothing was written into the retired row").isZero();
    }

    /** The advisory lock a writer (or a rename) takes SHARED on a collection's sweep gate, and a sweep takes EXCLUSIVE. */
    private static void sweepGate(DSLContext ctx, String collection, boolean shared) {
        ctx.select(DSL.function(shared ? "pg_advisory_xact_lock_shared" : "pg_advisory_xact_lock", Object.class,
            DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + collection)))).fetch();
    }

    // ---- the canonical lock order: gate, then row, on both sides (no cycle with a sweep) -----------------------------------

    /**
     * The cycle the order closes: a writer holds the shared gate of X and has not yet taken its key share; a sweep queues for the
     * exclusive gate behind it; then the rename. With the row locked before the gates the rename would take X {@code FOR UPDATE} and
     * queue for its gate behind the sweep, and the writer's key share would wait for the rename: writer, rename and sweep in a cycle,
     * which Postgres breaks with a deadlock and {@code DeadlockRetry} retries. In the canonical order the rename waits at the gate
     * with no row locked, the writer's key share goes through and it commits, the sweep takes and drops the gate, and the rename then
     * completes with the writer's row in its repoint. The test asserts no retry fired.
     */
    @Timeout(120)
    @Test
    void aSweepQueuedForTheExclusiveGate_closesNoCycleBetweenAWriterAndARename() throws Exception {
        String x = registered("cy-x");
        String y = label("cy-y");
        long retriesBefore = dev.nexus.service.db.DeadlockRetry.retryAttemptCount();
        Future<?> sweep;
        Future<?> rename;
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            sweepGate(wc, x, true);   // the writer's gate, shared; its key share comes after
            sweep = pool.submit(() -> {
                try (Connection c = pg.createConnection("")) {
                    c.setAutoCommit(false);
                    sweepGate(DSL.using(c, SQLDialect.POSTGRES), x, false);   // queues behind the writer
                    c.commit();
                }
                return null;
            });
            awaitWaiters(1, sweep, "the sweep queues for the exclusive gate of " + x);
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            awaitWaiters(2, rename, "the rename queues for the gate behind the sweep");

            keyShare(wc, x);   // in the canonical order the rename holds no row yet, so this does not wait
            writeChunk(wc, x);
            w.commit();
        }
        sweep.get(60, TimeUnit.SECONDS);
        rename.get(60, TimeUnit.SECONDS);

        assertThat(dev.nexus.service.db.DeadlockRetry.retryAttemptCount()).as("no deadlock, so no retry fired").isEqualTo(retriesBefore);
        assertThat(chunksNamed(y)).as("the rename's repoint includes the writer's row").isEqualTo(1);
        assertThat(supersededBy(x)).isEqualTo(y);
    }

    // ---- a rename onto a tombstone locks both rows, in collection_id order, and revives the target ------------------------

    /**
     * X is retired by a rename to Y, so Y has the higher id; renaming Y back onto X revives the tombstone X (the lower id). A writer
     * holds Y's key share, so the rename takes X {@code FOR UPDATE} first (the lower id) and then waits on Y; a second reader of X's key
     * share then waits too, which shows X was locked while the rename waited on Y. When the writer commits the rename completes and
     * X is live again.
     */
    @Timeout(120)
    @Test
    void aRenameOntoATombstoneLocksBothRowsInIdOrder_andRevivesTheTarget() throws Exception {
        String x = registered("rv-x");
        String y = label("rv-y");
        repo.renameCollection(TENANT, x, y);   // X is now the tombstone of Y; Y was minted after X, so it has the higher id
        assertThat(supersededBy(x)).isEqualTo(y);
        long idX = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        long idY = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(y)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        assertThat(idX).as("guard: the tombstone has the lower id").isLessThan(idY);

        Future<?> rename;
        Future<String> reader;
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            keyShare(DSL.using(w, SQLDialect.POSTGRES), y);   // a writer in flight on the source
            rename = pool.submit(() -> repo.renameCollection(TENANT, y, x));
            try {
                awaitWaiters(1, rename, "the rename waits for the writer on " + y);
                reader = pool.submit(writerSeeing(x));
                awaitWaiters(2, reader, "a key share on the tombstone waits: the rename holds it while it waits on " + y);
                assertThat(reader.isDone()).isFalse();
            } finally {
                w.commit();
            }
        }
        rename.get(60, TimeUnit.SECONDS);

        assertThat(reader.get(60, TimeUnit.SECONDS)).as("the reader saw X after the revive: live").isEmpty();
        assertThat(supersededBy(x)).as("the tombstone is revived").isEmpty();
        assertThat(supersededBy(y)).as("the source is retired onto it").isEqualTo(x);
    }

    /** A writer: key share on the row first, and a row written only when the row is still live. */
    private Callable<String> writerSeeing(String x) {
        return () -> {
            try (Connection c = pg.createConnection("")) {
                c.setAutoCommit(false);
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
                String retiredTo = keyShare(ctx, x);
                if (retiredTo == null || retiredTo.isEmpty()) {
                    writeChunk(ctx, x);   // live: the write goes ahead
                }
                c.commit();
                return retiredTo;
            }
        };
    }
}
