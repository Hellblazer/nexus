// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.Catalog051OwnerGuardTest.DOCS_MODEL;
import static dev.nexus.service.Catalog051OwnerGuardTest.TENANT;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CollectionTupleLock;
import dev.nexus.service.db.CollectionTupleLock.Tuple;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.SQLDataType;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 (bead nexus-p2m1x.14), catalog-051-1a and {@link CollectionTupleLock}: the one per-tuple advisory-lock key
 * ({@code nexus.collection_tuple_lock_key}) that the catalog-051-2 triggers and every Java taker share, and the one order
 * ({@code lock}) in which several tuple locks are taken.
 */
class Catalog051TupleLockTest {

    /** The high 56 bits every key shares (collections-003-1): md5('16:catalog_tuple_lock'), first 64 bits, low byte cleared. */
    private static final long STRIPE_NAMESPACE = -8534080144102783744L;

    /**
     * The length-prefixed encoding's 64-bit hash, computed here in Java, independently of the SQL: md5 of
     * {@code '13:catalog_tuple' || len(p):p ...}, first 64 bits as a signed long (catalog-051-1a's per-tuple key).
     */
    private static long encodingHash(String... parts) {
        StringBuilder sb = new StringBuilder("13:catalog_tuple");
        for (String part : parts) {
            sb.append(part.length()).append(':').append(part);
        }
        try {
            byte[] d = java.security.MessageDigest.getInstance("MD5").digest(sb.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.nio.ByteBuffer.wrap(d, 0, 8).getLong();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void theSqlKeyIsTheStripeOfAnInjectiveEncodingOfTheFiveParts() throws Exception {
        Catalog051OwnerGuardTest.withMigratedDatabase((su, ctx) -> {
            String[] base = {"tenant-a", "docs", "1-70", "voyage-context-3", "v1"};
            long k = key(su, base);
            // stable, and pinned (an independent Python computation gives the same numbers), so a change of encoding,
            // hash, namespace or stripe count fails here and no two takers can silently part ways. collections-003-1
            // (RDR-229 P3.8, linda's ruling (a)) striped the key: the namespace's high 56 bits, the encoding hash's low 8.
            assertThat(key(su, base)).isEqualTo(k);
            assertThat(key(su, "t", "docs", "1-2", "m", "v1")).isEqualTo(-8534080144102783556L);
            assertThat(k).isEqualTo(-8534080144102783525L);
            assertThat(encodingHash("t", "docs", "1-2", "m", "v1")).as("catalog-051-1a's pinned per-tuple hash").isEqualTo(-7552408002309144644L);

            String[][] tuples = {
                base,
                {"tenant-b", "docs", "1-70", "voyage-context-3", "v1"},
                {"tenant-a", "code", "1-70", "voyage-context-3", "v1"},
                {"tenant-a", "docs", "1-71", "voyage-context-3", "v1"},
                {"tenant-a", "docs", "1-70", "voyage-code-3", "v1"},
                {"tenant-a", "docs", "1-70", "voyage-context-3", "v2"},
                // delimiter collisions: a boundary moved across a '/', ':' or digit, a value moved between parts
                {"tenant-a/docs", "1-70", "voyage-context-3", "v1", ""},
                {"tenant-a", "docs/1-70", "voyage-context-3", "v1", ""},
                {"tenant-a", "docs", "1-70/voyage-context-3", "v1", ""},
                {"tenant-a:docs", "1-70", "voyage-context-3", "v1", ""},
                {"tenant-a", "", "docs", "1-70", "voyage-context-3v1"},
                {"tenant-a1:docs", "1-70", "voyage-context-3", "v1", ""},
                {"tenant-a", "docs1:1-70", "voyage-context-3", "v1", ""},
                {"a/b", "c", "x", "y", "z"}, {"a", "b/c", "x", "y", "z"}, {"ab", "c", "x", "y", "z"}, {"a", "bc", "x", "y", "z"},
            };
            Set<Long> hashes = new HashSet<>();
            for (String[] t : tuples) {
                long h = encodingHash(t);
                // the encoding stays injective (a moved boundary is another hash), so a stripe is shared only by chance
                assertThat(hashes.add(h)).as("a distinct encoding hash for %s", String.join("|", t)).isTrue();
                assertThat(key(su, t)).as("the SQL key of %s is its hash's stripe", String.join("|", t))
                    .isEqualTo(STRIPE_NAMESPACE | (h & 0xFF));
            }
        });
    }

    @Test
    void theJavaHelperTakesTheLocksInAscendingKeyOrder() throws Exception {
        Catalog051OwnerGuardTest.withMigratedDatabase((su, ctx) -> {
            List<Tuple> tuples = new ArrayList<>();
            for (int i = 0; i < 3; i++) {
                tuples.add(new Tuple(TENANT, "docs", "1-9" + i, DOCS_MODEL, "v1"));
            }
            tuples.sort((x, y) -> Long.compare(CollectionTupleLock.key(ctx, x), CollectionTupleLock.key(ctx, y)));
            Tuple lo = tuples.get(0), mid = tuples.get(1), hi = tuples.get(2);
            long kLo = CollectionTupleLock.key(ctx, lo), kMid = CollectionTupleLock.key(ctx, mid),
                kHi = CollectionTupleLock.key(ctx, hi);
            assertThat(kLo).isLessThan(kMid);
            assertThat(kMid).isLessThan(kHi);
            // the helper reports ascending, deduplicated keys whatever the argument order
            try (Connection c = Catalog051OwnerGuardTest.openConnection(su)) {
                c.setAutoCommit(false);
                List<Long> locked = CollectionTupleLock.lock(DSL.using(c, SQLDialect.POSTGRES),
                    List.of(hi, lo, mid, lo));
                assertThat(locked).containsExactly(kLo, kMid, kHi);
                assertThat(CollectionTupleLock.lockTuple(DSL.using(c, SQLDialect.POSTGRES), mid)).isEqualTo(kMid);
                assertThat(CollectionTupleLock.lock(DSL.using(c, SQLDialect.POSTGRES), List.of())).isEmpty();
                c.rollback();
            }
            // and it really locks in that order: another transaction holds the MIDDLE key, so the helper (given the
            // tuples highest first) has taken the lowest and is waiting on the middle, and has not reached the highest
            try (Connection holder = Catalog051OwnerGuardTest.openConnection(su);
                 Connection taker = Catalog051OwnerGuardTest.openConnection(su)) {
                holder.setAutoCommit(false);
                taker.setAutoCommit(false);
                rawLock(holder, kMid);
                int takerPid = Catalog051OwnerGuardTest.backendPid(taker);
                taker.commit();
                CompletableFuture<List<Long>> f = CompletableFuture.supplyAsync(() ->
                    CollectionTupleLock.lock(DSL.using(taker, SQLDialect.POSTGRES), List.of(hi, mid, lo)));
                awaitLockWait(su, takerPid, f);
                assertThat(grantedAdvisoryKeys(su, takerPid)).as("the taker holds the lowest key and waits on the middle")
                    .containsExactly(kLo);
                holder.commit();
                assertThat(f.get(30, TimeUnit.SECONDS)).containsExactly(kLo, kMid, kHi);
                taker.commit();
            }
        });
    }

    /**
     * A Java holder of a tuple's lock (as the P3.8 mint is) and the catalog-051-2 trigger rewrite onto the same free tuple
     * take turns: connection B's document insert waits for A and proceeds after A commits. Without the shared key B
     * does not wait.
     */
    @Test
    void aTriggerRewriteWaitsForAJavaHolderOfTheSameFreeTuple() throws Exception {
        Catalog051OwnerGuardTest.withMigratedDatabase((su, ctx) -> {
            String slug = "docs__tl-aa000001__voyage-context-3__v1";
            Catalog051OwnerGuardTest.collection(ctx, slug, "docs", "tl-aa000001", DOCS_MODEL, "v1", "live", "");
            try (Connection a = Catalog051OwnerGuardTest.openConnection(su);
                 Connection b = Catalog051OwnerGuardTest.openConnection(su)) {
                a.setAutoCommit(false);
                b.setAutoCommit(false);
                int bPid = Catalog051OwnerGuardTest.backendPid(b);
                b.commit();
                CollectionTupleLock.lockTuple(DSL.using(a, SQLDialect.POSTGRES),
                    new Tuple(TENANT, "docs", "1-80", DOCS_MODEL, "v1"));
                CompletableFuture<List<String>> bResult = CompletableFuture.supplyAsync(
                    () -> Catalog051OwnerGuardTest.insertDocumentOn(b, "1.80.1", slug));
                awaitLockWait(su, bPid, bResult);
                assertThat(Catalog051OwnerGuardTest.ownerOf(ctx, slug)).as("B has not changed the row while A holds the lock")
                    .isEqualTo("tl-aa000001");
                a.commit();
                bResult.get(30, TimeUnit.SECONDS);
                b.commit();
                assertThat(Catalog051OwnerGuardTest.ownerOf(ctx, slug)).as("B proceeds after A and takes the free tuple")
                    .isEqualTo("1-80");
            }
        });
    }

    /**
     * Two Java callers take the same two tuples in opposite argument order, many times over; the helper's ascending order
     * means neither deadlocks. The control takes the same two locks by hand in opposite orders and PostgreSQL kills one
     * with 40P01, so the harness can see a deadlock when there is one.
     */
    @Test
    void twoCallersTakingTwoTuplesInOppositeOrderDoNotDeadlock() throws Exception {
        Catalog051OwnerGuardTest.withMigratedDatabase((su, ctx) -> {
            Tuple t1 = new Tuple(TENANT, "docs", "1-91", DOCS_MODEL, "v1");
            Tuple t2 = new Tuple(TENANT, "docs", "1-92", DOCS_MODEL, "v1");
            long k1 = CollectionTupleLock.key(ctx, t1), k2 = CollectionTupleLock.key(ctx, t2);

            // control: hand-ordered opposite locks deadlock
            try (Connection x = Catalog051OwnerGuardTest.openConnection(su);
                 Connection y = Catalog051OwnerGuardTest.openConnection(su)) {
                x.setAutoCommit(false);
                y.setAutoCommit(false);
                CyclicBarrier barrier = new CyclicBarrier(2);
                CompletableFuture<String> fx = CompletableFuture.supplyAsync(() -> rawLockPair(x, k1, k2, barrier));
                CompletableFuture<String> fy = CompletableFuture.supplyAsync(() -> rawLockPair(y, k2, k1, barrier));
                List<String> states = List.of(fx.get(30, TimeUnit.SECONDS), fy.get(30, TimeUnit.SECONDS));
                assertThat(states).as("exactly one of the two unordered takers is chosen as the deadlock victim")
                    .containsExactlyInAnyOrder("40P01", "ok");
                x.rollback();
                y.rollback();
            }

            // the helper: opposite argument orders, 40 rounds, no deadlock and no error
            try (Connection x = Catalog051OwnerGuardTest.openConnection(su);
                 Connection y = Catalog051OwnerGuardTest.openConnection(su)) {
                x.setAutoCommit(false);
                y.setAutoCommit(false);
                CyclicBarrier barrier = new CyclicBarrier(2);
                CompletableFuture<Void> fx = CompletableFuture.runAsync(() -> rounds(x, List.of(t1, t2), barrier));
                CompletableFuture<Void> fy = CompletableFuture.runAsync(() -> rounds(y, List.of(t2, t1), barrier));
                try {
                    fx.get(60, TimeUnit.SECONDS);
                    fy.get(60, TimeUnit.SECONDS);
                } catch (ExecutionException e) {
                    throw new AssertionError("a helper caller failed (a deadlock would be 40P01): " + e.getCause(), e);
                }
            }
        });
    }

    private static void rounds(Connection c, List<Tuple> order, CyclicBarrier barrier) {
        try {
            for (int i = 0; i < 40; i++) {
                barrier.await(30, TimeUnit.SECONDS);
                CollectionTupleLock.lock(DSL.using(c, SQLDialect.POSTGRES), order);
                c.commit();
            }
        } catch (Exception e) {
            try {
                c.rollback();
            } catch (SQLException ignored) {
                // the original failure is the one to report
            }
            throw new IllegalStateException(e);
        }
    }

    /** Takes {@code first}, waits for the peer to hold its own first lock, then takes {@code second}; "ok" or the SQLSTATE. */
    private static String rawLockPair(Connection c, long first, long second, CyclicBarrier barrier) {
        try {
            rawLock(c, first);
            barrier.await(30, TimeUnit.SECONDS);
            rawLock(c, second);
            c.commit();
            return "ok";
        } catch (DataAccessException e) {
            try {
                c.rollback();
            } catch (SQLException ignored) {
                // report the lock failure
            }
            return e.sqlState();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** One {@code pg_advisory_xact_lock} by hand, the way a caller that does not use the helper would take it. */
    private static void rawLock(Connection c, long key) {
        DSL.using(c, SQLDialect.POSTGRES)
            .select(DSL.function("pg_advisory_xact_lock", SQLDataType.VARCHAR, DSL.val(key))).fetch();
    }

    private static long key(Connection su, String... p) {
        return DSL.using(su, SQLDialect.POSTGRES)
            .select(DSL.function(DSL.name("nexus", "collection_tuple_lock_key"), SQLDataType.BIGINT,
                DSL.val(p[0]), DSL.val(p[1]), DSL.val(p[2]), DSL.val(p[3]), DSL.val(p[4])))
            .fetchOne(0, Long.class);
    }

    private static void awaitLockWait(Connection su, int pid, CompletableFuture<?> work) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!Catalog051OwnerGuardTest.isWaitingOnALock(su, pid)) {
            assertThat(work.isDone()).as("the writer must be waiting on the lock, not finished").isFalse();
            assertThat(System.nanoTime()).as("the writer never started waiting").isLessThan(deadline);
            Thread.sleep(50);
        }
    }

    /** The advisory keys {@code pid} holds (granted), rebuilt from pg_locks' (classid, objid) halves, ascending. */
    private static List<Long> grantedAdvisoryKeys(Connection su, int pid) {
        Field<Long> classid = DSL.field(DSL.name("classid"), Long.class);
        Field<Long> objid = DSL.field(DSL.name("objid"), Long.class);
        List<Long> keys = new ArrayList<>();
        for (var r : DSL.using(su, SQLDialect.POSTGRES).select(classid, objid)
                .from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
                .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
                .and(DSL.field(DSL.name("granted"), Boolean.class).isTrue())
                .and(DSL.field(DSL.name("pid"), Integer.class).eq(pid)).fetch()) {
            keys.add((r.get(classid) << 32) | r.get(objid));
        }
        keys.sort(null);
        return keys;
    }
}
