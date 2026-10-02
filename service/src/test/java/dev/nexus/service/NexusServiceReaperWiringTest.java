// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.ChunkReaper.Refusal;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 9 (bead nexus-2x9xa): the reaper is WIRED into {@code NexusService}, not merely correct in isolation.
 * Every arm of the sweep loop had its own unit test while nothing proved the scheduler invoked it (nexus-lgiqw), so
 * this drives the instance's own scheduled entry point.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceReaperWiringTest {

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    private NexusService withVectors;
    private NexusService withoutVectors;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);

        var zero = new dev.nexus.service.vectors.Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        var vectors = new PgVectorRepository(new TenantScope(ds), zero, zero);
        withVectors = new NexusService(0, "reaper-wiring-token", ds, null, vectors, null, null, null);
        withoutVectors = new NexusService(0, "reaper-wiring-token", ds, null, null, null, null, null);
    }

    @AfterAll
    void stopAll() {
        for (NexusService s : new NexusService[] {withVectors, withoutVectors}) {
            if (s != null) {
                try {
                    s.stop();
                } catch (Exception ignored) {
                    // never started
                }
            }
        }
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @Test
    void anInstanceWithAVectorBackendSchedulesTheReaper_withSamsDefaults() {
        ChunkReaper reaper = withVectors.chunkReaper();
        assertThat(reaper).isNotNull();
        assertThat(reaper.settings()).isEqualTo(ChunkReaper.Settings.defaults());
    }

    @Test
    void anInstanceWithoutAVectorBackendHasNothingToReap() {
        assertThat(withoutVectors.chunkReaper()).isNull();
    }

    @Test
    void theScheduledEntryPointVisitsTheDefaultTenant_andRefusesItWithoutABackfillRecord() {
        RunResult run = withVectors.chunkReaper().run();

        assertThat(run.tenant("default")).as("the default tenant is always visited").isNotNull();
        assertThat(run.tenant("default").tenantRefusal()).isEqualTo(Refusal.BACKFILL_INCOMPLETE);
        assertThat(withVectors.chunkReaper().refusedTotal()).isGreaterThanOrEqualTo(1);
    }

    // ── the SCHEDULED task body, not a copy of it ────────────────────────────

    private final AtomicInteger seq = new AtomicInteger();

    private String col(String prefix) {
        return prefix + "__wire" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    /** Inserts one chunk by substrate SQL and returns its chash (hex). */
    private String insertChunk(String tenant, String collection, String seed, Map<String, Object> metadata,
                               OffsetDateTime lastWritten) throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex),
                List.of(seed + " text"), List.of(new float[384]), List.of(metadata));
            if (lastWritten != null) {
                ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, lastWritten).set(CHUNKS.LAST_WRITTEN_AT, lastWritten)
                   .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))).execute();
            }
        }
        return hex;
    }

    private boolean stored(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(CHUNKS,
                CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())));
        }
    }

    @Test
    void theScheduledTaskItselfReapsATokenBearingTenant_andExpiresItsQuarantine() throws Exception {
        String tenant = "wire-tenant-" + seq.incrementAndGet();
        new TokenStore(ds, Clock.systemUTC()).issueToken(tenant, "wiring", null);
        new LadderRepository(new TenantScope(ds)).record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");

        // A chunk nothing owns, last written 40 days ago: reapable under the 30 day default the schedule runs.
        String origin = col("knowledge");
        String debris = insertChunk(tenant, origin, "debris", Map.of(), OffsetDateTime.now().minusDays(40));
        // A chunk the reaper's earlier passes quarantined 15 days ago: tagged, and past the 14 day retention.
        String stamp = Instant.now().minus(15, ChronoUnit.DAYS).truncatedTo(ChronoUnit.SECONDS).toString();
        String other = col("knowledge");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, other);
        }
        String old = insertChunk(tenant, "quarantine-" + other, "old",
            Map.of("quarantined_at", stamp, "origin_collection", other, "quarantined_by", "engine-reaper",
                "reaper_quarantined_at", stamp), null);

        assertThat(withVectors.reaperTenantsForTests()).as("the default tenant plus every token-bearing one")
            .contains("default", tenant);
        RunResult before = withVectors.chunkReaper().lastRun();   // other tests on this instance may have run a pass

        // THE object the scheduler runs. A schedule turned into a no-op leaves every assertion below red.
        withVectors.reaperScheduledTask().run();

        RunResult run = withVectors.chunkReaper().lastRun();
        assertThat(run).as("the scheduled task ran a pass").isNotNull().isNotSameAs(before);
        assertThat(run.tenant("default")).isNotNull();
        assertThat(run.tenant(tenant)).as("a tenant that only holds a token is visited").isNotNull();
        assertThat(run.tenant(tenant).collection(origin).moved()).isEqualTo(1);
        assertThat(stored(tenant, origin, debris)).as("moved out of the collection").isFalse();
        assertThat(stored(tenant, "quarantine-" + origin, debris)).as("into quarantine").isTrue();
        assertThat(run.tenant(tenant).expiry("quarantine-" + other).expired())
            .as("the same task expires the quarantine it fills").isEqualTo(1);
        assertThat(stored(tenant, "quarantine-" + other, old)).isFalse();
    }

    // ── the SCHEDULE CALL itself (code review I3) ────────────────────────────

    /** A scheduler that records what is registered on it and runs none of it. */
    private static final class RecordingScheduler extends java.util.concurrent.ScheduledThreadPoolExecutor {
        record Registration(String kind, Runnable task, long initialDelay, long period, TimeUnit unit) {}

        final List<Registration> registrations = new java.util.concurrent.CopyOnWriteArrayList<>();

        RecordingScheduler() {
            super(1, r -> {
                Thread t = new Thread(r, "recording-scheduler");
                t.setDaemon(true);
                return t;
            });
        }

        @Override
        public java.util.concurrent.ScheduledFuture<?> scheduleWithFixedDelay(Runnable command, long initialDelay,
                                                                              long delay, TimeUnit unit) {
            registrations.add(new Registration("fixed-delay", command, initialDelay, delay, unit));
            return null;
        }

        @Override
        public java.util.concurrent.ScheduledFuture<?> scheduleAtFixedRate(Runnable command, long initialDelay,
                                                                           long period, TimeUnit unit) {
            registrations.add(new Registration("fixed-rate", command, initialDelay, period, unit));
            return null;
        }

        List<Registration> fixedDelay() {
            return registrations.stream().filter(r -> r.kind().equals("fixed-delay")).toList();
        }
    }

    @Test
    void theSchedulerIsHandedTheVerySameTask_atTheBootDelay_andTheConfiguredInterval() throws Exception {
        var recording = new RecordingScheduler();
        var zero = new dev.nexus.service.vectors.Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        var vectors = new PgVectorRepository(new TenantScope(ds), zero, zero);
        NexusService service = new NexusService(0, "reaper-wiring-token", ds, null, vectors, null, null, null, recording);
        try {
            Runnable task = service.reaperScheduledTask();
            assertThat(task).isNotNull();
            // Deleting the scheduleWithFixedDelay call for the reaper leaves this list empty and fails here; the
            // task body being correct proves nothing about it being scheduled (nexus-lgiqw).
            assertThat(recording.fixedDelay()).as("the reaper is registered on the sweep scheduler, exactly once")
                .singleElement().satisfies(r -> {
                    assertThat(r.task()).as("the VERY SAME Runnable the wiring test runs, not a copy of its body")
                        .isSameAs(task);
                    assertThat(r.unit()).isEqualTo(TimeUnit.SECONDS);
                    assertThat(r.initialDelay()).as("the first pass shortly after boot")
                        .isEqualTo(ChunkReaper.INITIAL_DELAY.toSeconds());
                    assertThat(r.period()).as("the configured interval, hourly by default")
                        .isEqualTo(service.chunkReaper().settings().interval().toSeconds());
                });
            assertThat(service.chunkReaper().settings().interval()).isEqualTo(java.time.Duration.ofHours(1));
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void anInstanceWithoutAVectorBackendRegistersNothingForTheReaper() throws Exception {
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "reaper-wiring-token", ds, null, null, null, null, null, recording);
        try {
            assertThat(service.reaperScheduledTask()).as("nothing to reap, nothing scheduled").isNull();
            assertThat(recording.fixedDelay()).isEmpty();
        } finally {
            stopQuietly(service);
        }
    }

    // ── a thrown Error must not end a schedule (nexus-wbfpw.56, RDR-192 Phase 3 gate S5) ─────────────────────

    @Test
    void aThrownErrorDoesNotEndAnyScheduledTask_theTtlSweepAndTheReaperKeepTheirSchedule() throws Exception {
        var failing = new java.util.concurrent.atomic.AtomicBoolean();
        var thrown = new AtomicInteger();
        javax.sql.DataSource flaky = (javax.sql.DataSource) java.lang.reflect.Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {javax.sql.DataSource.class}, (proxy, method, args) -> {
                if (failing.get() && method.getName().equals("getConnection")) {
                    thrown.incrementAndGet();
                    throw new NoClassDefFoundError("simulated: a class the driver needs is gone");
                }
                try {
                    return method.invoke(ds, args);
                } catch (java.lang.reflect.InvocationTargetException e) {
                    throw e.getCause();
                }
            });
        var recording = new RecordingScheduler();
        var zero = new dev.nexus.service.vectors.Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        var vectors = new PgVectorRepository(new TenantScope(ds), zero, zero);
        NexusService service = new NexusService(0, "reaper-wiring-token", flaky, null, vectors, null, null, null,
            recording);
        try {
            assertThat(recording.registrations).as("the TTL sweep and the reaper share the one scheduler thread")
                .hasSizeGreaterThanOrEqualTo(2);
            failing.set(true);
            for (var registration : recording.registrations) {
                // scheduleAtFixedRate and scheduleWithFixedDelay both suppress every later run of a task whose
                // Runnable threw: an Error that gets out is a silently dead schedule on a shared thread.
                org.assertj.core.api.Assertions.assertThatCode(() -> registration.task().run())
                    .as("the " + registration.kind() + " task registered at delay " + registration.initialDelay())
                    .doesNotThrowAnyException();
            }
            assertThat(thrown.get()).as("every task really did hit the Error").isGreaterThanOrEqualTo(2);
        } finally {
            failing.set(false);
            stopQuietly(service);
        }
    }

    @Test
    void theSurvivingWrapperKeepsAFixedDelayScheduleAlive_whereAnExceptionOnlyCatchDoesNot() throws Exception {
        var pool = new java.util.concurrent.ScheduledThreadPoolExecutor(1, r -> {
            Thread t = new Thread(r, "survive-test");
            t.setDaemon(true);
            return t;
        });
        try {
            var wrapped = new AtomicInteger();
            var exceptionOnly = new AtomicInteger();
            pool.scheduleWithFixedDelay(NexusService.surviving("test_task_failed", () -> {
                wrapped.incrementAndGet();
                throw new NoClassDefFoundError("simulated");
            }), 0, 20, TimeUnit.MILLISECONDS);
            // The control: what the three scheduled tasks did before the fix.
            pool.scheduleWithFixedDelay(() -> {
                try {
                    exceptionOnly.incrementAndGet();
                    throw new NoClassDefFoundError("simulated");
                } catch (Exception ignored) {
                    // an Error is not an Exception
                }
            }, 0, 20, TimeUnit.MILLISECONDS);

            long deadline = System.nanoTime() + java.time.Duration.ofSeconds(10).toNanos();
            while (wrapped.get() < 3 && System.nanoTime() < deadline) Thread.sleep(10);
            assertThat(wrapped.get()).as("the wrapped task ran again after each Error").isGreaterThanOrEqualTo(3);
            assertThat(exceptionOnly.get()).as("the Exception-only task never ran a second time").isEqualTo(1);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── the reaper's liveness reaches GET /v1/status ─────────────────────────

    @Test
    void theStatusRouteReportsTheReapersLastCompletedPass_nullBeforeItsFirst() throws Exception {
        var zero = new dev.nexus.service.vectors.Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        NexusService service = new NexusService(0, "reaper-wiring-token", ds, null,
            new PgVectorRepository(new TenantScope(ds), zero, zero), null, null, null);
        try {
            service.start();
            var http = java.net.http.HttpClient.newHttpClient();
            var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
            java.util.function.Supplier<com.fasterxml.jackson.databind.JsonNode> status = () -> {
                try {
                    var resp = http.send(java.net.http.HttpRequest.newBuilder(
                        java.net.URI.create("http://127.0.0.1:" + service.getPort() + "/v1/status")).GET().build(),
                        java.net.http.HttpResponse.BodyHandlers.ofString());
                    return mapper.readTree(resp.body()).get("reaper");
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            };

            var before = status.get();
            assertThat(before.get("enabled").asBoolean()).isTrue();
            assertThat(before.get("interval_seconds").asLong()).isEqualTo(3600L);
            assertThat(before.get("wall_clock_budget_seconds").asLong()).as("the default 10 minute budget")
                .isEqualTo(600L);
            assertThat(before.get("last_completed_pass_at").isNull()).as("no pass yet").isTrue();

            service.reaperScheduledTask().run();

            var after = status.get();
            assertThat(after.get("last_completed_pass_at").isTextual()).isTrue();
            assertThat(java.time.Instant.parse(after.get("last_completed_pass_at").asText()))
                .isBetween(java.time.Instant.now().minusSeconds(120), java.time.Instant.now().plusSeconds(5));
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void anInstanceWithNoReaperReportsItAsNotRunning_notAsAbsent() throws Exception {
        NexusService service = new NexusService(0, "reaper-wiring-token", ds, null, null, null, null, null);
        try {
            service.start();
            var resp = java.net.http.HttpClient.newHttpClient().send(java.net.http.HttpRequest.newBuilder(
                java.net.URI.create("http://127.0.0.1:" + service.getPort() + "/v1/status")).GET().build(),
                java.net.http.HttpResponse.BodyHandlers.ofString());
            var reaper = new com.fasterxml.jackson.databind.ObjectMapper().readTree(resp.body()).get("reaper");
            assertThat(reaper).as("the key is present so a client can tell 'disabled' from 'engine predates this'")
                .isNotNull();
            assertThat(reaper.get("enabled").asBoolean()).isFalse();
        } finally {
            stopQuietly(service);
        }
    }

    private static void stopQuietly(NexusService service) {
        try {
            service.stop();
        } catch (Exception ignored) {
            // never started
        }
    }

    @Test
    void theInstancesOwnTaskIsNonNullWhenWired_andNullWhenNot() {
        assertThat(withVectors.reaperScheduledTask()).isNotNull();
        assertThat(withoutVectors.reaperScheduledTask()).isNull();
    }
}
