// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-d94qm (shakeout F5): expired tuple rows lingered for days in the cloud ({@code board/ci/nexus-develop}
 * showed ~2,100 {@code expired_unpurged}; {@code directory/*} rows expired on 2026-10-18 were still there on
 * 2026-10-22). The purge arm itself is pinned by {@link NexusServiceTupleSweepTest}, which calls
 * {@code runScheduledTupleSweep} directly. Nothing pinned the SCHEDULE: the task was registered with a first
 * pass one full {@code SWEEP_INTERVAL_HOURS} after boot, so an engine process that lives less than six hours
 * (a redeploy, a task replacement) never ran it, while the request-ledger retention task and the chunk reaper
 * fire {@link ChunkReaper#INITIAL_DELAY} after boot for exactly that reason. Deleting the
 * {@code scheduleAtFixedRate} call, or restoring the long first delay, fails here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceTupleSweepScheduleTest {

    private static final String TOKEN = "tuple-sweep-schedule-token-3b1d";
    private static final String TENANT = "tuple-sweep-schedule-tenant";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    private TemplateRegistry registry;
    private Connection suConn;
    private DSLContext su;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection c = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(c);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);
        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        suConn = pg.createConnection("");
        su = DSL.using(suConn, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (suConn != null) suConn.close();
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

    private static void stopQuietly(NexusService service) {
        try {
            service.stop();
        } catch (Exception ignored) {
            // nothing further to tear down
        }
    }

    private static byte[] fakeId(String label) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(label.getBytes(StandardCharsets.UTF_8));
    }

    private void seedTuple(byte[] id, String subspace, OffsetDateTime expiresAt, OffsetDateTime createdAt) {
        su.insertInto(TUPLES, TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE, TUPLES.KEYS,
                        TUPLES.ATTEMPTS, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                .values(id, TENANT, subspace, "directory", JSONB.valueOf("{}"), 0, expiresAt, createdAt)
                .execute();
    }

    private boolean tupleExists(byte[] id) {
        return su.fetchExists(su.selectFrom(TUPLES).where(TUPLES.ID.eq(id)));
    }

    @Test
    void theSchedulerIsHandedTheTupleSweepTask_atTheBootDelay_andTheSweepInterval() throws Exception {
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, TOKEN, ds, null, null, null, null, registry, recording);
        try {
            Runnable task = service.tupleSweepTask();
            assertThat(task).as("a service wired with a template registry owns a tuple sweep task").isNotNull();
            assertThat(recording.fixedRate).as("the tuple sweep is registered on the sweep scheduler, exactly once")
                    .filteredOn(r -> r.task() == task)
                    .singleElement().satisfies(r -> {
                        assertThat(r.unit()).isEqualTo(TimeUnit.SECONDS);
                        assertThat(r.initialDelay())
                                .as("the first pass shortly after boot, like the reaper and the ledger retention: "
                                        + "an engine that restarts more often than every six hours must still purge")
                                .isEqualTo(ChunkReaper.INITIAL_DELAY.toSeconds())
                                .as("an absolute bound, so raising the shared constant cannot restore the bug")
                                .isLessThanOrEqualTo(300L);
                        assertThat(r.period()).isEqualTo(TimeUnit.HOURS.toSeconds(NexusService.SWEEP_INTERVAL_HOURS));
                    });
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void theSchedulerIsHandedTheT1TtlSweepTask_atTheBootDelay_andTheSweepInterval() throws Exception {
        // The T1 TTL sweep had the same six-hour first delay as the tuple sweep (nexus-d94qm sibling): an engine
        // that restarts more often than every six hours never expired scratch rows.
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, TOKEN, ds, null, null, null, null, null, recording);
        try {
            Runnable task = service.t1SweepTask();
            assertThat(task).as("every service owns a T1 TTL sweep task").isNotNull();
            assertThat(recording.fixedRate).as("the T1 sweep is registered on the sweep scheduler, exactly once")
                    .filteredOn(r -> r.task() == task)
                    .singleElement().satisfies(r -> {
                        assertThat(r.unit()).isEqualTo(TimeUnit.SECONDS);
                        assertThat(r.initialDelay()).isEqualTo(ChunkReaper.INITIAL_DELAY.toSeconds())
                                .isLessThanOrEqualTo(300L);
                        assertThat(r.period()).isEqualTo(TimeUnit.HOURS.toSeconds(NexusService.SWEEP_INTERVAL_HOURS));
                    });
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void anInstanceWithoutATemplateRegistrySchedulesNoTupleSweep() throws Exception {
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, TOKEN, ds, null, null, null, null, null, recording);
        try {
            assertThat(service.tupleSweepTask()).isNull();
        } finally {
            stopQuietly(service);
        }
    }

    @Test
    void theRegisteredTaskItself_purgesAnExpiredRow_andLeavesALiveOne() throws Exception {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        byte[] expired = fakeId("expired-" + UUID.randomUUID());
        byte[] live = fakeId("live-" + UUID.randomUUID());
        seedTuple(expired, "directory/sched-expired", now.minusMinutes(5), now.minusDays(2));
        seedTuple(live, "directory/sched-live", now.plusDays(1), now.minusHours(1));
        su.insertInto(TUPLE_TENANTS, TUPLE_TENANTS.TENANT_ID, TUPLE_TENANTS.FIRST_SEEN, TUPLE_TENANTS.LAST_SEEN)
                .values(TENANT, now.minusDays(3), now.minusDays(2))
                .onConflict(TUPLE_TENANTS.TENANT_ID).doNothing()
                .execute();

        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, TOKEN, ds, null, null, null, null, registry, recording);
        try {
            Runnable task = service.tupleSweepTask();
            assertThat(tupleExists(expired)).as("seeded: expired and not yet purged").isTrue();

            // The registered Runnable, not the method it wraps: one scheduled pass is the unit of the guarantee.
            task.run();

            assertThat(tupleExists(expired)).as("an expired row is gone after one scheduled pass").isFalse();
            assertThat(tupleExists(live)).as("a live row is untouched").isTrue();
        } finally {
            stopQuietly(service);
        }
    }
}
