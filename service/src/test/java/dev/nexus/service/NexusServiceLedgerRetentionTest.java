// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.RequestLedger;
import dev.nexus.service.db.TokenStore;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.3): the request-ledger retention arm is WIRED into
 * {@code NexusService}: scheduled on the sweep scheduler at the sweep interval, visiting the default
 * tenant and every token-bearing tenant, with the cutoff taken from the {@code now} it is handed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceLedgerRetentionTest {

    private static final OffsetDateTime NOW = OffsetDateTime.of(2026, 10, 18, 12, 0, 0, 0, ZoneOffset.UTC);

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;

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
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** A scheduler that records what is registered on it and runs none of it. */
    private static final class RecordingScheduler extends java.util.concurrent.ScheduledThreadPoolExecutor {
        record Registration(Runnable task, long initialDelay, long period, TimeUnit unit) {}

        final List<Registration> fixedRate = new CopyOnWriteArrayList<>();

        RecordingScheduler() {
            super(1, r -> {
                Thread t = new Thread(r, "recording-scheduler");
                t.setDaemon(true);
                return t;
            });
        }

        @Override
        public java.util.concurrent.ScheduledFuture<?> scheduleAtFixedRate(Runnable command, long initialDelay,
                                                                           long period, TimeUnit unit) {
            fixedRate.add(new Registration(command, initialDelay, period, unit));
            return null;
        }
    }

    private void seed(String tenant, String state, OffsetDateTime finishedAt, OffsetDateTime leaseUntil,
                      UUID id) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).insertInto(REQUEST_LEDGER)
                .set(REQUEST_LEDGER.TENANT_ID, tenant)
                .set(REQUEST_LEDGER.REQUEST_ID, id)
                .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
                .set(REQUEST_LEDGER.BODY_SHA256, new byte[32])
                .set(REQUEST_LEDGER.STATE, state)
                .set(REQUEST_LEDGER.STARTED_AT, NOW.minusDays(30))
                .set(REQUEST_LEDGER.LEASE_UNTIL, leaseUntil)
                .set(REQUEST_LEDGER.FINISHED_AT, finishedAt)
                .execute();
        }
    }

    private boolean exists(String tenant, UUID id) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(
                REQUEST_LEDGER, REQUEST_LEDGER.TENANT_ID.eq(tenant).and(REQUEST_LEDGER.REQUEST_ID.eq(id)));
        }
    }

    @Test
    void theScheduledRetentionPassVisitsTheDefaultTenantAndEveryTokenTenant_withASevenDayCutoff() throws Exception {
        String tokenTenant = "ledger-retention-token-tenant";
        new TokenStore(ds, Clock.systemUTC()).issueToken(tokenTenant, "retention", null);
        UUID defaultOld = UUID.randomUUID();
        UUID defaultFresh = UUID.randomUUID();
        UUID tokenOld = UUID.randomUUID();
        UUID tokenLive = UUID.randomUUID();
        seed("default", "committed", NOW.minusDays(7).minusHours(1), null, defaultOld);
        seed("default", "committed", NOW.minusDays(7).plusHours(1), null, defaultFresh);
        // A tenant with ledger rows and no service_tokens row is not visited: the documented liveness gap.
        UUID noTokenOld = UUID.randomUUID();
        seed("ledger-retention-no-token", "committed", NOW.minusDays(30), null, noTokenOld);
        seed(tokenTenant, "failed", NOW.minusDays(8), null, tokenOld);
        seed(tokenTenant, "in_flight", null, NOW.plusHours(1), tokenLive);

        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "ledger-retention-token", ds, null, null, null, null, null,
            recording);
        try {
            List<RequestLedger.RetentionPass> passes = service.runScheduledLedgerRetention(NOW);

            assertThat(passes).extracting(RequestLedger.RetentionPass::tenant)
                .contains("default", tokenTenant);
            assertThat(passes).allMatch(p -> !p.failed());
            assertThat(exists("default", defaultOld)).isFalse();
            assertThat(exists("default", defaultFresh)).isTrue();
            assertThat(exists(tokenTenant, tokenOld)).isFalse();
            assertThat(exists(tokenTenant, tokenLive)).as("a live lease is never deleted").isTrue();
            assertThat(passes).extracting(RequestLedger.RetentionPass::tenant)
                .as("a tenant with no service_tokens row is not visited").doesNotContain("ledger-retention-no-token");
            assertThat(exists("ledger-retention-no-token", noTokenOld)).isTrue();
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void theSchedulerIsHandedTheLedgerRetentionTask_atTheSweepInterval() throws Exception {
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "ledger-retention-token", ds, null, null, null, null, null,
            recording);
        try {
            Runnable task = service.requestLedgerRetentionTask();
            assertThat(task).isNotNull();
            assertThat(recording.fixedRate).as("the retention task is registered on the sweep scheduler")
                .anySatisfy(r -> {
                    assertThat(r.task()).as("the VERY SAME Runnable, not a copy of its body").isSameAs(task);
                    assertThat(r.unit()).isEqualTo(TimeUnit.SECONDS);
                    assertThat(r.initialDelay()).as("the first pass shortly after boot, like the reaper")
                        .isEqualTo(ChunkReaper.INITIAL_DELAY.toSeconds());
                    assertThat(r.period()).isEqualTo(TimeUnit.HOURS.toSeconds(NexusService.SWEEP_INTERVAL_HOURS));
                });
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void theRegisteredTaskItselfPrunesByTheRealClock_sevenDaysBack() throws Exception {
        String tenant = "ledger-retention-task-tenant";
        new TokenStore(ds, Clock.systemUTC()).issueToken(tenant, "retention-task", null);
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        UUID committedOld = UUID.randomUUID();
        UUID failedOld = UUID.randomUUID();
        UUID expiredOld = UUID.randomUUID();
        UUID fresh = UUID.randomUUID();
        UUID liveLease = UUID.randomUUID();
        seed(tenant, "committed", now.minusDays(8), null, committedOld);
        seed(tenant, "failed", now.minusDays(9), null, failedOld);
        seed(tenant, "in_flight", null, now.minusDays(8), expiredOld);
        seed(tenant, "committed", now.minusDays(6), null, fresh);
        seed(tenant, "in_flight", null, now.plusHours(1), liveLease);

        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "ledger-retention-token", ds, null, null, null, null, null,
            recording);
        try {
            // The VERY SAME Runnable the scheduler is handed, run as the scheduler would run it.
            recording.fixedRate.stream().filter(r -> r.task() == service.requestLedgerRetentionTask())
                .findFirst().orElseThrow().task().run();

            assertThat(exists(tenant, committedOld)).isFalse();
            assertThat(exists(tenant, failedOld)).isFalse();
            assertThat(exists(tenant, expiredOld)).isFalse();
            assertThat(exists(tenant, fresh)).as("six days old is inside the seven-day window").isTrue();
            assertThat(exists(tenant, liveLease)).isTrue();
        } finally {
            stopQuietly(service);
        }
    }

    private static void stopQuietly(NexusService s) {
        try {
            s.stop();
        } catch (Exception ignored) {
            // never started
        }
    }
}
