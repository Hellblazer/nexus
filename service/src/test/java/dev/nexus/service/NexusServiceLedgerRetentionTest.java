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

    private static final String ROUTE = "/v1/vectors/gc/quarantine-orphans";
    private static final OffsetDateTime NOW = OffsetDateTime.of(2026, 10, 18, 12, 0, 0, 0, ZoneOffset.UTC);

    private static final String SVC_ROLE = "svc_ledger_service_retention_test";
    private static final String SVC_PASS = "svc_ledger_service_retention_test_pass";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    /** The non-superuser service role (FORCE RLS applies to it); {@code ds} is the superuser. */
    private com.zaxxer.hikari.HikariDataSource svcDs;

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
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var svcCfg = new com.zaxxer.hikari.HikariConfig();
        svcCfg.setJdbcUrl(pg.getJdbcUrl());
        svcCfg.setUsername(SVC_ROLE);
        svcCfg.setPassword(SVC_PASS);
        svcCfg.setMaximumPoolSize(6);
        svcCfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(svcCfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
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
        } finally {
            stopQuietly(service);
        }
    }

    /**
     * nexus-yye8l.21: a tenant that holds ledger rows and no {@code service_tokens} row (it lost its last token)
     * is still retained. The rows get there through the real claim path, which records the tenant; a row inserted
     * around the claim path would not be recorded, and none of these are.
     */
    @Test
    void aTenantWithLedgerRowsAndNoToken_hasItsExpiredRowsPurged_andNothingElse() throws Exception {
        String noToken = "ledger-retention-no-token-a";
        String otherNoToken = "ledger-retention-no-token-b";
        UUID expiredCommitted = UUID.randomUUID();
        UUID expiredFailed = UUID.randomUUID();
        UUID unexpired = UUID.randomUUID();
        UUID liveLease = UUID.randomUUID();
        UUID otherFresh = UUID.randomUUID();
        UUID otherExpired = UUID.randomUUID();
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "ledger-retention-token", ds, null, null, null, null, null,
            recording);
        try {
            RequestLedger ledger = service.requestLedgerForTests();
            for (UUID id : List.of(expiredCommitted, expiredFailed, unexpired, liveLease)) {
                assertThat(ledger.claim(noToken, id, ROUTE, new byte[32], 25_000L))
                    .isInstanceOf(RequestLedger.Claimed.class);
            }
            for (UUID id : List.of(otherFresh, otherExpired)) {
                assertThat(ledger.claim(otherNoToken, id, ROUTE, new byte[32], 25_000L))
                    .isInstanceOf(RequestLedger.Claimed.class);
            }
            // No token for either tenant: the reaper's own tenant list does not name them.
            assertThat(service.reaperTenantsForTests()).doesNotContain(noToken, otherNoToken);
            finish(noToken, expiredCommitted, "committed", NOW.minusDays(8));
            finish(noToken, expiredFailed, "failed", NOW.minusDays(9));
            finish(noToken, unexpired, "committed", NOW.minusDays(6));
            // liveLease stays in_flight with the lease the claim gave it (about 95 s after the real clock).
            finish(otherNoToken, otherFresh, "committed", NOW.minusDays(1));
            finish(otherNoToken, otherExpired, "committed", NOW.minusDays(8));

            List<RequestLedger.RetentionPass> passes = service.runScheduledLedgerRetention(NOW);

            assertThat(passes).extracting(RequestLedger.RetentionPass::tenant)
                .contains("default", noToken, otherNoToken);
            assertThat(passes).allMatch(p -> !p.failed());
            assertThat(exists(noToken, expiredCommitted)).isFalse();
            assertThat(exists(noToken, expiredFailed)).isFalse();
            assertThat(exists(noToken, unexpired)).as("six days old is inside the seven-day window").isTrue();
            assertThat(exists(noToken, liveLease)).as("a live lease is never deleted").isTrue();
            assertThat(exists(otherNoToken, otherFresh)).isTrue();
            assertThat(exists(otherNoToken, otherExpired)).isFalse();
        } finally {
            stopQuietly(service);
        }
    }

    /**
     * nexus-yye8l.21, the whole path as the service role: claim, then the retention cycle (which lists the registry
     * and retains), with the superuser nowhere in it. FORCE RLS applies to this role, and neither tenant has a
     * service token, so only the registry can reach them. The superuser pool is used afterwards to read the result.
     */
    @Test
    void asTheServiceRole_claimThenRetention_purgesATokenlessTenantsExpiredRowsAndNothingElse() throws Exception {
        String tenant = "ledger-retention-svc-" + UUID.randomUUID();
        String other = "ledger-retention-svc-other-" + UUID.randomUUID();
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "ledger-retention-token", svcDs, null, null, null, null, null,
            recording);
        try {
            RequestLedger ledger = service.requestLedgerForTests();
            var scope = new dev.nexus.service.db.TenantScope(svcDs);
            UUID expired = UUID.randomUUID();
            UUID unexpired = UUID.randomUUID();
            UUID live = UUID.randomUUID();
            UUID otherFresh = UUID.randomUUID();
            for (var tenantAndId : List.of(
                    java.util.Map.entry(tenant, expired), java.util.Map.entry(tenant, unexpired),
                    java.util.Map.entry(tenant, live), java.util.Map.entry(other, otherFresh))) {
                assertThat(ledger.claim(tenantAndId.getKey(), tenantAndId.getValue(), ROUTE, new byte[32], 25_000L))
                    .isInstanceOf(RequestLedger.Claimed.class);
            }
            assertThat(service.reaperTenantsForTests()).doesNotContain(tenant, other);
            for (var aged : List.of(
                    java.util.Map.entry(java.util.Map.entry(tenant, expired), NOW.minusDays(8)),
                    java.util.Map.entry(java.util.Map.entry(tenant, unexpired), NOW.minusDays(6)),
                    java.util.Map.entry(java.util.Map.entry(other, otherFresh), NOW.minusDays(1)))) {
                var key = aged.getKey();
                scope.withTenant(key.getKey(), ctx -> ctx.update(REQUEST_LEDGER)
                    .set(REQUEST_LEDGER.STATE, "committed")
                    .set(REQUEST_LEDGER.HOLDER, (UUID) null)
                    .set(REQUEST_LEDGER.STARTED_AT, aged.getValue().minusMinutes(1))
                    .set(REQUEST_LEDGER.FINISHED_AT, aged.getValue())
                    .where(REQUEST_LEDGER.REQUEST_ID.eq(key.getValue()))
                    .execute());
            }

            List<RequestLedger.RetentionPass> passes = service.runScheduledLedgerRetention(NOW);

            assertThat(passes).extracting(RequestLedger.RetentionPass::tenant).contains(tenant, other);
            assertThat(passes).allMatch(p -> !p.failed());
            assertThat(exists(tenant, expired)).isFalse();
            assertThat(exists(tenant, unexpired)).as("six days old is inside the seven-day window").isTrue();
            assertThat(exists(tenant, live)).as("a live lease is never deleted").isTrue();
            assertThat(exists(other, otherFresh)).isTrue();
        } finally {
            stopQuietly(service);
        }
    }

    /**
     * nexus-yye8l.21: a registry read that throws must not skip the reaper's tenants for the cycle. The registry
     * is renamed away for the one cycle, so the list query fails with an undefined-table error, and put back.
     */
    @Test
    void aLedgerTenantsFailure_stillRunsTheReaperTenantsPasses() throws Exception {
        UUID defaultOld = UUID.randomUUID();
        UUID defaultFresh = UUID.randomUUID();
        seed("default", "committed", NOW.minusDays(8), null, defaultOld);
        seed("default", "committed", NOW.minusDays(1), null, defaultFresh);
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "ledger-retention-token", ds, null, null, null, null, null,
            recording);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.request_ledger_tenants RENAME TO request_ledger_tenants_away");
            try {
                List<RequestLedger.RetentionPass> passes = service.runScheduledLedgerRetention(NOW);

                assertThat(passes).extracting(RequestLedger.RetentionPass::tenant).contains("default");
                assertThat(passes).allMatch(p -> !p.failed());
            } finally {
                su.createStatement().execute(
                    "ALTER TABLE nexus.request_ledger_tenants_away RENAME TO request_ledger_tenants");
            }
            assertThat(exists("default", defaultOld)).as("the default tenant's pass still ran").isFalse();
            assertThat(exists("default", defaultFresh)).isTrue();
        } finally {
            stopQuietly(service);
        }
    }

    /** Mark a claimed row terminal at {@code finishedAt}, as the commit marker would, with the clock the test controls. */
    private void finish(String tenant, UUID id, String state, OffsetDateTime finishedAt) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(REQUEST_LEDGER)
                .set(REQUEST_LEDGER.STATE, state)
                .set(REQUEST_LEDGER.HOLDER, (UUID) null)
                .set(REQUEST_LEDGER.STARTED_AT, finishedAt.minusMinutes(1))
                .set(REQUEST_LEDGER.FINISHED_AT, finishedAt)
                .where(REQUEST_LEDGER.TENANT_ID.eq(tenant).and(REQUEST_LEDGER.REQUEST_ID.eq(id)))
                .execute();
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
