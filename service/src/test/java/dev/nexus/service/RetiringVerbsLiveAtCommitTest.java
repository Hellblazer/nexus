// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.OneRowRename;
import dev.nexus.service.db.RenameLatchLock;
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
 * work. A writer holds {@code FOR KEY SHARE} on the registry row it writes under (the registry check every write transaction runs
 * first takes it, by id through the request carrier and by label through the memoised in-transaction read, and the foreign key takes it
 * again at the first child insert) and never the rename lock, and {@code FOR UPDATE} conflicts with {@code FOR KEY SHARE}. The writers
 * below are hand-rolled sessions that take that key share; the production writers are exercised after slice A's assembly.
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

    /** A writer's registry check: the row's {@code superseded_by}, read {@code FOR KEY SHARE} first in its transaction. */
    private static String keyShare(DSLContext ctx, String name) {
        return ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .forKeyShare().fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY);
    }

    /** A writer's row: a chunk filed under {@code collection} on the writer's own session. */
    private static void writeChunk(DSLContext ctx, String collection) {
        writeChunk(ctx, collection, "chunk");
    }

    private static void writeChunk(DSLContext ctx, String collection, String salt) {
        byte[] chash = HexFormat.of().parseHex(dev.nexus.service.db.Chash.ofText(collection + "/" + salt).toHex());
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

    /** The two kinds of wait the tests order by: a row lock (the tuple or the transaction it waits on) and an advisory lock. */
    private enum Wait {
        ROW("tuple", "transactionid"), ADVISORY("advisory");

        final String[] locktypes;

        Wait(String... locktypes) {
            this.locktypes = locktypes;
        }
    }

    /** How many lock requests of this kind are waiting now: the specific rows of {@code pg_locks} that are not granted. */
    private int waiting(Wait kind) {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in(kind.locktypes))));
    }

    /** Waits (against a deadline, no assumption about speed) until {@code count} lock requests of {@code kind} wait; fails when {@code racer} ended. */
    private void awaitWaiting(Wait kind, int count, Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waiting(kind) < count) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": " + count + " waiting " + kind + " lock request(s) never showed in pg_locks (racer done: "
                    + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
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
                awaitWaiting(Wait.ROW, 1, rename, "the rename waits for the writer's key share on " + x);
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
                awaitWaiting(Wait.ROW, 1, supersede, "the supersede waits for the writer's key share on " + x);
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
        su(ctx -> {
            writeChunk(ctx, x);   // a chunk of X, committed: the rename's re-home rewrites it, in its child-row phase
            return null;
        });
        Future<?> rename;
        Future<String> writer;
        try (Connection gate = pg.createConnection("")) {
            gate.setAutoCommit(false);
            // X's chunk row held FOR UPDATE parks the rename at its chunk re-home, AFTER it took the gates and X's registry row
            DSL.using(gate, SQLDialect.POSTGRES).select(CHUNKS.CHASH).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(x)).forUpdate().fetch();
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            try {
                awaitWaiting(Wait.ROW, 1, rename, "the rename parks on X's chunk row");
                writer = pool.submit(writerSeeing(x));
                awaitWaiting(Wait.ROW, 2, writer, "the writer waits for the key share on " + x);
                assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
                assertThat(rename.isDone()).as("guard: the rename is parked").isFalse();
            } finally {
                gate.rollback();
            }
        }
        rename.get(60, TimeUnit.SECONDS);

        assertThat(writer.get(60, TimeUnit.SECONDS)).as("the writer reads the retired row: the successor's label, the 409's input").isEqualTo(y);
        assertThat(chunksNamed(x)).as("nothing was written into the retired row: the chunk moved to Y").isZero();
        assertThat(chunksNamed(y)).as("the rename's re-home had X's chunk, and the writer added none").isEqualTo(1);
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
                awaitWaiting(Wait.ROW, 1, supersede, "the supersede parks on the successor's row");
                writer = pool.submit(writerSeeing(x));
                awaitWaiting(Wait.ROW, 2, writer, "the writer waits for the key share on " + x);
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
            awaitWaiting(Wait.ADVISORY, 1, sweep, "the sweep queues for the exclusive gate of " + x);
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            awaitWaiting(Wait.ADVISORY, 2, rename, "the rename queues for the gate behind the sweep");

            keyShare(wc, x);   // in the canonical order the rename holds no row yet, so this does not wait
            writeChunk(wc, x);
            w.commit();
        }
        sweep.get(60, TimeUnit.SECONDS);
        rename.get(60, TimeUnit.SECONDS);

        assertThat(dev.nexus.service.db.DeadlockRetry.lastRetriedContext()).as("no deadlock, so this rename's transaction was not retried")
            .isNotEqualTo("catalog.renameCollection " + x);
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
        // X sorts AFTER Y by label and has the LOWER id: locking by label order instead of id order would lock Y first and this goes red
        String x = registered("rv-z");
        String y = label("rv-a");
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
                awaitWaiting(Wait.ROW, 1, rename, "the rename waits for the writer on " + y);
                reader = pool.submit(writerSeeing(x));
                awaitWaiting(Wait.ROW, 2, reader, "a key share on the tombstone waits: the rename holds it while it waits on " + y);
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

    // ---- the supersede: the rename lock, and the successor's row too ------------------------------------------------------

    @Timeout(120)
    @Test
    void aSupersedeTakesTheRenameLockShared_soALatchWriteInFlightHoldsItBack() throws Exception {
        String x = registered("sp-rl-x");
        String y = registered("sp-rl-y");
        Future<Integer> supersede;
        try (Connection latchWrite = pg.createConnection("")) {
            latchWrite.setAutoCommit(false);
            RenameLatchLock.exclusive(DSL.using(latchWrite, SQLDialect.POSTGRES));   // the latch write holds the rename lock
            supersede = pool.submit(() -> repo.supersedeCollection(TENANT, x, y, null));
            try {
                awaitWaiting(Wait.ADVISORY, 1, supersede, "the supersede waits on the rename lock");
                assertThat(supersede.isDone()).isFalse();
                assertThat(supersededBy(x)).as("nothing was written while it waited").isEmpty();
            } finally {
                latchWrite.rollback();
            }
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS)).isEqualTo(1);
        assertThat(supersededBy(x)).isEqualTo(y);
    }

    @Timeout(120)
    @Test
    void aSupersedeLocksTheSuccessorsRowToo_soAWriterInFlightOnItHoldsTheSupersedeBack() throws Exception {
        String x = registered("sp-sc-x");
        String y = registered("sp-sc-y");
        Future<Integer> supersede;
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            keyShare(DSL.using(w, SQLDialect.POSTGRES), y);   // a writer in flight on the SUCCESSOR
            supersede = pool.submit(() -> repo.supersedeCollection(TENANT, x, y, null));
            try {
                awaitWaiting(Wait.ROW, 1, supersede, "the supersede waits for the writer on the successor " + y);
                assertThat(supersede.isDone()).isFalse();
            } finally {
                w.commit();
            }
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS)).isEqualTo(1);
        assertThat(supersededBy(x)).isEqualTo(y);
    }

    // ---- the pinned order, with the live-tuple index ABSENT so the tuple locks actually run -----------------------------

    private static org.jooq.Field<Object> tupleLock(String owner, String model, String version) {
        return DSL.function("pg_advisory_xact_lock", Object.class,
            DSL.function(DSL.name("nexus", "collection_tuple_lock_key"), Long.class, DSL.val(TENANT), DSL.val("knowledge"),
                DSL.val(owner), DSL.val(model), DSL.val(version)));
    }

    /**
     * RDR-229 P3.8 pinned the tuple locks before any registry row, and the verb's row locks follow them. With the index absent the
     * rename takes the new row's tuple lock; a session holds it, so the rename waits (an advisory request), and from another
     * session X's row is free: a rename that locked X first would hold it here.
     */
    @Timeout(120)
    @Test
    void theTupleLocksComeBeforeTheRegistryRows_withTheIndexAbsent() throws Exception {
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
        String x = registered("tl-x");
        String y = label("tl-y");
        var attrs = su(ctx -> ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x)).fetchOne());
        String newOwner = "tl-owner-y";
        Future<?> rename;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(tupleLock(newOwner, attrs.value1(), attrs.value2())).fetch();
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y, null, "knowledge", newOwner));
            try {
                awaitWaiting(Wait.ADVISORY, 1, rename, "the rename waits for the new row's tuple lock");
                su(ctx -> ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x)).forUpdate().noWait().fetch());
            } finally {
                holder.commit();
            }
        }
        rename.get(60, TimeUnit.SECONDS);
        assertThat(supersededBy(x)).as("the rename went through once the tuple lock was free").isEqualTo(y);
    }

    // ---- a writer arrives while the verb is queued behind a holder; the verb's bound --------------------------------------

    /**
     * The observed fairness (the engine run of this test is green): a new {@code FOR KEY SHARE} locker JOINS a row while a
     * {@code FOR UPDATE} waits for the earlier holder, it does not queue behind the verb. So the verb waits for BOTH writers, and its
     * repoint includes both rows. The second writer's key share does not wait (a 3 s lock bound on it makes the test fail rather than
     * hang if it did). This is why a verb can be starved by overlapping writers, and why it carries a bound.
     */
    @Timeout(120)
    @Test
    void aWriterArrivingWhileTheVerbIsQueuedBehindAHolderJoinsIt_andTheVerbWaitsForBoth() throws Exception {
        String x = registered("qq-x");
        String y = label("qq-y");
        Future<?> rename;
        try (Connection w1 = pg.createConnection(""); Connection w2 = pg.createConnection("")) {
            w1.setAutoCommit(false);
            w2.setAutoCommit(false);
            DSLContext c1 = DSL.using(w1, SQLDialect.POSTGRES);
            DSLContext c2 = DSL.using(w2, SQLDialect.POSTGRES);
            keyShare(c1, x);
            writeChunk(c1, x);
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            awaitWaiting(Wait.ROW, 1, rename, "the rename queues behind the first writer");
            c2.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("3000"), DSL.val(true))).fetch();
            keyShare(c2, x);   // joins: does not wait behind the queued FOR UPDATE
            writeChunk(c2, x, "second writer");
            assertThat(rename.isDone()).isFalse();
            w1.commit();
            assertThat(rename.isDone()).as("the first writer is done, the second still holds the row").isFalse();
            assertThat(waiting(Wait.ROW)).as("the rename still waits").isGreaterThanOrEqualTo(1);
            w2.commit();
        }
        rename.get(60, TimeUnit.SECONDS);
        assertThat(chunksNamed(y)).as("the repoint includes both writers' rows").isEqualTo(2);
        assertThat(chunksNamed(x)).isZero();
    }

    /**
     * The verb's bound: one writer keeps its key share on X longer than the (shortened, 1 s) bound, so the verb answers
     * {@link dev.nexus.service.db.RenameBusyException} (503 {@code rename_busy}) within the bound plus slack and not a hang, X stays
     * live, Y is never registered, and nothing changed.
     */
    @Timeout(120)
    @Test
    void aVerbWhoseRowIsHeldPastItsBoundAnswersRenameBusy_andChangesNothing() throws Exception {
        dev.nexus.service.db.CollectionTombstones.setRowLockTimeoutMsForTests("1000");
        try {
            String x = registered("bd-x");
            String y = label("bd-y");
            Future<?> rename;
            long started;
            try (Connection holder = pg.createConnection("")) {
                holder.setAutoCommit(false);
                keyShare(DSL.using(holder, SQLDialect.POSTGRES), x);   // one writer, still in flight when the bound passes
                started = System.nanoTime();
                rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
                Throwable failure = null;
                try {
                    rename.get(30, TimeUnit.SECONDS);   // the holder has not committed: only the bound can end the wait
                } catch (java.util.concurrent.ExecutionException e) {
                    failure = e.getCause();
                } finally {
                    holder.rollback();
                }
                long elapsedMs = (System.nanoTime() - started) / 1_000_000;

                assertThat(failure).as("the verb answered instead of hanging").isNotNull();
                boolean busy = false;
                for (Throwable c = failure; c != null; c = c.getCause()) busy |= c instanceof dev.nexus.service.db.RenameBusyException;
                assertThat(busy).as("rename_busy, got %s", failure).isTrue();
                assertThat(elapsedMs).as("within the bound plus slack").isGreaterThanOrEqualTo(900).isLessThan(20_000);
            }
            assertThat(supersededBy(x)).as("nothing changed: X is live").isEmpty();
            boolean yRegistered = su(ctx -> ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(y))));
            assertThat(yRegistered).as("and Y was not registered").isFalse();
        } finally {
            dev.nexus.service.db.CollectionTombstones.setRowLockTimeoutMsForTests(null);
        }
    }

    // ---- what the wait made stale is read again ----------------------------------------------------------------------------

    /**
     * A writer in flight changes X's owner (so X's tuple) and commits; the rename, which read X's tuple before it waited for X's row,
     * reads it again once it holds the row, sees the change and answers the named refusal, not a 23505 or a duplicate live tuple.
     */
    @Timeout(120)
    @Test
    void whatTheWaitMadeStaleIsReadAgain_aChangedTupleIsTheNamedRefusal() throws Exception {
        String x = registered("rr-x");
        String y = label("rr-y");
        Future<?> rename;
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            keyShare(wc, x);
            rename = pool.submit(() -> repo.renameCollection(TENANT, x, y));
            try {
                awaitWaiting(Wait.ROW, 1, rename, "the rename waits for the writer");
                wc.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, "changed-while-waiting")
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x)).execute();
            } finally {
                w.commit();
            }
        }
        Throwable failure = null;
        try {
            rename.get(60, TimeUnit.SECONDS);
        } catch (java.util.concurrent.ExecutionException e) {
            failure = e.getCause();
        }
        assertThat(failure).as("the rename is refused").isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining("changed while this rename waited");
        assertThat(supersededBy(x)).as("nothing was renamed").isEmpty();
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
