// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * The negative control of the "no deadlock was absorbed" assertion (linda's cross-lane rule, bead nexus-p2m1x.23): {@code DeadlockRetry}
 * turns a 40P01 into a clean answer, so a concurrency test that asserts statuses alone cannot see a deadlock, and the lock-order tests
 * of this branch assert, through {@link DeadlockRetry#setRetryListenerForTests}, that no retry happened. That assertion is only worth
 * something if it CAN fail. Here the sessions take the locks in the WRONG order on purpose: the writer takes the registry row
 * {@code FOR KEY SHARE} and then a child row (the chunk), while the verb takes the child row first and then {@code FOR UPDATE} on the
 * registry row (the reverse of the canonical order: registry row, then children). The two wait for each other, PostgreSQL breaks the
 * cycle with 40P01 after {@code deadlock_timeout}, and one of two things MUST show: the writer's retry (the listener fires) when the
 * writer is the victim, or the verb session's own 40P01 when it is.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class WriterLockOrderNegativeControlTest {

    private static final String TENANT = "lock-order-negative-control";
    private static final String SVC_ROLE = "svc_lock_order_nc";
    private static final String SVC_PASS = "svc_lock_order_nc_pass";
    private static final String CONTEXT = "lock-order-negative-control writer";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
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
        su(ctx -> {
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            return null;
        });
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        scope = new TenantScope(svcDs);
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        DeadlockRetry.setRetryListenerForTests(null);
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(java.util.function.Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private int waitingRequests() {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private static void childRow(DSLContext ctx, String collection) {
        ctx.select(CHUNKS.CHASH).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection)).forUpdate().fetch();
    }

    @Timeout(180)
    @Test
    void writerKeySharesTheRegistryRowThenAChild_whileTheVerbTakesTheChildThenTheRegistryRow_aDeadlockMustShow() throws Exception {
        String x = "knowledge__nc-" + System.nanoTime() + "__minilm-l6-v2-384__v1";
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, x);
            byte[] chash = HexFormat.of().parseHex(dev.nexus.service.db.Chash.ofText(x + "/chunk").toHex());
            PgContainerHelper.insertChunk384(ctx, TENANT, x, chash, VectorSeeds.vec(384, 0.1f));
            return null;
        });
        ConcurrentLinkedQueue<String> retries = new ConcurrentLinkedQueue<>();
        DeadlockRetry.setRetryListenerForTests(retries::add);
        CountDownLatch writerHoldsKeyShare = new CountDownLatch(1);
        CountDownLatch verbHoldsChild = new CountDownLatch(1);
        AtomicBoolean verbGot40P01 = new AtomicBoolean();
        AtomicInteger attempts = new AtomicInteger();
        try {
            // the verb, in the reverse order: the child row first, then FOR UPDATE on the registry row (which the writer's key share holds)
            Future<?> verb = pool.submit(() -> {
                try (Connection c = pg.createConnection("")) {
                    c.setAutoCommit(false);
                    DSLContext vc = DSL.using(c, SQLDialect.POSTGRES);
                    assertThat(writerHoldsKeyShare.await(60, TimeUnit.SECONDS)).isTrue();
                    childRow(vc, x);
                    verbHoldsChild.countDown();
                    try {
                        vc.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x)).forUpdate().fetch();
                    } catch (DataAccessException e) {
                        if ("40P01".equals(e.sqlState())) {
                            verbGot40P01.set(true);
                        } else {
                            throw e;
                        }
                    } finally {
                        c.rollback();
                    }
                }
                return null;
            });
            // the writer, in the canonical order: the registry row FOR KEY SHARE, then the child row, under the retry belt
            Future<?> writer = pool.submit(() -> DeadlockRetry.run(CONTEXT, () -> scope.withTenant(TENANT, ctx -> {
                CollectionRegistry.requireForWrite(ctx, TENANT, x);
                if (attempts.getAndIncrement() == 0) {
                    writerHoldsKeyShare.countDown();
                    try {
                        assertThat(verbHoldsChild.await(60, TimeUnit.SECONDS)).isTrue();
                        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
                        while (waitingRequests() < 1 && System.nanoTime() < deadline) {   // the verb now waits for the registry row
                            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
                        }
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }
                childRow(ctx, x);   // the verb holds it, and waits for our key share: a cycle
                return null;
            })));
            writer.get(120, TimeUnit.SECONDS);
            verb.get(120, TimeUnit.SECONDS);
        } finally {
            DeadlockRetry.setRetryListenerForTests(null);
        }
        assertThat(!retries.isEmpty() || verbGot40P01.get())
            .as("sessions that take the locks in opposite orders deadlock: the writer's retry or the verb's own 40P01 must show "
                + "(retries=%s, verb 40P01=%s), or the no-deadlock assertions of the lock-order tests cannot fail", retries, verbGot40P01.get())
            .isTrue();
        assertThat(retries).as("the only retries are the writer's").allMatch(CONTEXT::equals);
    }
}
