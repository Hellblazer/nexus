// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.CollectionMintSupport;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28, linda's ruling (a)): the per-tuple advisory lock of the window before the live-tuple index
 * exists is striped. {@code nexus.collection_tuple_lock_key} maps every tuple to one of 256 keys in one namespace, so a batch
 * that locks many tuples (an import, a restore) takes at most 256 locks, and every locker, Java and the catalog-051
 * triggers alike, takes the key from that one function. Once the index exists it is the arbiter and no lock is taken.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionTupleLockStripeTest {

    private static final String T = "tuple-stripe";
    private static final String SVC_ROLE = "svc_tuple_stripe";
    private static final String SVC_PASS = "svc_tuple_stripe_pass";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final int STRIPES = 256;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <R> R su(Function<DSLContext, R> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Field<Long> key(String tenant, String contentType, String owner, String model, String version) {
        return DSL.function(DSL.name("nexus", "collection_tuple_lock_key"), SQLDataType.BIGINT,
            DSL.val(tenant, SQLDataType.CLOB), DSL.val(contentType, SQLDataType.CLOB), DSL.val(owner, SQLDataType.CLOB),
            DSL.val(model, SQLDataType.CLOB), DSL.val(version, SQLDataType.CLOB));
    }

    /** The advisory locks this backend holds. */
    private static int advisoryLocksHeld(DSLContext ctx) {
        var locks = DSL.table(DSL.name("pg_catalog", "pg_locks"));
        return ctx.fetchCount(ctx.selectOne().from(locks)
            .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
            .and(DSL.field(DSL.name("pid"), Integer.class).eq(DSL.function("pg_backend_pid", Integer.class))));
    }

    /** The distinct stripes of {@link #freshRows}'s tuples, read from the SQL function one tuple a statement (no lock taken). */
    private long distinctStripes(String prefix, int n) {
        return su(ctx -> {
            java.util.Set<Long> keys = new java.util.HashSet<>();
            for (int i = 0; i < n; i++) {
                keys.add(ctx.select(key(T, "knowledge", prefix + i, MODEL, "v1")).fetchOne(0, Long.class));
            }
            return (long) keys.size();
        });
    }

    private static List<CollectionTupleLock.Tuple> tuples(String prefix, int n) {
        List<CollectionTupleLock.Tuple> out = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            out.add(new CollectionTupleLock.Tuple(T, "knowledge", prefix + i, MODEL, "v1"));
        }
        return out;
    }

    private static List<Map<String, Object>> freshRows(String prefix, int n) {
        List<Map<String, Object>> rows = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", "knowledge__" + prefix + i + "__" + MODEL + "__v1");
            m.put("content_type", "knowledge");
            m.put("owner_id", prefix + i);
            m.put("embedding_model", MODEL);
            m.put("model_version", "v1");
            rows.add(m);
        }
        return rows;
    }

    // ── the key ──────────────────────────────────────────────────────────────────────

    @Test
    void everyTupleKeyIsOneOf256StripesOfOneNamespace_includingTheEdgeCases() {
        List<Long> keys = su(ctx -> {
            List<Long> out = new ArrayList<>();
            for (int i = 0; i < 4000; i++) {
                out.add(ctx.select(key("t" + (i % 7), "knowledge", "owner-" + i, MODEL, "v" + (i % 3))).fetchOne(0, Long.class));
            }
            out.add(ctx.select(key(T, "knowledge", "", MODEL, "v1")).fetchOne(0, Long.class));
            out.add(ctx.select(key(T, "knowledge", null, MODEL, "v1")).fetchOne(0, Long.class));
            out.add(ctx.select(key(T, "code", "o", "m".repeat(500), "")).fetchOne(0, Long.class));
            out.add(ctx.select(key("", "", "", "", "")).fetchOne(0, Long.class));
            return out;
        });
        assertThat(keys).doesNotContainNull();
        assertThat(keys.stream().map(k -> k >> 8).distinct().count()).as("one namespace: the high 56 bits are the same").isEqualTo(1);
        long stripes = keys.stream().map(k -> k & 0xFF).distinct().count();
        assertThat(stripes).as("at most %d stripes", STRIPES).isLessThanOrEqualTo(STRIPES);
        assertThat(stripes).as("non-vacuity: 4000 tuples spread over most stripes").isGreaterThan(200);
        Long first = su(ctx -> ctx.select(key(T, "knowledge", "o", MODEL, "v1")).fetchOne(0, Long.class));
        Long again = su(ctx -> ctx.select(key(T, "knowledge", "o", MODEL, "v1")).fetchOne(0, Long.class));
        assertThat(again).as("a tuple's key is stable").isEqualTo(first);
    }

    // ── one derivation ───────────────────────────────────────────────────────────────

    @Test
    void aRegistrationWaitsOnTheKeyACatalog051TriggerTakes_theOneDerivation() throws Exception {
        String owner = "derivation-owner";
        CountDownLatch held = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        // The trigger's lock: pg_advisory_xact_lock(nexus.collection_tuple_lock_key(...)), taken in SQL, held open.
        CompletableFuture<Void> trigger = CompletableFuture.runAsync(() -> {
            try (Connection c = pg.createConnection("")) {
                c.setAutoCommit(false);
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
                ctx.select(DSL.function("pg_advisory_xact_lock", SQLDataType.VARCHAR, key(T, "knowledge", owner, MODEL, "v1"))).fetch();
                held.countDown();
                release.await(30, TimeUnit.SECONDS);
                c.rollback();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(held.await(10, TimeUnit.SECONDS)).isTrue();
        CompletableFuture<Void> registration = CompletableFuture.runAsync(() -> scope.withTenant(T, ctx -> {
            CollectionMint.refuseHeldTuple(ctx, T, "knowledge__derivation__" + MODEL + "__v1", "knowledge", owner, MODEL, "v1",
                "live", "");
            return null;
        }));
        Thread.sleep(1500);
        assertThat(registration.isDone()).as("the registration waits on the lock the trigger holds: one key derivation").isFalse();
        release.countDown();
        registration.get(20, TimeUnit.SECONDS);
        trigger.get(20, TimeUnit.SECONDS);
    }

    // ── a bounded lock count ─────────────────────────────────────────────────────────

    @Test
    void aLargeImportWithTheIndexAbsentTakesAtMost256TupleLocks() {
        int held = scope.withTenant(T, ctx -> {
            CollectionMint.refuseLiveTupleConflicts(ctx, T, freshRows("bulk-", 5000));
            return advisoryLocksHeld(ctx);
        });
        assertThat(held).as("5000 tuples: exactly one lock for each stripe they fall in, no more and none dropped")
            .isEqualTo((int) distinctStripes("bulk-", 5000)).isLessThanOrEqualTo(STRIPES);
    }

    @Test
    void theSharedPrimitiveLocksEachStripeOfItsTuplesOnceInAscendingOrder_acrossKeyChunks() {
        // CollectionTupleLock.lock is what every batch caller takes its locks through (an import, a rename, a restore's
        // TenantImportLoad); 2500 tuples read their keys over three statements of KEYS_PER_STATEMENT.
        List<CollectionTupleLock.Tuple> in = tuples("prim-", 2500);
        List<Object> result = scope.withTenant(T, ctx -> {
            List<Long> locked = CollectionTupleLock.lock(ctx, in);
            return List.of(locked, advisoryLocksHeld(ctx));
        });
        @SuppressWarnings("unchecked") List<Long> locked = (List<Long>) result.get(0);
        long expected = distinctStripes("prim-", 2500);
        assertThat(locked).as("one key a stripe, ascending").hasSize((int) expected).isSorted().doesNotHaveDuplicates();
        assertThat((Integer) result.get(1)).as("held, as reported").isEqualTo((int) expected);
    }

    @Test
    void twoConcurrentBatchesWhoseStripesInterleaveBothComplete() throws Exception {
        CountDownLatch go = new CountDownLatch(1);
        List<CompletableFuture<Integer>> batches = new ArrayList<>();
        for (String prefix : List.of("left-", "right-")) {
            batches.add(CompletableFuture.supplyAsync(() -> {
                try {
                    go.await();
                } catch (InterruptedException e) {
                    throw new IllegalStateException(e);
                }
                return scope.withTenant(T, ctx -> {
                    CollectionMint.refuseLiveTupleConflicts(ctx, T, freshRows(prefix, 1500));
                    return advisoryLocksHeld(ctx);
                });
            }));
        }
        go.countDown();
        assertThat(batches.get(0).get(60, TimeUnit.SECONDS)).as("the left batch completes, holding each of its stripes")
            .isEqualTo((int) distinctStripes("left-", 1500));
        assertThat(batches.get(1).get(60, TimeUnit.SECONDS)).as("the right batch completes, holding each of its stripes")
            .isEqualTo((int) distinctStripes("right-", 1500));
    }

    @Test
    void withTheIndexPresentNoTupleLockIsTaken_theUniqueIsTheArbiter() {
        su(ctx -> {
            CollectionMintSupport.createIndex(ctx);
            return null;
        });
        try {
            int held = scope.withTenant(T, ctx -> {
                CollectionMint.refuseLiveTupleConflicts(ctx, T, freshRows("indexed-", 50));
                CollectionMint.refuseHeldTuple(ctx, T, "knowledge__indexed-one__" + MODEL + "__v1", "knowledge", "indexed-one",
                    MODEL, "v1", "live", "");
                return advisoryLocksHeld(ctx);
            });
            assertThat(held).isZero();
        } finally {
            su(ctx -> {
                CollectionMintSupport.dropIndex(ctx);
                return null;
            });
        }
    }
}
