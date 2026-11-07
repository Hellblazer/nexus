// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.vectors.ChunkKeySweep;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * nexus-v75nw: the retire-keys dry run as a background job ({@code "async": true} on the sweep route, {@code GET
 * /v1/admin/chunk-keys/sweep/jobs/{id}} to poll), through the real service and a real PostgreSQL. The count of a large
 * tenant outlasts the edge's origin timeout (about 30 s, a 504 on the production tenant); the engine cannot wait for the
 * edge, so the start must answer while the count is still running. The count is made slow here by a census reader that
 * waits on a latch the test holds: while it is held the start has answered, the poll reads the stage, and the job's
 * answer, once the latch opens, is the synchronous call's answer.
 */
class ChunkKeySweepAsyncIntegrationTest extends ChunkKeysTestBase {

    private static final String JOBS = "/v1/admin/chunk-keys/sweep/jobs/";

    /** One per test: the base class keeps a single instance for the whole class, so a final field would stay open. */
    private volatile CountDownLatch release;

    @BeforeEach
    void closeTheLatch() {
        release = new CountDownLatch(1);
    }

    @AfterEach
    void restoreTheEnginesOwnSweep() {
        release.countDown();
        service.chunkKeysSweepForTests(null);
    }

    private static Map<String, Object> legacy(Map<String, Object> keyed) {
        Map<String, Object> m = new LinkedHashMap<>(keyed);
        m.put("title", "a title");
        m.put("source_path", "/x/y");
        return m;
    }

    /** A registered tenant with {@code n} swept-able rows in one collection; returns its tenant token. */
    private String tenantWithRows(String tenant, int n) throws Exception {
        String token = tenantToken(tenant);
        String col = conformant(tenant);
        Map<String, Map<String, Object>> rows = new LinkedHashMap<>();
        for (int i = 1; i <= n; i++) {
            rows.put("r" + i, legacy(keyed(tenant, col, "r" + i)));
        }
        seed(tenant, col, rows);
        return token;
    }

    /** The engine's sweep with a census that waits for {@link #release}: the slow count. */
    private AtomicBoolean slowCount() {
        AtomicBoolean entered = new AtomicBoolean();
        service.chunkKeysSweepForTests(new ChunkKeySweep(tenantScope, (t, c, bound) -> {
            entered.set(true);
            try {
                release.await(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return 0L;
        }));
        return entered;
    }

    private JsonNode poll(String token, String id) throws Exception {
        HttpResponse<String> r = get(JOBS + id, token);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return json(r);
    }

    private JsonNode pollUntilEnded(String token, String id) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (System.nanoTime() < deadline) {
            JsonNode j = poll(token, id);
            if (!"running".equals(j.path("state").asText())) {
                return j;
            }
            Thread.sleep(25);
        }
        throw new AssertionError("job " + id + " did not end within 60 s");
    }

    /** The answer without what differs between two runs of the same count: the audit ids and the timed batch SELECT. */
    private static JsonNode comparable(JsonNode body) {
        ObjectNode copy = body.deepCopy();
        copy.remove("audit");
        copy.remove("batch_cost");
        return copy;
    }

    // ---- the job lifecycle --------------------------------------------------------------------------------------

    @Test
    void aDryRunThatOutlastsTheEdgeCompletesThroughTheJob_andItsCountsEqualTheSynchronousOnes() throws Exception {
        String token = tenantWithRows("as-life", 4);
        JsonNode synchronous = json(sweep(token, plan()));   // the reference: the engine's own, fast sweep
        AtomicBoolean entered = slowCount();

        long t0 = System.nanoTime();
        HttpResponse<String> started = sweep(token, plan("async", true));
        long startMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);

        assertThat(started.statusCode()).as(started.body()).isEqualTo(202);
        JsonNode job = json(started);
        assertThat(job.path("state").asText()).isEqualTo("running");
        assertThat(job.path("tenant").asText()).isEqualTo("as-life");
        assertThat(job.path("job_id").asText()).isNotBlank();
        assertThat(job.has("body")).as("no answer yet").isFalse();

        // The count is held open, so a start that waited for it would still be waiting: this is the edge timeout.
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!entered.get() && System.nanoTime() < deadline) Thread.sleep(10);
        assertThat(entered.get()).as("the job reached the census").isTrue();
        assertThat(startMs).as("the start did not wait for the count").isLessThan(5_000);
        JsonNode midway = poll(token, job.path("job_id").asText());
        assertThat(midway.path("state").asText()).isEqualTo("running");
        assertThat(midway.path("stage").asText()).startsWith("census 1/1: ");
        assertThat(midway.has("http_status")).isFalse();

        release.countDown();
        JsonNode ended = pollUntilEnded(token, job.path("job_id").asText());

