// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.TenantResetException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.http.MintRateLimiter;
import dev.nexus.service.tuples.TemplateRegistry;
import dev.nexus.service.vectors.OwnerlessLogLimiter;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-0bl77 (rtg20 phase A): one test per tenant-keyed JVM structure the test-only tenant reset route evicts
 * ({@code POST /v1/_test/tenant-reset}). Each seeds tenants X and Y, evicts X, and asserts that X's state is gone and
 * Y's is intact. The route that lists these evictors is {@code TenantResetRouteTest}; the completeness gate that
 * makes this list exhaustive is {@code TenantKeyedStateGateTest}.
 *
 * <p>The structures that need Postgres (the token cache's store, the ghost-sweep marker, the tuple wait park) share
 * one container. The pure in-memory ones need none.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantKeyedEvictorsTest {

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
    TupleRepository tuples;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        tuples = new TupleRepository(tenantScope, registry, TupleRepository.DEFAULT_READ_MAX,
                TupleRepository.DEFAULT_CLAIM_PASSES, /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4,
                /* parkCapGlobal */ 16);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String tenant(String prefix) {
        return prefix + "-" + UUID.randomUUID().toString().substring(0, 8);
    }

    // ── TokenCache.map ───────────────────────────────────────────────────────────────────────────

    @Test
    void tokenCache_invalidateTenant_dropsXsPositiveEntryAndKeepsYs() throws Exception {
        String x = tenant("tc-x");
        String y = tenant("tc-y");
        var store = new TokenStore(svcDs, Clock.systemUTC());
        var cache = new TokenCache(store, Clock.systemUTC());
        String xHash = store.issueToken(x, "evict-test", null).tokenHash();
        String yHash = store.issueToken(y, "evict-test", null).tokenHash();
        assertThat(cache.resolveTenant(xHash)).contains(x);
        assertThat(cache.resolveTenant(yHash)).contains(y);

        // The reset deletes the tenant's token rows. The cache still answers from memory (a stale positive, up to
        // its 30 s TTL): that is the defect the evictor exists to close.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TENANT_ID.in(x, y)).execute();
        }
        assertThat(cache.resolveTenant(xHash)).as("stale positive before eviction").contains(x);

        assertThat(cache.invalidateTenant(x)).isEqualTo(1);

        assertThat(cache.resolveTenant(xHash)).as("X's entry is gone, so the deleted token reads as absent").isEmpty();
        assertThat(cache.resolveTenant(yHash)).as("Y's entry is intact").contains(y);
        assertThat(cache.invalidateTenant(x)).as("nothing left to evict").isZero();
    }

    // ── CollectionRegistry.KNOWN ─────────────────────────────────────────────────────────────────

    @Test
    void collectionRegistry_evictTenant_dropsXsPairsOnly_andMatchesTheTenantExactly() {
        String x = tenant("cr-x");
        String lookalike = x + "2";   // starts with X's name, is a different tenant
        var row = new CollectionRow("code", "1.1", "voyage-code-3", 1024, "live");
        CollectionRegistry.markKnown(x, "c1", row);
        CollectionRegistry.markKnown(x, "c2", row);
        CollectionRegistry.markKnown(lookalike, "c1", row);
        try {
            assertThat(CollectionRegistry.evictTenant(x)).isEqualTo(2);

            assertThat(CollectionRegistry.isKnown(x, "c1")).isFalse();
            assertThat(CollectionRegistry.isKnown(x, "c2")).isFalse();
            assertThat(CollectionRegistry.isKnown(lookalike, "c1")).as("a different tenant is intact").isTrue();
            assertThat(CollectionRegistry.evictTenant(x)).as("second call has nothing to do").isZero();
        } finally {
            CollectionRegistry.evictTenant(lookalike);
        }
    }

    // ── CatalogRepository.ghostSweepCheckedTenants ───────────────────────────────────────────────

    @Test
    void ghostSweep_evictGhostSweepTenant_forgetsXSoItsSweepRunsAgain_andKeepsY() {
        String x = tenant("gs-x");
        String y = tenant("gs-y");
        var catalog = new CatalogRepository(tenantScope);
        catalog.ensureGhostSweepRanOnce(x);
        catalog.ensureGhostSweepRanOnce(y);
        assertThat(catalog.ghostSweepChecked(x)).isTrue();
        assertThat(catalog.ghostSweepChecked(y)).isTrue();

        assertThat(catalog.evictGhostSweepTenant(x)).isEqualTo(1);

        assertThat(catalog.ghostSweepChecked(x)).as("X will be swept again on its next request").isFalse();
        assertThat(catalog.ghostSweepChecked(y)).as("Y keeps its in-process marker").isTrue();
        assertThat(catalog.evictGhostSweepTenant(x)).isZero();
    }

    // ── MintRateLimiter.credentials / CredentialState.tenants ────────────────────────────────────

    @Test
    void mintRateLimiter_evictTenant_restoresXsBurstAndLeavesYsExhausted() {
        var limiter = new MintRateLimiter(Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC),
            /* tenantBurst */ 2, /* tenantSustainedPerMinute */ 1, /* globalSustainedPerMinute */ 1000);
        String credential = "credential-hash";
        for (String t : List.of("mint-x", "mint-y")) {
            assertThat(limiter.tryAcquire(credential, t)).isTrue();
            assertThat(limiter.tryAcquire(credential, t)).isTrue();
            assertThat(limiter.tryAcquire(credential, t)).as("%s burst spent", t).isFalse();
        }

        assertThat(limiter.evictTenant("mint-x")).isEqualTo(1);

        assertThat(limiter.tryAcquire(credential, "mint-x")).as("X has a fresh bucket").isTrue();
        assertThat(limiter.tryAcquire(credential, "mint-y")).as("Y is still exhausted").isFalse();
        assertThat(limiter.evictTenant("mint-unknown")).isZero();
    }

    @Test
    void mintRateLimiter_evictTenant_countsEveryCredentialThatHoldsABucketForTheTenant() {
        var limiter = new MintRateLimiter(Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), 2, 1, 1000);
        limiter.tryAcquire("cred-1", "mint-x");
        limiter.tryAcquire("cred-2", "mint-x");
        limiter.tryAcquire("cred-2", "mint-y");
        assertThat(limiter.evictTenant("mint-x")).isEqualTo(2);
    }

    // ── OwnerlessLogLimiter.states ───────────────────────────────────────────────────────────────

    @Test
    void ownerlessLogLimiter_evictTenant_dropsXsWindowsOnly() {
        var limiter = new OwnerlessLogLimiter(() -> 1_000_000L);   // a frozen clock: nothing ages out
        assertThat(limiter.tryAcquire("/v1/vectors/add|log-x|coll-1")).isGreaterThanOrEqualTo(0);
        assertThat(limiter.tryAcquire("/v1/vectors/upsert|log-x|coll-2")).isGreaterThanOrEqualTo(0);
        assertThat(limiter.tryAcquire("/v1/vectors/add|log-x2|coll-1")).isGreaterThanOrEqualTo(0);
        assertThat(limiter.tryAcquire("/v1/vectors/add|log-x|coll-1")).as("inside its window: suppressed").isEqualTo(-1);

        assertThat(limiter.evictTenant("log-x")).isEqualTo(2);

        assertThat(limiter.tryAcquire("/v1/vectors/add|log-x|coll-1")).as("X logs again").isGreaterThanOrEqualTo(0);
        assertThat(limiter.tryAcquire("/v1/vectors/add|log-x2|coll-1")).as("log-x2 is another tenant").isEqualTo(-1);
    }

    // ── ChunkReaper.timeoutStreaks / resume* ─────────────────────────────────────────────────────

    @Test
    void chunkReaper_evictTenant_dropsXsStreaksAndResumeCursorsAndKeepsY() throws Exception {
        var reaper = new ChunkReaper(null, null, null, null, List::of, ChunkReaper.Settings.fromEnv(k -> null),
            Clock.systemUTC(), null, System::nanoTime);
        Method noteTimeout = ChunkReaper.class.getDeclaredMethod("noteTimeout", String.class, long.class);
        noteTimeout.setAccessible(true);
        noteTimeout.invoke(reaper, "rx/coll-1#census", 1L);
        noteTimeout.invoke(reaper, "rx/quarantine-coll#expire", 1L);
        noteTimeout.invoke(reaper, "ry/coll-1#census", 1L);
        map(reaper, "resumeCollection").put("rx", "coll-5");
        map(reaper, "resumeCollection").put("ry", "coll-5");
        map(reaper, "resumeExpiry").put("rx", "quarantine-coll");
        setResumeTenant(reaper, "rx");

        // 2 streaks + 1 resumeCollection + 1 resumeExpiry + the resume cursor naming X
        assertThat(reaper.evictTenant("rx")).isEqualTo(5);

        assertThat(map(reaper, "timeoutStreaks").keySet()).containsExactly("ry/coll-1#census");
        assertThat(map(reaper, "resumeCollection")).containsOnlyKeys("ry");
        assertThat(map(reaper, "resumeExpiry")).isEmpty();
        assertThat(resumeTenant(reaper)).as("the cut that stopped at X is forgotten").isNull();
        assertThat(reaper.evictTenant("rx")).isZero();

        setResumeTenant(reaper, "ry");
        assertThat(reaper.evictTenant("rx")).as("a cursor naming Y is not X's to clear").isZero();
        assertThat(resumeTenant(reaper)).isEqualTo("ry");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(ChunkReaper reaper, String field) throws Exception {
        Field f = ChunkReaper.class.getDeclaredField(field);
        f.setAccessible(true);
        return (Map<String, Object>) f.get(reaper);
    }

    private static void setResumeTenant(ChunkReaper reaper, String value) throws Exception {
        Field f = ChunkReaper.class.getDeclaredField("resumeTenant");
        f.setAccessible(true);
        f.set(reaper, value);
    }

    private static String resumeTenant(ChunkReaper reaper) throws Exception {
        Field f = ChunkReaper.class.getDeclaredField("resumeTenant");
        f.setAccessible(true);
        return (String) f.get(reaper);
    }

    // ── TupleWaitRegistry (through TupleRepository) ──────────────────────────────────────────────

    @Test
    void tuples_abortTenantWaiters_wakesXsParkedReadAsReset_andLeavesYsParkedReadServing() throws Exception {
        String x = tenant("tw-x");
        String y = tenant("tw-y");
        String session = "evict-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.TupleRow>> parkedX = pool.submit(() ->
                tuples.rd(x, "ledger/" + session, null, 10, null, 8));
            Future<List<TupleRepository.TupleRow>> parkedY = pool.submit(() ->
                tuples.rd(y, "ledger/" + session, null, 10, null, 8));
            awaitParked(2);

            long start = System.nanoTime();
            int removed = tuples.abortTenantWaiters(x, Duration.ofSeconds(3));

            assertThatThrownBy(() -> parkedX.get(3, TimeUnit.SECONDS))
                .isInstanceOf(ExecutionException.class)
                .cause().isInstanceOf(TenantResetException.class)
                .satisfies(e -> {
                    var t = (TenantResetException) e;
                    assertThat(t.code()).isEqualTo("TenantReset");
                    assertThat(t.httpStatus()).isEqualTo(409);
                    assertThat(e).as("not part of the client's typed tuple-error vocabulary")
                        .isNotInstanceOf(dev.nexus.service.db.TupleException.class);
                });
            assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start))
                .as("the abort did not ride out the 8 s read").isLessThan(3000);
            assertThat(removed).isEqualTo(1);
            assertThat(tuples.parkStats().globalInUse()).as("only Y is still parked").isEqualTo(1);

            // Y is unaffected: a write to Y wakes it and it returns the row.
            tuples.out(y, "ledger/" + session, Map.of("agent_id", "waker", "kind", "start"), Map.of(), null, null, null);
            assertThat(parkedY.get(5, TimeUnit.SECONDS)).hasSize(1);
        } finally {
            pool.shutdownNow();
        }
    }

    private void awaitParked(int count) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (tuples.parkStats().globalInUse() < count && System.nanoTime() < deadline) {
            Thread.sleep(25);
        }
        assertThat(tuples.parkStats().globalInUse()).as("both reads are parked").isGreaterThanOrEqualTo(count);
    }
}
