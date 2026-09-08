// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-hxrcm: the have-vector metadata-only UPDATE batch
 * ({@link PgVectorRepository#batchUpdateMetadata}) deadlocked (SQLSTATE 40P01)
 * in the cloud six times after the v0.1.104 flip, always inside
 * {@code POST /v1/catalog/manifest/write_many}, pairs of calls about one second
 * apart on one collection.
 *
 * <p><strong>Root cause.</strong> The same shape nexus-ps9wb fixed for the
 * multi-row INSERT: each statement in the JDBC batch takes one row lock, in the
 * CALLER'S index order. The direct upsert path sorts its dedup list by chash
 * before {@code resolveNeedEmbedIdx}, so it was already safe; the combined-write
 * path ({@code CombinedWriteService.writeManyCombined} phase 2a) feeds the batch
 * in arrival order, so two overlapping batches in different orders lock the
 * shared rows in opposite orders and one is killed as the deadlock victim.
 *
 * <p><strong>Fix under test.</strong> {@code batchUpdateMetadata} orders the
 * indices it updates by chash before building the batch, so every caller gets
 * one global lock order regardless of what order it passed. This test drives two
 * threads over one shared chash set in opposite orders through the primitive
 * itself, under the same {@link TenantScope#withTenant} transaction shape the
 * combined-write path uses, and asserts zero failures. Pre-fix it deadlocks
 * within a few iterations.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorMetadataUpdateDeadlockTest {

    private static final String SVC_ROLE = "svc_meta_deadlock_test";
    private static final String SVC_PASS = "svc_meta_deadlock_test_pass";
    private static final String TENANT = "meta-deadlock-tenant";
    private static final String COLLECTION = "code__metadeadlock__voyage-code-3__v1";
    private static final int DIM = 1024;

    private static final int SHARED_CHASHES = 120;
    private static final int ITERATIONS     = 40;
    private static final int POOL_SIZE      = 4;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository pgRepo;

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
        cfg.setMaximumPoolSize(POOL_SIZE);
        cfg.setConnectionTimeout(5000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(DIM);
        pgRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        // RDR-204 Phase 1 (bead nexus-ft04v.7): chunks_collection_fk is a REAL,
        // always-enforced FK now -- PgVectorRepository's stub-insert is retired, so
        // a real row must exist before any chunk write, or upsertChunks below fails
        // loud instead of exercising the metadata-update deadlock race.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }
        // Seed every shared row once; the raced window is the metadata UPDATE only.
        pgRepo.upsertChunks(TENANT, COLLECTION, sharedIds(), sharedDocs(), metasFor(sharedIds(), 0));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void concurrentOppositeOrderMetadataUpdatesDoNotDeadlock() throws Exception {
        List<String> ascIds = sharedIds();
        List<String> descIds = new ArrayList<>(ascIds);
        Collections.reverse(descIds);

        List<Throwable> failures = new CopyOnWriteArrayList<>();
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        pool.submit(worker(ascIds, start, failures));
        pool.submit(worker(descIds, start, failures));
        start.countDown();
        pool.shutdown();
        boolean done = pool.awaitTermination(120, TimeUnit.SECONDS);

        assertThat(done).as("both metadata-update workers finished within the budget").isTrue();
        assertThat(failures)
            .as("no deadlock (SQLSTATE 40P01) or any other failure across %d opposite-order iterations",
                ITERATIONS)
            .isEmpty();
    }

    @Test
    void everyRowIsUpdatedAndZeroAffectedIsReportedForMissingChash() {
        List<String> ids = new ArrayList<>(sharedIds());
        Collections.reverse(ids);
        String missing = String.format("%064x", 0xdead_beefL);
        ids.add(2, missing);   // a chash that has no row, placed mid-list
        List<Map<String, Object>> metas = metasFor(ids, 7);
        List<Integer> all = new ArrayList<>();
        for (int i = 0; i < ids.size(); i++) all.add(i);

        DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
        List<Integer> zero = tenantScope.withTenant(TENANT, ctx ->
            PgVectorRepository.batchUpdateMetadata(ctx, ch, COLLECTION, ids, metas, all));

        assertThat(zero)
            .as("the only zero-affected index is the missing chash, reported by the CALLER'S index")
            .containsExactly(2);
    }

    @Test
    void zeroAffectedIndicesComeBackInChashOrderNotCallerOrder() {
        // Three missing chashes scattered through a REVERSED list: the caller's index
        // order is 1, 5, 9 (descending chash); the statements run in chash order, so the
        // report comes back 9, 5, 1. Pins the documented return-order contract on its
        // own merits rather than on both current callers happening to addAll.
        List<String> ids = new ArrayList<>(sharedIds());
        Collections.reverse(ids);
        ids.add(1, String.format("%064x", 0xf000L));
        ids.add(5, String.format("%064x", 0xe000L));
        ids.add(9, String.format("%064x", 0xd000L));
        List<Map<String, Object>> metas = metasFor(ids, 3);
        List<Integer> all = new ArrayList<>();
        for (int i = 0; i < ids.size(); i++) all.add(i);

        DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
        List<Integer> zero = tenantScope.withTenant(TENANT, ctx ->
            PgVectorRepository.batchUpdateMetadata(ctx, ch, COLLECTION, ids, metas, all));

        assertThat(zero).containsExactly(9, 5, 1);
    }

    @Test
    void directUpsertMetadataRefresh_waitsForExternalExclusiveSweepGate() throws Exception {
        // nexus-hxrcm residual on the DIRECT path: resolveNeedEmbedIdx's have-vector
        // metadata-only UPDATE now takes the SHARED sweep gate. With an EXCLUSIVE holder on
        // the key, an identical-text re-upsert (metadata-only) must wait, not race the sweep.
        List<String> ids = List.of(sharedIds().get(0));
        List<String> docs = List.of("doc-" + ids.get(0));
        List<Map<String, Object>> newMeta = metasFor(ids, 99);

        ExecutorService pool = Executors.newSingleThreadExecutor();
        try (Connection external = svcDs.getConnection()) {
            external.setAutoCommit(false);
            DSLContext ext = DSL.using(external);
            ext.select(DSL.function("pg_advisory_xact_lock", Object.class,
                    DSL.function("hashtext", Integer.class,
                        DSL.val("sweepgate:" + TENANT + "/" + COLLECTION)))).fetch();

            Future<?> refresh = pool.submit(() -> pgRepo.upsertChunks(TENANT, COLLECTION, ids, docs, newMeta));
            assertThatThrownBy(() -> refresh.get(1500, TimeUnit.MILLISECONDS))
                .as("the have-vector metadata refresh must BLOCK on the exclusive holder")
                .isInstanceOf(TimeoutException.class);
            assertThat(roundOf(ids.get(0))).as("nothing landed while held").isNotEqualTo(99);

            external.rollback();
            refresh.get(30, TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
        assertThat(roundOf(ids.get(0))).as("landed once the gate was released").isEqualTo(99);
    }

    /** The {@code round} field of the stored metadata for one seeded chash. */
    private int roundOf(String hexChash) {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
        return tenantScope.withTenant(TENANT, ctx -> {
            var r = ctx.select(ch.metadata()).from(ch.table())
                .where(ch.collection().eq(COLLECTION).and(ch.chash().eq(hexChash)))
                .fetchOne();
            String json = r.value1().data();
            var m = java.util.regex.Pattern.compile("\"round\":\\s*(\\d+)").matcher(json);
            if (!m.find()) throw new AssertionError("no round in " + json);
            return Integer.parseInt(m.group(1));
        });
    }

    private Runnable worker(List<String> ids, CountDownLatch start, List<Throwable> failures) {
        List<Integer> all = new ArrayList<>(ids.size());
        for (int i = 0; i < ids.size(); i++) all.add(i);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
        return () -> {
            try {
                start.await();
                for (int it = 0; it < ITERATIONS; it++) {
                    List<Map<String, Object>> metas = metasFor(ids, it);
                    List<Integer> zero = tenantScope.withTenant(TENANT, ctx ->
                        PgVectorRepository.batchUpdateMetadata(ctx, ch, COLLECTION, ids, metas, all));
                    if (!zero.isEmpty()) {
                        throw new AssertionError("seeded rows reported zero-affected: " + zero);
                    }
                }
            } catch (Throwable t) {
                failures.add(t);
            }
        };
    }

    private static List<String> sharedIds() {
        List<String> ids = new ArrayList<>(SHARED_CHASHES);
        for (int i = 0; i < SHARED_CHASHES; i++) ids.add(String.format("%064x", i + 1));
        return ids;
    }

    private static List<String> sharedDocs() {
        List<String> docs = new ArrayList<>(SHARED_CHASHES);
        for (String id : sharedIds()) docs.add("doc-" + id);
        return docs;
    }

    private static List<Map<String, Object>> metasFor(List<String> ids, int round) {
        List<Map<String, Object>> metas = new ArrayList<>(ids.size());
        for (String id : ids) {
            Map<String, Object> m = new HashMap<>();
            m.put("chash", id);
            m.put("round", round);
            metas.add(m);
        }
        return metas;
    }
}
