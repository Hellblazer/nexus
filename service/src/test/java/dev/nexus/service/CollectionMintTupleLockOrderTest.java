// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionTupleLock;
import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.db.ExpectedCollectionIds;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the tuple lock comes BEFORE any registry read. A verb takes the tuple advisory lock and then the
 * registry rows {@code FOR UPDATE}; a registration that waited for a tuple lock while holding a registry key share would close a cycle
 * with it. No batch writer registers a name (the aspect, highlight and taxonomy imports only read the registry); the paths that
 * register (mint, named registration, import of collections, the rename's retire-and-register) take the tuple lock with no registry row
 * read in their transaction, and this test runs them with the index absent and the census on, where
 * {@code WriteRegistryMemo#assertNoneHeld} fails a tuple lock taken after a registry read.
 *
 * <p>The verb is a hand session: tuple lock, then the registry row {@code FOR UPDATE}. The real mint of the same tuple waits for the tuple
 * lock holding nothing, the verb takes its row at once and commits, the mint completes, and {@code DeadlockRetry} absorbed no retry.
 * {@link #negativeControl_aRegistrationThatReadsTheRegistryFirstDeadlocksWithTheVerb} runs the reverse order and shows PostgreSQL
 * aborting one side, so the assertion above can fail.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionMintTupleLockOrderTest {

    private static final String TENANT = "mint-lockorder-tenant";
    private static final String SVC_ROLE = "svc_mint_lockorder";
    private static final String SVC_PASS = "svc_mint_lockorder_pass";
    private static final String MODEL = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository repo;
    ExecutorService pool;
    boolean censusWas;
    int n;
    private final ConcurrentLinkedQueue<String> retries = new ConcurrentLinkedQueue<>();

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
        pool = Executors.newFixedThreadPool(3);
        censusWas = ExpectedCollectionIds.censusOn();
        ExpectedCollectionIds.setCensus(true);   // a tuple lock taken after a registry read throws
    }

    @AfterAll
    void stopAll() {
        ExpectedCollectionIds.setCensus(censusWas);
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** The state of an install before the re-own move completes: no live-tuple unique index, so the mint takes the tuple lock. */
    @BeforeEach
    void indexAbsent() {
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
        retries.clear();
        DeadlockRetry.setRetryListenerForTests(retries::add);
    }

    @AfterEach
    void noRetry() {
        DeadlockRetry.setRetryListenerForTests(null);
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Map<String, Object> attrs(String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        return m;
    }

    private static CollectionTupleLock.Tuple tuple(String owner) {
        return new CollectionTupleLock.Tuple(TENANT, "knowledge", owner, MODEL, "v1");
    }

    private int waitingAdvisory() {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse().and(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))));
    }

    private void awaitAdvisoryWaiter(Future<?> racer) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingAdvisory() < 1) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError("no advisory lock request ever showed waiting in pg_locks (racer done: " + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private static void lockRowForUpdate(Connection c, String name) {
        DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).forUpdate().fetch();
    }

    private static void lockRowForKeyShare(Connection c, String name) {
        DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).forKeyShare().fetch();
    }

    @Timeout(180)
    @Test
    void aMintOfATupleAVerbHolds_waitsForTheTupleLockHoldingNothing_andTheVerbTakesItsRowAtOnce() throws Exception {
        String ownerX = "lockorder-x-" + (n++);
        String x = (String) repo.mintCollection(TENANT, attrs(ownerX), true).get("name");   // the row the verb will retire
        String ownerY = "lockorder-y-" + (n++);
        boolean[] verbGot40P01 = new boolean[1];
        Future<Map<String, Object>> mint;
        try (Connection verb = pg.createConnection("")) {
            verb.setAutoCommit(false);
            CollectionTupleLock.lockTuple(DSL.using(verb, SQLDialect.POSTGRES), tuple(ownerY));   // the verb's first lock, the tuple's
            mint = pool.submit(() -> repo.mintCollection(TENANT, attrs(ownerY), true));
            try {
                awaitAdvisoryWaiter(mint);   // the registration is waiting for the tuple lock
                try {
                    lockRowForUpdate(verb, x);   // the verb's row: goes through at once unless the registration holds it (a cycle)
                } catch (DataAccessException e) {
                    if ("40P01".equals(e.sqlState())) {
                        verbGot40P01[0] = true;
                    } else {
                        throw e;
                    }
                }
            } finally {
                verb.commit();
            }
        }
        Map<String, Object> minted = mint.get(120, TimeUnit.SECONDS);
        assertThat(verbGot40P01[0]).as("the verb took its row while the registration waited: no deadlock").isFalse();
        assertThat(minted.get("owner_id")).as("the registration completed").isEqualTo(ownerY);
        assertThat(retries).as("no DeadlockRetry was needed").isEmpty();
    }

    @Timeout(180)
    @Test
    void aMintWithNoVerbRunsWithTheCensusOn_soItsTupleLockFollowsNoRegistryRead() {
        var row = repo.mintCollection(TENANT, attrs("lockorder-solo-" + (n++)), true);
        assertThat(row.get("name")).isNotNull();
        boolean indexPresent = su(ctx -> CollectionMintSupport.indexPresent(ctx));
        assertThat(indexPresent).as("setup: the index is absent, so the tuple lock was taken").isFalse();
    }

    @Timeout(180)
    @Test
    void negativeControl_aRegistrationThatReadsTheRegistryFirstDeadlocksWithTheVerb() throws Exception {
        String x = (String) repo.mintCollection(TENANT, attrs("lockorder-nc-x-" + (n++)), true).get("name");
        String ownerY = "lockorder-nc-y-" + (n++);
        int[] aborted = new int[2];
        try (Connection verb = pg.createConnection(""); Connection reg = pg.createConnection("")) {
            verb.setAutoCommit(false);
            reg.setAutoCommit(false);
            lockRowForKeyShare(reg, x);   // the wrong order: a registry read first
            CollectionTupleLock.lockTuple(DSL.using(verb, SQLDialect.POSTGRES), tuple(ownerY));   // the verb's tuple lock
            Future<?> verbSide = pool.submit(() -> {
                try {
                    lockRowForUpdate(verb, x);   // waits for the registration's key share
                } catch (DataAccessException e) {
                    if ("40P01".equals(e.sqlState())) {
                        aborted[0]++;
                        rollbackQuietly(verb);   // the aborted side's locks go with its rollback, which lets the other side finish
                    }
                }
            });
            Future<?> regSide = pool.submit(() -> {
                try {
                    while (!verbSideWaiting()) {
                        LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
                    }
                    CollectionTupleLock.lockTuple(DSL.using(reg, SQLDialect.POSTGRES), tuple(ownerY));   // waits for the verb: a cycle
                } catch (DataAccessException e) {
                    if ("40P01".equals(e.sqlState())) {
                        aborted[1]++;
                        rollbackQuietly(reg);
                    }
                }
            });
            verbSide.get(60, TimeUnit.SECONDS);
            regSide.get(60, TimeUnit.SECONDS);
            verb.rollback();
            reg.rollback();
        }
        assertThat(aborted[0] + aborted[1]).as("PostgreSQL aborted exactly one side of the cycle (40P01)").isEqualTo(1);
    }

    private static void rollbackQuietly(Connection c) {
        try {
            c.rollback();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private boolean verbSideWaiting() {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))) > 0);
    }
}
