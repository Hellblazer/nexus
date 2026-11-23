// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda, folio's P3.5a review): the one-row rename's lock order is the rename lock, then FOR UPDATE on the registry rows it
 * touches in ascending collection_id order, then everything else. The id writers hold FOR KEY SHARE on the registry row; once the name key is gone the
 * name UPDATE is a non-key update, so without FOR UPDATE a writer could commit into the row the rename just retired. On a real engine:
 * (a) an id write in flight (FOR KEY SHARE) makes the rename wait, and after the writer commits the rename proceeds and the write is visible under the
 * new label; the waiting backend is the rename's {@code FOR UPDATE} select, which the test asserts by its statement (without that lock the rename would wait at
 * its name UPDATE instead: while {@code (tenant_id, name, embedding_model)} is a unique key the name UPDATE is a key update and conflicts with KEY SHARE
 * anyway, so the outcome alone cannot tell the two apart). (b) the reverse: with the rename in flight, a writer asking for the retired label waits and then finds
 * no row, while by id it finds the row under the new label, never a row under the retired label; (b) holds with or without {@code forUpdate()} for the same
 * reason and is not the pin of the lock. (c) the three-way case the canonical order exists for, pinned by pg_locks: it goes red (40P01) if the rename takes
 * the registry row before its sweep gates (the mutant run is recorded on the bead).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowLockOrderTest {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final org.jooq.Table<?> ACTIVITY = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v1";
    }

    /** Backends of this database waiting on a lock. */
    private int lockWaiters() {
        return engine.su(ctx -> ctx.fetchCount(ACTIVITY, DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
            .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class)))));
    }

    /** The (lower-cased) statements of the backends of this database waiting on a lock. */
    private List<String> waitingQueries() {
        return engine.su(ctx -> ctx.select(DSL.field(DSL.name("query"), String.class)).from(ACTIVITY)
            .where(DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
                .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class))))
            .fetch(0, String.class).stream().map(q -> q == null ? "" : q.toLowerCase()).toList());
    }

    /** Backends of this database waiting for an ADVISORY lock (pg_locks, not granted). */
    private int advisoryWaiters() {
        return engine.su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("locktype"), String.class).eq("advisory").and(DSL.field(DSL.name("granted"), Boolean.class).isFalse())));
    }

    @Test
    void anIdWriteInFlightMakesTheRenameWait_andAfterItCommitsTheRenameProceedsAndTheWriteIsVisibleUnderTheNewLabel() throws Exception {
        String tenant = "p37-lo-a";
        String token = engine.newTenant(tenant);
        String a = coll("p37loaa");
        String b = coll("p37loab");
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, a);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        String chash = dev.nexus.service.db.Chash.ofText("lock order writer").toHex();
        var pool = Executors.newFixedThreadPool(2);
        try (Connection w = engine.pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            wc.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(a))).forKeyShare().fetch();   // an id write in flight
            int waitersBefore = lockWaiters();

            Future<Rdr229RenameEngine.Renamed> rename = pool.submit(() -> engine.rename(token, a, b));

            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > waitersBefore))
                .as("the rename waits on the registry row the writer holds FOR KEY SHARE").isTrue();
            assertThat(waitingQueries()).as("and the waiting backend is the rename's FOR UPDATE select of the registry rows, not a later UPDATE")
                .anyMatch(q -> q.contains("for update") && q.contains("catalog_collections"));
            assertThat(rename.isDone()).as("and has not renamed under the writer").isFalse();
            PgContainerHelper.insertChunks(wc, tenant, a, List.of(chash), List.of("written by the in-flight writer"), List.of(new float[384]), List.of(Map.of()));
            w.commit();

            var r = rename.get(60, TimeUnit.SECONDS);
            assertThat(r.status()).as("after the writer commits the rename proceeds: " + r).isEqualTo(200);
        } finally {
            pool.shutdownNow();
        }
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("one-row: B holds the id").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(id))
            .and(CHUNKS.CHASH.eq(java.util.HexFormat.of().parseHex(chash))))))
            .as("the write is visible under the new label's id").isEqualTo(1L);
    }

    @Test
    void withTheRenameInFlight_aWriterForTheRetiredLabelWaitsAndFindsNoRow_whileByIdItFindsTheRowUnderTheNewLabel() throws Exception {
        String tenant = "p37-lo-b";
        String token = engine.newTenant(tenant);
        String a = coll("p37lobaa");
        String b = coll("p37lobab");
        String t = coll("p37lobat");
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, t);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        var sup = engine.call("POST", "/v1/catalog/collections/supersede", token, "{\"name\":\"" + t + "\",\"superseded_by\":\"" + a + "\"}");
        assertThat(sup.statusCode()).as("non-vacuity: a tombstone T that names A (the rename repoints it): " + sup.body()).isEqualTo(200);
        var pool = Executors.newFixedThreadPool(2);
        try (Connection h = engine.pg.createConnection(""); Connection w = engine.pg.createConnection("")) {
            h.setAutoCommit(false);
            DSL.using(h, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(t))).forUpdate().fetch();   // stalls the rename after its name UPDATE
            int base = lockWaiters();
            Future<Rdr229RenameEngine.Renamed> rename = pool.submit(() -> engine.rename(token, a, b));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > base)).as("the rename is in flight, parked on the tombstone").isTrue();

            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            Future<List<Long>> byLabel = pool.submit(() -> wc.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(a))).forKeyShare().fetch(CATALOG_COLLECTIONS.COLLECTION_ID));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > base + 1))
                .as("the writer for the retired label waits on the row the rename holds FOR UPDATE").isTrue();
            assertThat(byLabel.isDone()).as("it has not slipped a lock onto the retired row").isFalse();

            h.commit();   // the tombstone is released: the rename finishes

            assertThat(rename.get(60, TimeUnit.SECONDS).status()).as("the rename").isEqualTo(200);
            assertThat(byLabel.get(60, TimeUnit.SECONDS)).as("the writer for the retired label finds no row: never a row under the retired label").isEmpty();
            var byId = wc.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))).forKeyShare().fetch(CATALOG_COLLECTIONS.NAME);
            assertThat(byId).as("by id the writer finds the row under the new label").containsExactly(b);
            w.commit();
        } finally {
            pool.shutdownNow();
        }
    }

    private static org.jooq.Select<org.jooq.Record1<Object>> gate(DSLContext ctx, String function, String tenant, String label) {
        return ctx.select(DSL.function(function, Object.class, DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + label))));
    }

    /**
     * The three-way case the canonical order exists for (every advisory lock before any row lock): a writer holding only the SHARED sweep gate, a queued
     * EXCLUSIVE sweep, and the rename, started after the sweep queued; THEN the writer takes its FOR KEY SHARE on the registry row and writes. In the canonical
     * order the rename queues on the gate holding no row lock, so the writer's key share is granted at once, it commits, the sweep runs and the rename proceeds.
     * With the row taken first the rename holds it FOR UPDATE while it waits for the gate: the writer waits for the rename, the sweep for the writer, the
     * rename for the sweep, and PostgreSQL ends the cycle with 40P01. The queue is shown by pg_locks: two advisory waiters.
     */
    @Test
    void aQueuedExclusiveSweep_aWriterHoldingTheSharedGate_andTheRename_allFinishWithoutADeadlock() throws Exception {
        String tenant = "p37-lo-c";
        String token = engine.newTenant(tenant);
        String a = coll("p37loca");
        String b = coll("p37locb");
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, a);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        String chash = dev.nexus.service.db.Chash.ofText("lock order three way").toHex();
        var pool = Executors.newFixedThreadPool(2);
        try (Connection w = engine.pg.createConnection(""); Connection s = engine.pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            gate(wc, "pg_advisory_xact_lock_shared", tenant, a).fetch();   // the writer holds only the shared gate
            int advisoryBase = advisoryWaiters();

            s.setAutoCommit(false);
            Future<?> sweep = pool.submit(() -> {
                gate(DSL.using(s, SQLDialect.POSTGRES), "pg_advisory_xact_lock", tenant, a).fetch();   // the sweep queues EXCLUSIVE behind the writer
                s.commit();
                return null;
            });
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> advisoryWaiters() > advisoryBase)).as("the sweep is queued behind the writer").isTrue();
            Future<Rdr229RenameEngine.Renamed> rename = pool.submit(() -> engine.rename(token, a, b));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> advisoryWaiters() > advisoryBase + 1))
                .as("the rename queues behind the sweep, on the ADVISORY gate (pg_locks)").isTrue();

            // only now does the writer take its registry row lock and write: with the rename holding no row, this is granted at once
            wc.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(a))).forKeyShare().fetch();
            PgContainerHelper.insertChunks(wc, tenant, a, List.of(chash), List.of("the writer finishes"), List.of(new float[384]), List.of(Map.of()));
            w.commit();

            sweep.get(60, TimeUnit.SECONDS);
            assertThat(rename.get(60, TimeUnit.SECONDS).status()).as("the rename finishes after the sweep, no 40P01").isEqualTo(200);
        } finally {
            pool.shutdownNow();
        }
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).isEqualTo(id);
    }

    /**
     * folio's SB3 interleaving: the rename holds its registry rows FOR UPDATE and is stalled (a tombstone's row held by another transaction), a registration of the
     * old label arrives and waits on the renamed row; the rename commits; the registration, now a fresh INSERT of a label whose rows sit under the renamed
     * collection's id, is refused {@code label_reserved}: the check runs after the INSERT, on a new statement snapshot.
     */
    @Test
    void aRegistrationWaitingOnARenameInFlight_isRefusedLabelReservedOnceTheRenameCommits() throws Exception {
        String tenant = "p37-lo-d";
        String token = engine.newTenant(tenant);
        String a = coll("p37lodaa");
        String b = coll("p37lodab");
        String t = coll("p37lodat");
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, t);
            return null;
        });
        var sup = engine.call("POST", "/v1/catalog/collections/supersede", token, "{\"name\":\"" + t + "\",\"superseded_by\":\"" + a + "\"}");
        assertThat(sup.statusCode()).as("non-vacuity: a tombstone naming A: " + sup.body()).isEqualTo(200);
        var pool = Executors.newFixedThreadPool(2);
        try (Connection h = engine.pg.createConnection("")) {
            h.setAutoCommit(false);
            DSL.using(h, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(t))).forUpdate().fetch();   // stalls the rename after its name UPDATE
            int base = lockWaiters();
            Future<Rdr229RenameEngine.Renamed> rename = pool.submit(() -> engine.rename(token, a, b));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > base)).as("the rename is in flight, holding A's row").isTrue();

            Future<java.net.http.HttpResponse<String>> register = pool.submit(() -> engine.call("POST", "/v1/catalog/collections/upsert", token,
                "{\"name\":\"" + a + "\",\"content_type\":\"knowledge\",\"owner_id\":\"someone-else\",\"embedding_model\":\"" + MODEL
                    + "\",\"model_version\":\"v1\"}"));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > base + 1)).as("the registration waits on the renamed row").isTrue();
            assertThat(register.isDone()).isFalse();

            h.commit();   // the rename finishes

            assertThat(rename.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
            var r = register.get(60, TimeUnit.SECONDS);
            assertThat(r.statusCode()).as("refused once the rename committed: " + r.body()).isEqualTo(409);
            assertThat(r.body()).contains("label_reserved");
        } finally {
            pool.shutdownNow();
        }
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a))).as("nothing was registered under A: the INSERT rolled back").isNull();
    }

    /**
     * linda's census, item 3: every registry row a rename touches is locked in ONE statement ordered by collection_id (the old row, the tombstone at the new
     * label, the tombstones naming the old label, those pointing at the target tombstone's id). The renames that share registry rows are renames of ONE source
     * onto two of its own empty tombstones: the handler revives a tombstone only for the rename its {@code superseded_by} names (nexus-v6za0), so two renames of
     * different sources onto one tombstone never both reach the transaction, and on consistent data their row sets are disjoint. Here A has tombstones X and Y
     * (ids below A's) and T (above); A to X and A to Y both pass the handler, queue on A's sweep gate inside their transactions, and are released together. With
     * one ordered statement they serialise and one wins (the other answers 404 or 409). With the tombstones locked in a second statement, each holds its own
     * target and one holds A while it waits for the other's target: 40P01. DeadlockRetry absorbs a 40P01 (the loser retries and is refused cleanly), so the
     * status alone cannot show a deadlock; the belt's process-wide retry counter does (the mutant run is recorded on the bead).
     */
    @Test
    void oneSourceRenamedOntoTwoOfItsTombstonesAtOnce_serialisesWithoutADeadlockAndOneWins() throws Exception {
        for (int round = 0; round < 3; round++) {
            String tenant = "p37-lo-f" + round;
            String token = engine.newTenant(tenant);
            String a = coll("p37lofa" + round);
            String x = coll("p37lofx" + round);
            String y = coll("p37lofy" + round);
            String t = coll("p37loft" + round);
            engine.su(ctx -> {
                for (String n : List.of(x, y, a, t)) {   // x and y below a in id order, t above it
                    PgContainerHelper.insertCollection(ctx, tenant, n);
                }
                return null;
            });
            for (String tomb : List.of(x, y, t)) {
                var r = engine.call("POST", "/v1/catalog/collections/supersede", token, "{\"name\":\"" + tomb + "\",\"superseded_by\":\"" + a + "\"}");
                assertThat(r.statusCode()).as("non-vacuity: tombstone " + tomb + " of " + a + ": " + r.body()).isEqualTo(200);
            }
            var pool = Executors.newFixedThreadPool(2);
            try (Connection g = engine.pg.createConnection("")) {
                g.setAutoCommit(false);
                gate(DSL.using(g, SQLDialect.POSTGRES), "pg_advisory_xact_lock", tenant, a).fetch();   // both renames queue here, past the handler
                int advisoryBase = advisoryWaiters();
                long retriesBefore = dev.nexus.service.db.DeadlockRetry.retryAttemptCount();
                Future<Rdr229RenameEngine.Renamed> r1 = pool.submit(() -> engine.rename(token, a, x));
                Future<Rdr229RenameEngine.Renamed> r2 = pool.submit(() -> engine.rename(token, a, y));
                assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> advisoryWaiters() >= advisoryBase + 2))
                    .as("non-vacuity: both renames passed the handler and wait on the sweep gate inside their transactions").isTrue();
                g.commit();   // released together into the registry row locks
                var o1 = r1.get(120, TimeUnit.SECONDS);
                var o2 = r2.get(120, TimeUnit.SECONDS);
                assertThat(List.of(o1.status(), o2.status())).as("no 5xx: r1=" + o1 + " r2=" + o2).allMatch(s -> s == 200 || s == 404 || s == 409);
                assertThat(List.of(o1, o2).stream().filter(o -> o.status() == 200).count()).as("exactly one rename won: r1=" + o1 + " r2=" + o2)
                    .isEqualTo(1L);
                assertThat(dev.nexus.service.db.DeadlockRetry.retryAttemptCount() - retriesBefore)
                    .as("no deadlock, not even one the retry belt absorbed: r1=" + o1 + " r2=" + o2).isZero();
            } finally {
                pool.shutdownNow();
            }
        }
    }
}
