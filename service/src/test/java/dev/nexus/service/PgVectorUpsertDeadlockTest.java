// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-ps9wb — reproduces the pgvector DEADLOCK (SQLSTATE 40P01) on the
 * multi-row {@code INSERT ... ON CONFLICT (tenant_id, collection, chash) DO UPDATE}
 * in {@link PgVectorRepository#upsertChunks} under concurrent overlapping-key writes.
 *
 * <p><strong>Root cause.</strong> Two concurrent upsert batches into the SAME
 * collection that touch an overlapping set of chashes in DIFFERENT arrival orders
 * lock the shared rows in opposite orders within their single multi-row statement.
 * Batch A holds the lock on chash X and waits for Y; batch B holds Y and waits for
 * X → cycle → Postgres kills a victim with {@code deadlock detected} → the caller
 * sees HTTP 500. (Surfaced by the nexus-duoak.11 fastapi validation against
 * engine-service-v0.1.24, root-caused from engine logs by conexus.)
 *
 * <p><strong>Fix under test.</strong> {@code upsertChunksInternal} now sorts the
 * dedup'd rows by chash before building the INSERT, so every concurrent batch
 * acquires row locks in one global order and no cycle can form; a 40P01 retry belt
 * covers residual cross-path deadlocks. This test drives the exact adversarial shape
 * (two threads, one shared chash set, opposite orders, many iterations) and asserts
 * zero failures — pre-fix it deadlocks within a few iterations.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc} role,
 * {@link PgVectorRepositoryContractTest.FakeEmbedder}, direct repository calls (no
 * HTTP), PER_CLASS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorUpsertDeadlockTest {

    private static final String TENANT = "deadlock-tenant";
    // One collection, both threads write into it — the shared contention surface.
    private static final String COLLECTION = "code__deadlock__voyage-code-3__v1";

    // A batch of shared chashes both threads upsert; opposite orders invert the
    // lock-acquisition sequence, which is what deadlocks pre-fix.
    private static final int SHARED_CHASHES = 80;
    private static final int ITERATIONS     = 40;
    private static final int POOL_SIZE      = 4;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository pgRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(POOL_SIZE);
        cfg.setConnectionTimeout(5000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var tenantScope = new TenantScope(svcDs);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        pgRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        // Seed the collection once so registration is not part of the raced window.
        pgRepo.upsertChunks(TENANT, COLLECTION,
                sharedIds(), sharedDocs(), sharedMetas());
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void concurrentOppositeOrderUpsertsDoNotDeadlock() throws Exception {
        List<String> ascIds = sharedIds();
        List<String> descIds = new ArrayList<>(ascIds);
        Collections.reverse(descIds);

        List<Throwable> failures = new CopyOnWriteArrayList<>();
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);

        Runnable ascWorker = worker(ascIds, start, failures);
        Runnable descWorker = worker(descIds, start, failures);

        pool.submit(ascWorker);
        pool.submit(descWorker);
        start.countDown();  // fire both threads together
        pool.shutdown();
        boolean done = pool.awaitTermination(120, TimeUnit.SECONDS);

        assertThat(done).as("both upsert workers finished within the budget").isTrue();
        assertThat(failures)
            .as("no deadlock (SQLSTATE 40P01) or any other failure across %d opposite-order iterations",
                ITERATIONS)
            .isEmpty();
    }

    private Runnable worker(List<String> ids, CountDownLatch start, List<Throwable> failures) {
        // Each worker rebuilds docs/metas aligned to its id order.
        List<String> docs = new ArrayList<>(ids.size());
        List<Map<String, Object>> metas = new ArrayList<>(ids.size());
        for (String id : ids) {
            docs.add("doc-" + id);
            Map<String, Object> m = new HashMap<>();
            m.put("chash", id);
            metas.add(m);
        }
        return () -> {
            try {
                start.await();
                for (int it = 0; it < ITERATIONS; it++) {
                    pgRepo.upsertChunks(TENANT, COLLECTION, ids, docs, metas);
                }
            } catch (Throwable t) {
                failures.add(t);
            }
        };
    }

    private static List<String> sharedIds() {
        List<String> ids = new ArrayList<>(SHARED_CHASHES);
        for (int i = 0; i < SHARED_CHASHES; i++) {
            // Full 64-hex-char chash (RDR-180: the chunks_<dim> chash column is
            // bytea(32), CHECK octet_length=32 — the full sha256 digest, not the
            // pre-flip [:32] half-digest). Distinct per index, shared across
            // threads so the two workers contend on the same rows.
            ids.add(String.format("%064x", i + 1));
        }
        return ids;
    }

    private static List<String> sharedDocs() {
        List<String> docs = new ArrayList<>(SHARED_CHASHES);
        for (String id : sharedIds()) docs.add("doc-" + id);
        return docs;
    }

    private static List<Map<String, Object>> sharedMetas() {
        List<Map<String, Object>> metas = new ArrayList<>(SHARED_CHASHES);
        for (String id : sharedIds()) {
            Map<String, Object> m = new HashMap<>();
            m.put("chash", id);
            metas.add(m);
        }
        return metas;
    }
}
