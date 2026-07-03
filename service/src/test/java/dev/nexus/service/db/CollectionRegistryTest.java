// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-h8rf6.2 — deterministic, non-flaky proof of the {@link CollectionRegistry}
 * contention-relief mechanism.
 *
 * <p>{@code ChashVectorConcurrencyTest} demonstrates the bug and fix end-to-end over real
 * HTTP + a real connection pool, but wall-clock races over Testcontainers' near-zero-RTT
 * localhost Postgres are inherently timing-sensitive. This suite proves the SAME mechanism
 * directly and deterministically: hold an uncommitted lock on a {@code catalog_collections}
 * row on one connection, then call {@link ChashRepository#upsert} for that exact
 * {@code (tenant, collection)} on another thread and observe whether it blocks.
 *
 * <ul>
 *   <li><strong>Never-registered collection, no cache:</strong> {@code upsert} blocks —
 *       its {@code ensureCollectionRegistered} INSERT races the held (first-ever-insert)
 *       row lock. This is the documented, UNCHANGED-by-design first-touch cost (see
 *       {@link CollectionRegistry} class doc) — the fix does not (and cannot) eliminate
 *       it.</li>
 *   <li><strong>Already-registered collection, no cache:</strong> {@code upsert} STILL
 *       blocks against a concurrent UPDATE lock on that row — empirically verified here
 *       (Postgres blocks {@code INSERT ... ON CONFLICT DO NOTHING} on ANY uncommitted
 *       concurrent write to the conflicting row, not merely a concurrent INSERT). This is
 *       what makes the bug RECURRING, not a one-off startup cost: every repeat
 *       registration attempt for a collection another process happens to be concurrently
 *       touching pays this tax, for the collection's whole lifetime.</li>
 *   <li><strong>Already-registered collection, cached:</strong> {@code upsert} returns
 *       immediately — {@code ensureCollectionRegistered} skips the INSERT entirely, so it
 *       never contends for the held row lock at all. This is the fix.</li>
 * </ul>
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc} role (full DML via
 * {@code grants-nexus-svc.xml}), PER_CLASS. {@link CollectionRegistry#clearForTests()} runs
 * after each test so the process-static cache never leaks state between test methods —
 * each method also uses a distinct collection name as defense-in-depth.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRegistryTest {

    private static final String TENANT = "cr-contention-tenant";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    ChashRepository repo;

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
            su.createStatement().execute("ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        // Pool sized 2: one connection for the blocker, one for the repo call under test —
        // proves the mechanism without needing a big pool or wall-clock racing.
        cfg.setMaximumPoolSize(2);
        cfg.setConnectionTimeout(15000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        repo = new ChashRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @AfterEach
    void clearCache() {
        CollectionRegistry.clearForTests();
    }

    // -------------------------------------------------------------------------
    // Helper: hold an uncommitted lock on the catalog_collections row for
    // `collection`, simulating a concurrent writer mid-registration.
    // -------------------------------------------------------------------------

    /** A held, uncommitted registration lock; call {@link #release()} to free it. */
    private final class HeldLock implements AutoCloseable {
        final Connection conn;

        /**
         * @param collection the (tenant, collection) row to lock
         * @param rowExists  false: lock via an uncommitted first-ever INSERT (the
         *                   row does not exist yet — a genuine first-touch race).
         *                   true: lock via an uncommitted UPDATE of an ALREADY
         *                   committed row (a second concurrent writer touching the
         *                   same collection after it is already registered) — a
         *                   {@code INSERT ... ON CONFLICT} targeting this row still
         *                   has to wait for this UPDATE's row lock to release
         *                   before it can determine the conflict outcome.
         */
        HeldLock(String collection, boolean rowExists) throws Exception {
            conn = svcDs.getConnection();
            conn.setAutoCommit(false);
            try (var ps = conn.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
                ps.setString(1, TENANT);
                ps.execute();
            }
            if (rowExists) {
                try (var ps = conn.prepareStatement(
                        "UPDATE nexus.catalog_collections SET name = name "
                        + "WHERE tenant_id = ? AND name = ?")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, collection);
                    int updated = ps.executeUpdate();
                    if (updated != 1) {
                        throw new IllegalStateException(
                            "expected row to already exist for rowExists=true: " + collection);
                    }
                }
            } else {
                try (var ps = conn.prepareStatement(
                        "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                        + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, collection);
                    ps.execute();
                }
            }
            // Deliberately NOT committed — the row lock is held until release().
        }

        void release() throws Exception {
            conn.commit();
            conn.close();
        }

        @Override
        public void close() throws Exception {
            release();
        }
    }

    // -------------------------------------------------------------------------
    // Test 1: cache MISS — upsert blocks on the held lock (documented, unchanged
    // first-touch cost; NOT what the fix targets).
    // -------------------------------------------------------------------------

    @Test
    void upsert_blocksOnHeldLock_whenCollectionNotCached() throws Exception {
        String collection = "code__cr-miss__voyage-code-3__v1";
        assertThat(CollectionRegistry.isKnown(TENANT, collection)).isFalse();

        try (HeldLock lock = new HeldLock(collection, false)) {
            var executor = Executors.newSingleThreadExecutor();
            CountDownLatch started = new CountDownLatch(1);
            CompletableFuture<Void> upsertDone = CompletableFuture.runAsync(() -> {
                started.countDown();
                repo.upsert(TENANT, "miss_chash", collection);
            }, executor);

            assertThat(started.await(5, TimeUnit.SECONDS)).isTrue();
            // The upsert call is racing the held (uncommitted) row lock — it must NOT
            // complete while the lock is held.
            assertThat(upsertDone)
                .as("upsert must block while a concurrent transaction holds an "
                    + "uncommitted registration for the same (tenant, collection) — "
                    + "this is the pre-existing, correct-by-design first-touch cost")
                .failsWithin(500, TimeUnit.MILLISECONDS);

            executor.shutdownNow();
        }
        // Lock released by try-with-resources — any still-running upsert can now proceed;
        // no further assertion needed (the block above is the point of this test).
    }

    // -------------------------------------------------------------------------
    // Test 2: cache HIT — upsert completes immediately even while a SECOND
    // concurrent writer holds an uncommitted lock on the (already-registered) row
    // — the fix: ensureCollectionRegistered skips the INSERT entirely once known,
    // so it never contends for that lock in the first place.
    // -------------------------------------------------------------------------

    @Test
    void upsert_skipsRegistration_whenCollectionCached() throws Exception {
        String collection = "code__cr-hit__voyage-code-3__v1";

        // Real registration via the actual code path: this both creates the
        // catalog_collections row AND marks CollectionRegistry known post-commit
        // (ChashRepository.upsert's own contract — see CollectionRegistry class doc).
        repo.upsert(TENANT, "seed_chash", collection);
        assertThat(CollectionRegistry.isKnown(TENANT, collection))
            .as("upsert() must mark the collection known after a successful commit")
            .isTrue();

        // A second writer now holds an uncommitted UPDATE lock on that SAME,
        // already-registered row — simulating another concurrent indexing worker
        // touching the collection. Pre-fix, ensureCollectionRegistered's
        // INSERT ... ON CONFLICT DO NOTHING would have to wait for this lock to
        // determine the conflict outcome; with the cache, it never issues that
        // INSERT at all.
        try (HeldLock lock = new HeldLock(collection, true)) {
            assertThat(CompletableFuture.runAsync(() -> repo.upsert(TENANT, "hit_chash", collection)))
                .as("upsert must complete immediately when CollectionRegistry already "
                    + "knows the collection — ensureCollectionRegistered must skip the "
                    + "redundant INSERT entirely, never contending for the held lock")
                .succeedsWithin(2, TimeUnit.SECONDS);
        }

        var rows = repo.lookup(TENANT, "hit_chash");
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection")).isEqualTo(collection);
    }

    /**
     * Negative control for Test 2: WITHOUT the cache (freshly cleared), the SAME
     * held-UPDATE-lock-on-an-ALREADY-registered-row scenario ALSO blocks {@code
     * upsert} when the cache is cleared — verified empirically (this test failed
     * loudly on first write, disproving the assumption that {@code INSERT ...
     * ON CONFLICT DO NOTHING} against a merely-being-updated row is lock-free;
     * Postgres blocks on ANY uncommitted concurrent write to the conflicting row,
     * not only a concurrent INSERT). This is the more general and more accurate
     * statement of the bug: EVERY repeat registration attempt for a collection
     * that some other transaction happens to be concurrently touching pays the
     * lock-wait tax, for as long as that process's writes to
     * {@code catalog_collections} keep happening — which, absent the cache, is
     * every single batch write for the life of an indexing run. See
     * {@link #upsert_blocksOnHeldLock_whenCollectionNotCached} for the sibling
     * first-touch-race case; together they show the cache is not merely a
     * throughput micro-optimization but removes a real, recurring lock-wait
     * hazard for the whole collection's lifetime, not just its first touch.
     */
    @Test
    void upsert_alsoBlocks_whenLockHeldOnRegisteredRow_andCacheCleared() throws Exception {
        String collection = "code__cr-control__voyage-code-3__v1";
        repo.upsert(TENANT, "seed_chash2", collection);
        // Deliberately clear the cache AFTER seeding, so ensureCollectionRegistered
        // WILL attempt the INSERT again for the next call (pre-fix-equivalent path).
        CollectionRegistry.clearForTests();
        assertThat(CollectionRegistry.isKnown(TENANT, collection)).isFalse();

        try (HeldLock lock = new HeldLock(collection, true)) {
            assertThat(CompletableFuture.runAsync(() -> repo.upsert(TENANT, "control_chash", collection)))
                .as("without the cache, a concurrent UPDATE lock on an ALREADY-registered "
                    + "row still blocks the next upsert's redundant ON CONFLICT DO NOTHING — "
                    + "the recurring lock-wait hazard the cache eliminates")
                .failsWithin(500, TimeUnit.MILLISECONDS);
        }
    }

    // -------------------------------------------------------------------------
    // Test 3: pure cache semantics (no DB) — fast sanity on isKnown/markKnown.
    // -------------------------------------------------------------------------

    @Test
    void isKnown_falseByDefault_trueAfterMarkKnown_scopedPerTenantAndCollection() {
        assertThat(CollectionRegistry.isKnown("t1", "c1")).isFalse();
        CollectionRegistry.markKnown("t1", "c1");
        assertThat(CollectionRegistry.isKnown("t1", "c1")).isTrue();
        // Distinct tenant, same collection name — must NOT be known.
        assertThat(CollectionRegistry.isKnown("t2", "c1")).isFalse();
        // Same tenant, distinct collection — must NOT be known.
        assertThat(CollectionRegistry.isKnown("t1", "c2")).isFalse();
    }
}
