// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionMint;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), the post-confirmation round on the registry half of a rename, as a NOBYPASSRLS service role:
 * <ul>
 * <li>two renames of different sources onto one fresh name: the loser used to retire its source naming an id of its own
 *     draw that no row holds, then revive the winner's LIVE row; it is now a named 409 and its transaction leaves nothing;</li>
 * <li>lock order with the live-tuple index absent: the rename takes the advisory locks of both tuples (its source's and its
 *     new row's), in the order the batch paths take theirs, before it touches either row, so a rename cannot hold a row
 *     while it waits for a tuple a registration holds and wants that row.</li>
 * </ul>
 * Each leg that needs a rename to stand still at one point holds the lock it will wait on from a session of the test (no hook
 * in the engine): a row held {@code FOR UPDATE}, an advisory lock.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRenameConcurrencyTest {

    private static final String TENANT = "rename-concurrency-tenant";
    private static final String SVC_ROLE = "svc_rename_conc";
    private static final String SVC_PASS = "svc_rename_conc_pass";
    private static final String MODEL = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository repo;
    ExecutorService pool;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(6);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
        pool = Executors.newFixedThreadPool(4);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void indexAbsent() {
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private void indexState(boolean present) {
        if (present) {
            su(ctx -> {
                CollectionMintSupport.createIndex(ctx);
                return null;
            });
        }
        assertThat(su(CollectionMintSupport::indexPresent)).as("guard: the index state under test").isEqualTo(present);
    }

    private void register(String name, String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        repo.upsertCollection(TENANT, m, true);
    }

    private Record row(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.SUPERSEDED_BY,
                CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.OWNER_ID)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne());
    }

    /** Wait until some session is blocked on a lock, or fail when {@code racer} ended without being blocked. */
    private void awaitABlockedSession(Future<?> racer, String what) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (!su(CollectionMintSupport::someSessionWaitsOnALock)) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + " did not wait on a lock (done: " + racer.isDone() + ")");
            }
            Thread.sleep(25);
        }
    }

    private static Throwable failureOf(Future<?> f) throws Exception {
        try {
            f.get(60, TimeUnit.SECONDS);
            return null;
        } catch (ExecutionException e) {
            return e.getCause();
        }
    }

    // ── two renames of different sources onto one fresh name ────────────────────────────────────

    private void assertTheLoserOfTwoRenamesOntoOneNameIsANamedConflictAndLeavesNothing(String suffix) throws Exception {
        String x1 = "rc-x1-" + suffix;
        String x2 = "rc-x2-" + suffix;
        String y = "rc-y-" + suffix;
        register(x1, "rc-owner-1-" + suffix);
        register(x2, "rc-owner-2-" + suffix);

        Future<?> loser;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // X2's row is held, so the loser's rename stands still at the statement that retires X2: after it has looked at
            // Y (free) and drawn Y's id, before it writes Y
            DSL.using(holder, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x2)).forUpdate().fetch();
            loser = pool.submit(() -> repo.renameCollection(TENANT, x2, y));
            try {
                awaitABlockedSession(loser, "the rename of " + x2);
                repo.renameCollection(TENANT, x1, y);   // the winner registers Y while the loser stands still
            } finally {
                holder.commit();
            }
        }

        Throwable thrown = failureOf(loser);
        assertThat(thrown).as("the loser is refused, named, not left with a revived live row").isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining(x2).hasMessageContaining(y);
        Record winner = row(y);
        assertThat(winner.get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).as("Y is the winner's, live").isEqualTo("");
        assertThat(row(x1).get(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)).as("the winner's source names Y's id")
            .isEqualTo(winner.get(CATALOG_COLLECTIONS.COLLECTION_ID));
        assertThat(winner.get(CATALOG_COLLECTIONS.OWNER_ID)).as("and Y still has the winner's attributes").isEqualTo("rc-owner-1-" + suffix);
        Record untouched = row(x2);
        assertThat(untouched.get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).as("the loser's source is as it was").isEqualTo("");
        assertThat(untouched.get(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)).isNull();
        assertThat(successorIdsNamingNoRow()).as("no superseded_by_id names an id no row holds").isEmpty();
    }

    /** Every {@code superseded_by_id} of the tenant that no row of the tenant holds as its {@code collection_id}. */
    private List<Long> successorIdsNamingNoRow() {
        return su(ctx -> {
            var held = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).fetch(CATALOG_COLLECTIONS.COLLECTION_ID);
            return ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNotNull())
                .fetch(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).stream().filter(id -> !held.contains(id)).toList();
        });
    }

    @Test
    void twoRenamesOntoOneFreshName_theLoserIsANamedConflictAndLeavesNothing_withTheIndexAbsent() throws Exception {
        indexState(false);
        assertTheLoserOfTwoRenamesOntoOneNameIsANamedConflictAndLeavesNothing("off");
    }

    @Test
    void twoRenamesOntoOneFreshName_theLoserIsANamedConflictAndLeavesNothing_withTheIndexPresent() throws Exception {
        indexState(true);
        assertTheLoserOfTwoRenamesOntoOneNameIsANamedConflictAndLeavesNothing("on");
    }

    // ── lock order, the live-tuple index absent ─────────────────────────────────────────────────

    /** The advisory lock a registration of the tuple takes: nexus.collection_tuple_lock_key, as CollectionTupleLock reads it. */
    private static Field<Object> tupleLock(String owner) {
        return DSL.function("pg_advisory_xact_lock", Object.class,
            DSL.function(DSL.name("nexus", "collection_tuple_lock_key"), Long.class, DSL.val(TENANT), DSL.val("knowledge"),
                DSL.val(owner), DSL.val(MODEL), DSL.val("v1")));
    }

    /**
     * RDR-229 P3.8 (collections-003-1): with the live-tuple index present the UNIQUE is the arbiter, so a rename, a named
     * registration and a mint take no tuple lock. Another session holds the stripes of every tuple they touch; each must
     * finish anyway (knowledge collections: the catalog-051-2 owner triggers, which lock whatever the index state, skip them).
     */
    @Test
    void withTheIndexPresent_aRenameANamedRegistrationAndAMintTakeNoTupleLock() throws Exception {
        indexState(true);
        try {
            register("ip-x", "ip-owner-x");
            try (Connection holder = pg.createConnection("")) {
                holder.setAutoCommit(false);
                DSLContext h = DSL.using(holder, SQLDialect.POSTGRES);
                for (String owner : List.of("ip-owner-x", "ip-owner-y", "ip-owner-named", "ip-owner-mint")) {
                    h.select(tupleLock(owner)).fetch();
                }
                Future<?> rename = pool.submit(() -> repo.renameCollection(TENANT, "ip-x", "ip-y", null, "knowledge", "ip-owner-y"));
                Future<?> named = pool.submit(() -> {
                    register("ip-named", "ip-owner-named");
                    return null;
                });
                Future<?> mint = pool.submit(() -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("content_type", "knowledge");
                    m.put("owner_id", "ip-owner-mint");
                    m.put("embedding_model", MODEL);
                    m.put("model_version", "v1");
                    return repo.mintCollection(TENANT, m, true);
                });
                rename.get(20, TimeUnit.SECONDS);
                named.get(20, TimeUnit.SECONDS);
                mint.get(20, TimeUnit.SECONDS);
                holder.rollback();
            }
            assertThat(row("ip-y")).as("the rename landed while the stripes were held").isNotNull();
            assertThat(row("ip-named")).as("the named registration landed").isNotNull();
        } finally {
            su(ctx -> {
                CollectionMintSupport.dropIndex(ctx);
                return null;
            });
        }
    }

    @Test
    void aRenameTakesTheTupleLocksBeforeItTouchesEitherRow_withTheIndexAbsent() throws Exception {
        indexState(false);
        register("lo-x", "lo-owner-x");
        String newOwner = "lo-owner-y";

        Future<?> rename;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // a registration of the new row's tuple holds its lock: the rename waits for it
            DSL.using(holder, SQLDialect.POSTGRES).select(tupleLock(newOwner)).fetch();
            rename = pool.submit(() -> repo.renameCollection(TENANT, "lo-x", "lo-y", null, "knowledge", newOwner));
            try {
                awaitABlockedSession(rename, "the rename");
                // seen from another session, X's row is free: a rename that retired X first would hold it here, and a
                // registration that holds the tuple's lock and wants that row would wait for it, each for the other
                assertThatCode(() -> su(ctx -> ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq("lo-x"))
                        .forUpdate().noWait().fetch()))
                    .as("the rename holds X's row while it waits for the new row's tuple").doesNotThrowAnyException();
            } finally {
                holder.commit();
            }
        }

        assertThat(failureOf(rename)).as("the rename goes through once the tuple's lock is free").isNull();
        assertThat(row("lo-y").get(CATALOG_COLLECTIONS.OWNER_ID)).isEqualTo(newOwner);
        assertThat(row("lo-x").get(CATALOG_COLLECTIONS.SUPERSEDED_BY)).isEqualTo("lo-y");
    }

    @Test
    void crossingRenamesOntoEachOthersTuplesNeverDeadlock_withTheIndexAbsent() throws Exception {
        indexState(false);
        for (int round = 0; round < 15; round++) {
            String oa = "cx-owner-a-" + round;
            String ob = "cx-owner-b-" + round;
            String a = "cx-a-" + round;
            String b = "cx-b-" + round;
            register(a, oa);
            register(b, ob);
            var barrier = new CyclicBarrier(2);
            // A takes the tuple B holds and B the tuple A holds: neither can be made without the other first leaving its own
            Future<?> ra = pool.submit(() -> {
                barrier.await(30, TimeUnit.SECONDS);
                return repo.renameCollection(TENANT, a, a + "-moved", null, "knowledge", ob);
            });
            Future<?> rb = pool.submit(() -> {
                barrier.await(30, TimeUnit.SECONDS);
                return repo.renameCollection(TENANT, b, b + "-moved", null, "knowledge", oa);
            });
            for (Future<?> f : List.of(ra, rb)) {
                Throwable t = failureOf(f);
                if (t != null) {
                    assertThat(t).as("round %d: refused naming both collections, never a deadlock or an unnamed error", round)
                        .isInstanceOf(CollectionMint.LiveTupleConflict.class);
                }
            }
            assertThat(row(a)).isNotNull();
            assertThat(row(b)).isNotNull();
        }
    }
}
