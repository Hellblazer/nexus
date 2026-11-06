// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.AspectQueueDrain;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-hbnml: the engine-start drain of the retired aspect queue table is WIRED into {@code NexusService}:
 * scheduled ONCE on the sweep scheduler a few seconds after it is bound, the scheduled task is the one that records
 * the result, and {@code GET /v1/status} serves {@code aspect_queue_table_present} / {@code aspect_queue_rows_retained}
 * from that record. Before the drain has run the keys are absent, so a client cannot read a clean zero from an
 * engine that has not looked.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceAspectQueueDrainWiringTest {

    private static final ObjectMapper JSON = new ObjectMapper();

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
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** Records one-shot registrations and runs none of them. */
    private static final class RecordingScheduler extends java.util.concurrent.ScheduledThreadPoolExecutor {
        record Once(Runnable task, long delay, TimeUnit unit) {}

        final List<Once> once = new CopyOnWriteArrayList<>();

        RecordingScheduler() {
            super(1, r -> {
                Thread t = new Thread(r, "recording-scheduler");
                t.setDaemon(true);
                return t;
            });
        }

        @Override
        public java.util.concurrent.ScheduledFuture<?> schedule(Runnable command, long delay, TimeUnit unit) {
            once.add(new Once(command, delay, unit));
            return null;
        }
    }

    @Test
    void theDrainIsScheduledOnce_itsRunRecordsTheResult_andStatusServesIt() throws Exception {
        var recording = new RecordingScheduler();
        NexusService service = new NexusService(0, "aq-drain-wiring-token", ds, null, null, null, null, null,
            recording);
        try {
            service.start();
            int before = recording.once.size();
            AspectQueueDrain.Census census = () -> new AspectQueueDrain.Snapshot(true, Map.of("t", 2L));
            service.aspectQueueDrain(new AspectQueueDrain(null, null, census, List::of));

            assertThat(recording.once).as("one one-shot registration for the drain").hasSize(before + 1);
            var reg = recording.once.get(before);
            assertThat(reg.delay()).isEqualTo(NexusService.ASPECT_QUEUE_DRAIN_DELAY_SECONDS);
            assertThat(reg.unit()).isEqualTo(TimeUnit.SECONDS);

            assertThat(status(service).has("aspect_queue_rows_retained"))
                .as("before the drain has run the keys are absent").isFalse();

            reg.task().run();   // THE scheduled task, not a copy of its body

            JsonNode s = status(service);
            assertThat(s.get("aspect_queue_table_present").asBoolean()).isTrue();
            assertThat(s.get("aspect_queue_rows_retained").asLong()).as("rows are still in the table").isEqualTo(2);
            assertThat(s.get("aspect_queue_rows_retained_reasons").get("no_tuple_space").asLong()).isEqualTo(2);
        } finally {
            try {
                service.stop();
            } catch (Exception ignored) {
                // nothing further to tear down
            }
        }
    }

    private static JsonNode status(NexusService service) throws Exception {
        HttpResponse<String> resp = TestHttp.client().send(
            TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/status").GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return JSON.readTree(resp.body());
    }
}