        assertThat(ended.path("state").asText()).isEqualTo("done");
        assertThat(ended.path("http_status").asInt()).isEqualTo(200);
        assertThat(comparable(ended.path("body")))
            .as("the job's answer is the synchronous answer, count for count")
            .isEqualTo(comparable(synchronous));
        assertThat(ended.path("body").path("totals").path("sweepable_rows").asLong()).isEqualTo(4);
        assertThat(ended.path("body").path("dry_run").asBoolean()).isTrue();
    }

    @Test
    void theSynchronousCallWithoutAsyncIsUnchanged() throws Exception {
        String token = tenantWithRows("as-sync", 2);

        HttpResponse<String> r = sweep(token, plan());

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).has("job_id")).as("no job unless asked").isFalse();
        assertThat(json(r).path("totals").path("sweepable_rows").asLong()).isEqualTo(2);
        assertThat(sweep(token, plan("async", false)).statusCode()).as("async false is a plain call").isEqualTo(200);
    }

    @Test
    void anApplyIgnoresAsync_itIsBoundedByItsOwnBudget() throws Exception {
        String token = tenantWithRows("as-apply", 3);

        HttpResponse<String> r = sweep(token, plan("dry_run", false, "async", true));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.has("job_id")).isFalse();
        assertThat(body.path("dry_run").asBoolean()).isFalse();
        assertThat(body.path("rows_swept").asLong()).isEqualTo(3);
    }

    @Test
    void aSecondStartOfTheSameRequestJoinsTheRunningJob_aDifferentOneIsRefused() throws Exception {
        String token = tenantWithRows("as-join", 2);
        slowCount();

        JsonNode first = json(sweep(token, plan("async", true)));
        JsonNode same = json(sweep(token, plan("async", true)));
        HttpResponse<String> different = sweep(token, plan("async", true, "accept", Map.of("keyless_notes", 5)));

        assertThat(same.path("job_id").asText()).isEqualTo(first.path("job_id").asText());
        assertThat(different.statusCode()).isEqualTo(409);
        assertThat(json(different).path("reason").asText()).isEqualTo("sweep_job_running");
        assertThat(json(different).path("job_id").asText()).isEqualTo(first.path("job_id").asText());
        release.countDown();
        assertThat(pollUntilEnded(token, first.path("job_id").asText()).path("state").asText()).isEqualTo("done");
    }

    @Test
    void aCountThatThrowsEndsTheJobFailed_withTheSweepFailedBody_andNothingIsChanged() throws Exception {
        String token = tenantWithRows("as-fail", 2);
        javax.sql.DataSource broken = (javax.sql.DataSource) java.lang.reflect.Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {javax.sql.DataSource.class}, (proxy, method, args) -> {
                throw new java.sql.SQLException("connection refused");
            });
        service.chunkKeysSweepForTests(new ChunkKeySweep(new dev.nexus.service.db.TenantScope(broken), (t, c, b) -> 0L));

        HttpResponse<String> started = sweep(token, plan("async", true));
        assertThat(started.statusCode()).as("the start does not run the count, so it cannot fail with it").isEqualTo(202);
        JsonNode ended = pollUntilEnded(token, json(started).path("job_id").asText());

        assertThat(ended.path("state").asText()).isEqualTo("failed");
        assertThat(ended.path("http_status").asInt()).isEqualTo(500);
        assertThat(ended.path("body").path("reason").asText()).isEqualTo("sweep_failed");
        assertThat(ended.path("body").path("rows_changed").asBoolean()).isFalse();
        assertThat(ended.path("body").path("error").asText()).doesNotContain("connection refused");
        assertThat(retiredRowCount("as-fail", conformant("as-fail"))).isEqualTo(2);
        service.chunkKeysSweepForTests(null);
        assertThat(sweep(token, plan("async", true)).statusCode()).as("a failed job does not hold the tenant")
            .isEqualTo(202);
    }

    // ---- who may read a job ------------------------------------------------------------------------------------

    @Test
    void aJobIsNotReadableByAnotherTenant_anUnknownIdIs404_aPostIs405_noCredentialIs401() throws Exception {
        String token = tenantWithRows("as-owner", 1);
        String stranger = tenantWithRows("as-stranger", 1);
        slowCount();
        String id = json(sweep(token, plan("async", true))).path("job_id").asText();

        assertThat(get(JOBS + id, token).statusCode()).as("positive control: the owner reads it").isEqualTo(200);
        HttpResponse<String> other = get(JOBS + id, stranger);
        assertThat(other.statusCode()).isEqualTo(404);
        assertThat(other.body()).doesNotContain("as-owner");
        assertThat(get(JOBS + "no-such-job", token).statusCode()).isEqualTo(404);
        assertThat(get(JOBS, token).statusCode()).isEqualTo(404);
        assertThat(post(JOBS + id, token, Map.of()).statusCode()).isEqualTo(405);
        assertThat(get(JOBS + id, null).statusCode()).isEqualTo(401);
        assertThat(get(JOBS + id, token(("as-owner"), "data")).statusCode()).as("a data credential").isEqualTo(403);
        release.countDown();
        assertThat(pollUntilEnded(token, id).path("state").asText()).isEqualTo("done");
    }

    // ---- a root credential's audit -----------------------------------------------------------------------------

    @Test
    void aRootAsyncDryRunWritesItsIntentAtOnce_andItsOutcomeWhenTheJobEnds() throws Exception {
        String t = "as-root";
        tenantWithRows(t, 3);
        slowCount();

        JsonNode job = json(sweep(ROOT, plan("tenant", t, "async", true, "operator", "sam-laptop")));
        String id = job.path("job_id").asText();

        List<String> phases = phases(t);
        assertThat(phases).as("the intent row is in before the answer, the outcome is not yet").containsExactly("started");
        release.countDown();
        JsonNode ended = pollUntilEnded(ROOT, id);

        assertThat(ended.path("state").asText()).isEqualTo("done");
        assertThat(phases(t)).containsExactly("started", "finished");
        JsonNode audit = ended.path("body").path("audit");
        assertThat(audit.path("intent_id").asLong()).isPositive();
        assertThat(audit.path("outcome_id").asLong()).isPositive();
        assertThat(ended.path("body").path("tenant").asText()).isEqualTo(t);
    }

    private List<String> phases(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(GC_AUDIT.DETAILS).from(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq("retire_keys_sweep"))
                .orderBy(GC_AUDIT.ID)
                .fetch(r -> {
                    try {
                        return MAPPER.readTree(r.get(GC_AUDIT.DETAILS).data()).path("phase").asText();
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                });
        }
    }
}
