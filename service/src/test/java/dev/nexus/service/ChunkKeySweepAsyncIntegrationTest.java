// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.http.ChunkKeysAdminHandler;
import dev.nexus.service.http.ChunkKeysSweepJobs;
import dev.nexus.service.vectors.ChunkKeySweep;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * nexus-v75nw: the retire-keys dry run as a background job ({@code "async": true} on the sweep route, {@code GET
 * /v1/admin/chunk-keys/sweep/jobs/{id}} to poll), through the real service and a real PostgreSQL. The count of a large
 * tenant outlasts the edge's origin timeout (about 30 s, a 504 on the production tenant); the engine cannot wait for the
 * edge, so the start must answer while the count is still running. The count is made slow here by a census reader that
 * waits on a latch the test holds: while it is held the start has answered, the poll reads the stage, and the job's
 * answer, once the latch opens, is the synchronous call's answer.
 *
 * <p>The apply runs as a job too: the client loop of the synchronous apply, inside the engine, with the same
 * precondition refusal on its first call, a cancel that stops it at a batch boundary and a wall-clock cap.
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

    // ---- the dry run's continuation: the apply goes straight to the batches ------------------------------------

    /** The engine's sweep with a census reader that counts its calls and never waits: one instance, so one key. */
    private AtomicInteger countingSweep() {
        AtomicInteger calls = new AtomicInteger();
        service.chunkKeysSweepForTests(new ChunkKeySweep(tenantScope, (t, c, bound) -> {
            calls.incrementAndGet();
            return 0L;
        }));
        return calls;
    }

    private JsonNode finishedJob(String token, Map<String, Object> startBody) throws Exception {
        HttpResponse<String> started = sweep(token, startBody);
        assertThat(started.statusCode()).as(started.body()).isEqualTo(202);
        return pollUntilEnded(token, json(started).path("job_id").asText());
    }

    // ---- who may read a job: the caller tenant AND root-ness ------------------------------------------------

    @Test
    void aTenantCredentialOfTheRootsOwnCallerTenantGets404OnARootStartedJob() throws Exception {
        String target = "as-c3";
        tenantWithRows(target, 1);
        String sameTenantNotRoot = tenantToken(dev.nexus.service.db.TenantConstants.DEFAULT_TENANT);
        slowCount();
        String id = json(sweep(ROOT, plan("tenant", target, "async", true))).path("job_id").asText();

        assertThat(get(JOBS + id, ROOT).statusCode()).as("positive control: the root credential reads it").isEqualTo(200);
        HttpResponse<String> r = get(JOBS + id, sameTenantNotRoot);
        assertThat(r.statusCode()).as("the root's own tenant, but not the root credential").isEqualTo(404);
        assertThat(r.body()).doesNotContain(target);
        release.countDown();
        assertThat(pollUntilEnded(ROOT, id).path("state").asText()).isEqualTo("done");
    }

    // ---- C1, C2: the job ends as the synchronous call ends -----------------------------------------------------

    @Test
    void anErrorInsideTheJobRecordsAFailedOutcome_notFinishedWithEmptyCounts() throws Exception {
        String t = "as-error";
        tenantWithRows(t, 2);
        service.chunkKeysSweepForTests(new ChunkKeySweep(tenantScope, (tn, c, bound) -> {
            throw new AssertionError("an Error, not an Exception");
        }));

        JsonNode ended = pollUntilEnded(ROOT, json(sweep(ROOT, plan("tenant", t, "async", true))).path("job_id").asText());

        assertThat(ended.path("state").asText()).isEqualTo("failed");
        assertThat(ended.path("http_status").asInt()).isEqualTo(500);
        assertThat(phases(t)).as("the audit says failed, not finished").containsExactly("started", "failed");
    }

    @Test
    void poolExhaustionInAJobIsTheRetryable503_anIllegalArgumentIsA400() throws Exception {
        String token = tenantWithRows("as-map", 1);
        service.chunkKeysSweepForTests(brokenSweep(new java.sql.SQLTransientConnectionException("pool")));
        JsonNode pool = pollUntilEnded(token, json(sweep(token, plan("async", true))).path("job_id").asText());
        assertThat(pool.path("http_status").asInt()).isEqualTo(503);
        assertThat(pool.path("body").path("error").asText()).containsIgnoringCase("pool exhausted");
        assertThat(pool.path("state").asText()).isEqualTo("failed");

        service.chunkKeysSweepForTests(brokenSweep(new IllegalArgumentException("bad batch size")));
        JsonNode bad = pollUntilEnded(token, json(sweep(token, plan("async", true))).path("job_id").asText());
        assertThat(bad.path("http_status").asInt()).isEqualTo(400);
        assertThat(bad.path("body").path("error").asText()).contains("bad batch size");
    }

    private ChunkKeySweep brokenSweep(Exception cause) {
        javax.sql.DataSource broken = (javax.sql.DataSource) java.lang.reflect.Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {javax.sql.DataSource.class}, (proxy, method, args) -> {
                if (cause instanceof RuntimeException re) {
                    throw re;
                }
                throw cause;
            });
        return new ChunkKeySweep(new dev.nexus.service.db.TenantScope(broken), (t, c, b) -> 0L);
    }

    // ---- C4: a tenant reset while a job runs ------------------------------------------------------------------

    @Test
    void aTenantResetHidesARunningJobButKeepsTheOnePerTenantGuard_andItsOutcomeRowIsNotWritten() throws Exception {
        String t = "as-c4";
        tenantWithRows(t, 2);
        slowCount();
        String id = json(sweep(ROOT, plan("tenant", t, "async", true))).path("job_id").asText();
        assertThat(phases(t)).containsExactly("started");

        assertThat(service.chunkKeysJobsForTests().evictTenant(t)).isEqualTo(1);

        assertThat(get(JOBS + id, ROOT).statusCode()).as("no poll finds it").isEqualTo(404);
        HttpResponse<String> again = sweep(ROOT, plan("tenant", t, "async", true));
        assertThat(again.statusCode()).as("the worker still runs: the tenant keeps its one job").isEqualTo(409);
        assertThat(json(again).path("reason").asText()).isEqualTo("sweep_job_running");
        release.countDown();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        HttpResponse<String> fresh;
        do {
            Thread.sleep(25);
            fresh = sweep(ROOT, plan("tenant", t, "async", true));
        } while (fresh.statusCode() == 409 && System.nanoTime() < deadline);
        assertThat(fresh.statusCode()).as("once the worker ends the tenant can start again").isEqualTo(202);
        assertThat(json(fresh).path("job_id").asText()).isNotEqualTo(id);
        pollUntilEnded(ROOT, json(fresh).path("job_id").asText());
        // Each refused start (409) writes its own intent+failed pair, however many the loop needed; the one job that
        // ended on a live registry wrote the only "finished" row. The evicted job wrote none.
        assertThat(java.util.Collections.frequency(phases(t), "finished"))
            .as("the evicted job wrote no outcome row into the reset tenant").isEqualTo(1);
    }

    // ---- C6: nothing caches a job answer -----------------------------------------------------------------------

    @Test
    void theStartAndEveryPollAnswerCarryCacheControlNoStore() throws Exception {
        String token = tenantWithRows("as-nostore", 1);
        HttpResponse<String> started = sweep(token, plan("async", true));
        String id = json(started).path("job_id").asText();
        pollUntilEnded(token, id);

        assertThat(started.headers().firstValue("Cache-Control")).hasValue("no-store");
        assertThat(get(JOBS + id, token).headers().firstValue("Cache-Control")).hasValue("no-store");
        HttpResponse<String> miss = get(JOBS + "nope", token);
        assertThat(miss.statusCode()).isEqualTo(404);
        assertThat(miss.headers().firstValue("Cache-Control")).hasValue("no-store");
        assertThat(get(JOBS + "..", token).statusCode()).isEqualTo(404);
    }

    // ---- the apply as a job -------------------------------------------------------------------------------------

    /** The synchronous client loop (the verb's): calls with the token handed back until {@code done}; the totals. */
    private JsonNode synchronousLoop(String token, Map<String, Object> request) throws Exception {
        long rows = 0;
        int batches = 0;
        int calls = 0;
        JsonNode last;
        Map<String, Object> resume = null;
        do {
            Map<String, Object> body = new LinkedHashMap<>(request);
            if (resume != null) {
                body.put("resume", resume);
            }
            HttpResponse<String> r = sweep(token, body);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            last = json(r);
            rows += last.path("rows_swept").asLong();
            batches += last.path("batches").asInt();
            calls++;
            resume = last.path("resume").isNull() ? null
                : MAPPER.convertValue(last.path("resume"), new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() { });
            assertThat(calls).as("the loop ends").isLessThan(500);
        } while (!last.path("done").asBoolean());
        ObjectNode total = MAPPER.createObjectNode();
        total.put("rows_swept", rows);
        total.put("batches", batches);
        total.put("calls", calls);
        total.put("done", true);
        total.put("remaining_rows", last.path("remaining_rows").asLong());
        total.put("locked_skipped_rows", last.path("locked_skipped_rows").asLong());
        total.set("blocked_collections", last.path("blocked_collections"));
        return total;
    }

    @Test
    void anApplyJobThatOutlastsTheEdgeCompletes_andItsOutcomeEqualsTheSynchronousLoopOnTheSameFixture() throws Exception {
        String viaLoop = tenantWithRows("ap-loop", 7);
        String viaJob = tenantWithRows("ap-job", 7);
        Map<String, Object> shape = plan("dry_run", false, "batch_size", 2, "max_batches", 1);
        JsonNode reference = synchronousLoop(viaLoop, shape);
        assertThat(retiredRowCount("ap-loop", conformant("ap-loop"))).isZero();
        AtomicBoolean entered = slowCount();

        long t0 = System.nanoTime();
        HttpResponse<String> started = sweep(viaJob, plan("dry_run", false, "batch_size", 2, "max_batches", 1, "async", true));
        long startMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);

        assertThat(started.statusCode()).as(started.body()).isEqualTo(202);
        JsonNode job = json(started);
        assertThat(job.path("kind").asText()).isEqualTo("apply");
        assertThat(job.path("state").asText()).isEqualTo("running");
        assertThat(job.has("body")).isFalse();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!entered.get() && System.nanoTime() < deadline) Thread.sleep(10);
        assertThat(entered.get()).as("the job's first call is in the precondition count").isTrue();
        assertThat(startMs).as("the start did not wait for the count, let alone the sweep").isLessThan(5_000);
        JsonNode midway = poll(viaJob, job.path("job_id").asText());
        assertThat(midway.path("state").asText()).isEqualTo("running");
        assertThat(midway.path("stage").asText()).startsWith("census 1/1: ");
        assertThat(midway.path("rows_swept").asLong()).isZero();
        assertThat(midway.path("remaining_rows").isNull()).as("not counted yet").isTrue();
        assertThat(retiredRowCount("ap-job", conformant("ap-job"))).as("nothing swept while the count runs").isEqualTo(7);

        release.countDown();
        JsonNode ended = pollUntilEnded(viaJob, job.path("job_id").asText());

        assertThat(ended.path("state").asText()).isEqualTo("done");
        assertThat(ended.path("http_status").asInt()).isEqualTo(200);
        JsonNode body = ended.path("body");
        assertThat(body.path("dry_run").asBoolean()).isFalse();
        assertThat(body.path("done").asBoolean()).isTrue();
        assertThat(body.path("rows_swept").asLong()).as("the loop's rows").isEqualTo(reference.path("rows_swept").asLong());
        assertThat(body.path("batches").asInt()).as("the loop's batches").isEqualTo(reference.path("batches").asInt());
        assertThat(body.path("calls").asInt()).as("the loop's calls").isEqualTo(reference.path("calls").asInt());
        assertThat(body.path("remaining_rows").asLong()).isEqualTo(reference.path("remaining_rows").asLong());
        assertThat(body.path("locked_skipped_rows").asLong()).isEqualTo(reference.path("locked_skipped_rows").asLong());
        assertThat(body.path("blocked_collections")).isEqualTo(reference.path("blocked_collections"));
        assertThat(retiredRowCount("ap-job", conformant("ap-job"))).isZero();
        assertThat(ended.path("rows_swept").asLong()).as("the poll's progress ends at the outcome's").isEqualTo(7);
        assertThat(ended.path("batches").asInt()).isEqualTo(body.path("batches").asInt());
        assertThat(ended.path("remaining_rows").asLong()).isZero();
    }

    @Test
    void anApplyJobWhoseResidueGrewSinceTheDryRunRefusesExactlyAsTheSynchronousApplyDoes_andSweepsNothing() throws Exception {
        String tenant = "ap-grow";
        String token = tenantWithRows(tenant, 4);
        JsonNode clean = finishedJob(token, plan("async", true)).path("body");
        assertThat(clean.path("blocking")).as("the dry run found nothing in the way").isEmpty();
        try (Connection su = pg.createConnection("")) {
            // The residue grows after the dry run: a document with no physical collection.
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), tenant, "grow.1");
        }

        JsonNode job = finishedJob(token, plan("dry_run", false, "async", true));
        JsonNode synchronous = json(sweep(token, plan("dry_run", false)));

        assertThat(job.path("state").asText()).isEqualTo("failed");
        assertThat(job.path("http_status").asInt()).as("the synchronous apply's status").isEqualTo(409);
        assertThat(job.path("body").path("reason").asText()).isEqualTo("sweep_preconditions_not_met");
        assertThat(job.path("body").path("blocking")).as("the synchronous apply's blocking list")
            .isEqualTo(synchronous.path("blocking"));
        assertThat(synchronous.path("reason").asText()).isEqualTo("sweep_preconditions_not_met");
        assertThat(job.path("body").path("blocking")).isNotEmpty();
        assertThat(retiredRowCount(tenant, conformant(tenant))).as("the job swept nothing").isEqualTo(4);

        JsonNode accepted = finishedJob(token, plan("dry_run", false, "async", true,
            "accept", accept("homeless_documents", 1, "generated_nulls", 1)));
        assertThat(accepted.path("body").path("rows_swept").asLong()).as("positive control: accepted, it sweeps").isEqualTo(4);
        assertThat(retiredRowCount(tenant, conformant(tenant))).isZero();
    }

    @Test
    void anAsyncApplyThatCarriesAResumeIsRefused400_theJobRunsTheWholeLoop() throws Exception {
        String token = tenantWithRows("ap-resume", 2);

        HttpResponse<String> r = sweep(token, plan("dry_run", false, "async", true, "resume", Map.of("sig", "x")));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(400);
        assertThat(retiredRowCount("ap-resume", conformant("ap-resume"))).isEqualTo(2);
    }

    @Test
    void aCancelBeforeTheFirstBatchStopsTheApplyWithNothingSwept_andRecordsCancelledInTheOutcomeRow() throws Exception {
        String t = "ap-cancel";
        tenantWithRows(t, 5);
        AtomicBoolean entered = slowCount();
        JsonNode job = json(sweep(ROOT, plan("tenant", t, "dry_run", false, "async", true, "operator", "sam-laptop")));
        String id = job.path("job_id").asText();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!entered.get() && System.nanoTime() < deadline) Thread.sleep(10);

        HttpResponse<String> cancel = post(JOBS + id + "/cancel", ROOT, Map.of());

        assertThat(cancel.statusCode()).as(cancel.body()).isEqualTo(200);
        assertThat(json(cancel).path("cancel_accepted").asBoolean()).isTrue();
        assertThat(json(cancel).path("stopping").asText()).isEqualTo("cancelled");
        release.countDown();
        JsonNode ended = pollUntilEnded(ROOT, id);
        assertThat(ended.path("state").asText()).isEqualTo("done");
        JsonNode body = ended.path("body");
        assertThat(body.path("stopped").asText()).isEqualTo("cancelled");
        assertThat(body.path("cancelled").asBoolean()).isTrue();
        assertThat(body.path("done").asBoolean()).isFalse();
        assertThat(body.path("rows_swept").asLong()).isZero();
        assertThat(body.path("remaining_rows").asLong()).as("the rows still to sweep, from the count").isEqualTo(5);
        assertThat(retiredRowCount(t, conformant(t))).isEqualTo(5);
        assertThat(phases(t)).containsExactly("started", "finished");
        try (Connection su = pg.createConnection("")) {
            JsonNode counts = MAPPER.readTree(DSL.using(su, SQLDialect.POSTGRES).select(GC_AUDIT.DETAILS).from(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(t)).and(GC_AUDIT.OPERATION.eq("retire_keys_sweep"))
                .orderBy(GC_AUDIT.ID.desc()).limit(1).fetchOne().get(GC_AUDIT.DETAILS).data()).path("counts");
            assertThat(counts.path("cancelled").asBoolean()).isTrue();
            assertThat(counts.path("stopped").asText()).isEqualTo("cancelled");
            assertThat(counts.path("rows_swept").asLong()).isZero();
        }
        assertThat(post(JOBS + id + "/cancel", ROOT, Map.of()).statusCode()).as("a finished job").isEqualTo(200);
        assertThat(json(post(JOBS + id + "/cancel", ROOT, Map.of())).path("cancel_accepted").asBoolean()).isFalse();
    }

    @Test
    void theCancelRouteAnswersTheStartersOnly_aDryRunIsNotCancellable_andOnlyAPostCancels() throws Exception {
        String token = tenantWithRows("ap-who", 2);
        String stranger = tenantWithRows("ap-who-other", 1);
        slowCount();
        String applyId = json(sweep(token, plan("dry_run", false, "async", true))).path("job_id").asText();

        assertThat(post(JOBS + applyId + "/cancel", stranger, Map.of()).statusCode()).as("another tenant").isEqualTo(404);
        assertThat(post(JOBS + "no-such/cancel", token, Map.of()).statusCode()).isEqualTo(404);
        assertThat(post(JOBS + applyId + "/cancel", null, Map.of()).statusCode()).isEqualTo(401);
        assertThat(post(JOBS + applyId + "/cancel", token("ap-who", "data"), Map.of()).statusCode()).isEqualTo(403);
        assertThat(get(JOBS + applyId + "/cancel", token).statusCode()).as("a GET does not cancel").isEqualTo(405);
        assertThat(json(get(JOBS + applyId, token)).path("stopping").isMissingNode()).as("nothing was asked yet").isTrue();
        release.countDown();
        pollUntilEnded(token, applyId);

        release = new CountDownLatch(1);
        slowCount();
        String dryId = json(sweep(stranger, plan("async", true))).path("job_id").asText();
        HttpResponse<String> refused = post(JOBS + dryId + "/cancel", stranger, Map.of());
        assertThat(refused.statusCode()).isEqualTo(409);
        assertThat(json(refused).path("reason").asText()).isEqualTo("sweep_job_not_cancellable");
        release.countDown();
        assertThat(pollUntilEnded(stranger, dryId).path("state").asText()).as("it ran to its end").isEqualTo("done");
    }

    @Test
    void aDryRunAndAnApplyOfOneTenantExcludeEachOther() throws Exception {
        String tenant = "ap-excl";
        String token = tenantWithRows(tenant, 3);
        slowCount();
        JsonNode dry = json(sweep(token, plan("async", true)));

        HttpResponse<String> apply = sweep(token, plan("dry_run", false, "async", true));

        assertThat(apply.statusCode()).as("an apply while a dry run counts").isEqualTo(409);
        assertThat(json(apply).path("reason").asText()).isEqualTo("sweep_job_running");
        assertThat(json(apply).path("job_id").asText()).isEqualTo(dry.path("job_id").asText());
        assertThat(json(apply).path("kind").asText()).as("the refusal names what is running").isEqualTo("dry_run");
        release.countDown();
        pollUntilEnded(token, dry.path("job_id").asText());

        release = new CountDownLatch(1);
        slowCount();
        JsonNode running = json(sweep(token, plan("dry_run", false, "async", true)));
        HttpResponse<String> dryAgain = sweep(token, plan("async", true));
        assertThat(dryAgain.statusCode()).as("a dry run while an apply runs").isEqualTo(409);
        assertThat(json(dryAgain).path("kind").asText()).as("a client may rejoin a running apply").isEqualTo("apply");
        assertThat(json(sweep(token, plan("dry_run", false, "async", true))).path("job_id").asText())
            .as("the same apply joins the running one").isEqualTo(running.path("job_id").asText());
        assertThat(sweep(token, plan("dry_run", false, "async", true, "batch_size", 3)).statusCode())
            .as("another apply request is another job").isEqualTo(409);
        release.countDown();
        assertThat(pollUntilEnded(token, running.path("job_id").asText()).path("state").asText()).isEqualTo("done");
    }

    @Test
    void aFinishedJobIsNeverJoined_theSameRequestRunsAgainAndRecounts() throws Exception {
        String token = tenantWithRows("ap-nojoin", 3);
        AtomicInteger census = countingSweep();
        JsonNode first = finishedJob(token, plan("async", true));
        int counted = census.get();
        assertThat(counted).as("the first job counted").isPositive();

        JsonNode second = finishedJob(token, plan("async", true));

        assertThat(second.path("job_id").asText()).as("a finished job is read by id, never joined")
            .isNotEqualTo(first.path("job_id").asText());
        assertThat(census.get()).as("the second start recounted").isGreaterThan(counted);
        assertThat(get(JOBS + first.path("job_id").asText(), token).statusCode()).as("the first stays readable")
            .isEqualTo(200);
    }

    @Test
    void aBlockedOutcomeIsNotReplayedToTheRerun_theRemedyIsSeenAtOnce() throws Exception {
        String tenant = "ap-remedy";
        String token = tenantWithRows(tenant, 2);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), tenant, "remedy.1");
        }
        JsonNode blocked = finishedJob(token, plan("async", true)).path("body");
        assertThat(blocked.path("blocking")).isNotEmpty();

        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();   // the remedy
        }
        JsonNode rerun = finishedJob(token, plan("async", true)).path("body");

        assertThat(rerun.path("blocking")).as("the same command, a minute later, sees the fix").isEmpty();
    }

    @Test
    void aWallClockCapEndsAnApplyAtItsFirstBoundary_andADryRunAtItsNextStage() throws Exception {
        String t = "ap-cap";
        String token = tenantWithRows(t, 4);
        service.chunkKeysJobsForTests().maxRuntimeForTests(Duration.ofMillis(1));
        try {
            slowCount();
            String applyId = json(sweep(token, plan("dry_run", false, "async", true))).path("job_id").asText();
            Thread.sleep(50);
            assertThat(json(get(JOBS + applyId, token)).path("stopping").asText()).isEqualTo("wall_clock_cap");
            release.countDown();
            JsonNode apply = pollUntilEnded(token, applyId);
            assertThat(apply.path("state").asText()).isEqualTo("done");
            assertThat(apply.path("body").path("stopped").asText()).isEqualTo("wall_clock_cap");
            assertThat(apply.path("body").path("cancelled").asBoolean()).isFalse();
            assertThat(apply.path("body").path("done").asBoolean()).isFalse();
            assertThat(apply.path("body").path("rows_swept").asLong()).isZero();
            assertThat(retiredRowCount(t, conformant(t))).as("no batch ran past the cap").isEqualTo(4);

            release = new CountDownLatch(1);
            slowCount();
            String dryId = json(sweep(token, plan("async", true))).path("job_id").asText();
            Thread.sleep(50);
            release.countDown();
            JsonNode dry = pollUntilEnded(token, dryId);
            assertThat(dry.path("state").asText()).isEqualTo("failed");
            assertThat(dry.path("body").path("reason").asText()).isEqualTo("sweep_job_stopped");
            assertThat(dry.path("body").path("stopped").asText()).isEqualTo("wall_clock_cap");
        } finally {
            service.chunkKeysJobsForTests().maxRuntimeForTests(ChunkKeysSweepJobs.MAX_RUNTIME);
        }
    }

    @Test
    void aTenantResetStopsARunningApplyAtItsBoundary_andItWritesNoOutcomeRow() throws Exception {
        String t = "ap-reset";
        tenantWithRows(t, 3);
        AtomicBoolean entered = slowCount();
        String id = json(sweep(ROOT, plan("tenant", t, "dry_run", false, "async", true))).path("job_id").asText();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!entered.get() && System.nanoTime() < deadline) Thread.sleep(10);

        assertThat(service.chunkKeysJobsForTests().evictTenant(t)).isEqualTo(1);
        release.countDown();
        long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        HttpResponse<String> fresh;
        do {
            Thread.sleep(25);
            fresh = sweep(ROOT, plan("tenant", t, "async", true));
        } while (fresh.statusCode() == 409 && System.nanoTime() < end);

        assertThat(fresh.statusCode()).as("the worker ended, so the tenant can start again").isEqualTo(202);
        pollUntilEnded(ROOT, json(fresh).path("job_id").asText());
        assertThat(retiredRowCount(t, conformant(t))).as("the evicted apply swept nothing").isEqualTo(3);
        assertThat(java.util.Collections.frequency(phases(t), "finished"))
            .as("the evicted apply wrote no outcome row: the one finished row is the fresh dry run's").isEqualTo(1);
        assertThat(get(JOBS + id, ROOT).statusCode()).as("and no poll finds the evicted job").isEqualTo(404);
    }

    @Test
    void anApplyFailingInsideTheLoopEndsFailedWithTheSweepFailedBody_andTheRowsItDidSweepAreNamed() throws Exception {
        String t = "ap-fail";
        String token = tenantWithRows(t, 2);
        service.chunkKeysSweepForTests(brokenSweep(new java.sql.SQLException("connection refused")));

        JsonNode ended = finishedJob(token, plan("dry_run", false, "async", true));

        assertThat(ended.path("state").asText()).isEqualTo("failed");
        assertThat(ended.path("http_status").asInt()).isEqualTo(500);
        assertThat(ended.path("body").path("reason").asText()).isEqualTo("sweep_failed");
        assertThat(ended.path("body").path("dry_run").asBoolean()).isFalse();
        assertThat(ended.path("body").path("rows_changed").asBoolean()).isFalse();
        assertThat(ended.path("body").path("error").asText()).doesNotContain("connection refused");
        service.chunkKeysSweepForTests(null);
        assertThat(retiredRowCount(t, conformant(t))).isEqualTo(2);
    }

    // ---- the sweep's own stop at a batch boundary --------------------------------------------------------------

    @Test
    void applyStopsBetweenBatchesNeverInsideOne_andReportsTheWholeBatchesItRan() throws Exception {
        String tenant = "ap-bound";
        tenantWithRows(tenant, 6);
        Map<String, Object> p = plan();
        @SuppressWarnings("unchecked")
        dev.nexus.service.vectors.ChunkKeyPlan mine = dev.nexus.service.vectors.ChunkKeyPlan.of(
            (List<String>) p.get("allowed_keys"), (List<String>) p.get("bypass_prefixes"),
            (List<String>) p.get("quarantine_implies"));
        ChunkKeySweep sw = new ChunkKeySweep(tenantScope, (t, c, b) -> 0L);
        AtomicBoolean stop = new AtomicBoolean();
        List<Long> remainingSeen = new ArrayList<>();
        List<String> stages = new ArrayList<>();
        ChunkKeySweep.Progress progress = new ChunkKeySweep.Progress(stop::get, pr -> {
            remainingSeen.add(pr.remaining());
            if (pr.batches() == 2) {
                stop.set(true);   // asked while batch 3 is next: it must not start
            }
        }, stages::add);

        ChunkKeySweep.Applied a = sw.apply(tenant, mine, Map.of(), null, 1, 25, Duration.ofSeconds(30), null, progress);

        assertThat(progress.stopped()).isTrue();
        assertThat(a.done()).as("rows are left").isFalse();
        assertThat(a.batches()).as("the third batch did not start").isEqualTo(2);
        assertThat(a.rowsSwept()).as("one row a batch: whole batches only").isEqualTo(2);
        assertThat(a.remainingRows()).isEqualTo(4);
        assertThat(retiredRowCount(tenant, conformant(tenant))).as("the store holds exactly what the batches swept").isEqualTo(4);
        assertThat(remainingSeen).as("progress after each batch, falling").containsExactly(5L, 4L);
        assertThat(stages).as("the count's stages, then sweeping").contains("collections").endsWith("sweeping");

        ChunkKeySweep.Applied rest = sw.apply(tenant, mine, Map.of(), null, 1, 25, Duration.ofSeconds(30), a.resume());
        assertThat(rest.done()).as("the token the stopped call returned takes up where it stopped").isTrue();
        assertThat(rest.rowsSwept()).isEqualTo(4);
        assertThat(retiredRowCount(tenant, conformant(tenant))).isZero();
    }

    // ---- C7: the refusals before the 202, Busy over HTTP, the root join, accept and batch_size ----------------------

    @Test
    void asyncRefusalsAreAnsweredBeforeAnyJobStarts() throws Exception {
        String token = tenantWithRows("as-ref", 1);
        AtomicInteger census = countingSweep();

        HttpResponse<String> mismatch = sweep(token, plan("tenant", "someone-else", "async", true));
        assertThat(mismatch.statusCode()).isEqualTo(403);
        assertThat(json(mismatch).path("reason").asText()).isEqualTo("tenant_mismatch");

        HttpResponse<String> unknown = sweep(ROOT, plan("tenant", "never-registered-tenant", "async", true));
        assertThat(unknown.statusCode()).isEqualTo(422);
        assertThat(json(unknown).path("reason").asText()).isEqualTo("unknown_tenant");

        ChunkKeysAdminHandler.Audit real = service.chunkKeysAudit();
        service.chunkKeysAudit(null);
        try {
            HttpResponse<String> noAudit = sweep(ROOT, plan("tenant", "as-ref", "async", true));
            assertThat(noAudit.statusCode()).isEqualTo(503);
            assertThat(json(noAudit).path("reason").asText()).isEqualTo("audit_unavailable");
        } finally {
            service.chunkKeysAudit(real);
        }
        for (HttpResponse<String> r : List.of(mismatch, unknown)) {
            assertThat(json(r).has("job_id")).as("no job on a refusal").isFalse();
        }
        assertThat(census.get()).as("nothing counted").isZero();
        assertThat(service.chunkKeysJobsForTests().size()).as("positive control below").isGreaterThanOrEqualTo(0);
        assertThat(sweep(token, plan("async", true)).statusCode()).isEqualTo(202);
    }

    @Test
    void aStartPastTheRegistryBoundIs503SweepJobsBusyOverHttp_andItsRootIntentRowIsClosed() throws Exception {
        slowCount();
        int bound = ChunkKeysSweepJobs.MAX_RUNNING + ChunkKeysSweepJobs.QUEUE;
        for (int i = 0; i < bound; i++) {
            String tn = "as-busy" + i;
            tenantWithRows(tn, 1);
            HttpResponse<String> r = sweep(ROOT, plan("tenant", tn, "async", true));
            assertThat(r.statusCode()).as("start %d: %s", i, r.body()).isEqualTo(202);
        }
        String last = "as-busy-over";
        tenantWithRows(last, 1);

        HttpResponse<String> over = sweep(ROOT, plan("tenant", last, "async", true));

        assertThat(over.statusCode()).as(over.body()).isEqualTo(503);
        assertThat(json(over).path("reason").asText()).isEqualTo("sweep_jobs_busy");
        assertThat(phases(last)).as("the refused start closed its own intent row").containsExactly("started", "failed");
        release.countDown();
    }

    @Test
    void aRootStartThatJoinsARunningJobClosesItsOwnIntentRow() throws Exception {
        String t = "as-rjoin";
        tenantWithRows(t, 1);
        slowCount();

        JsonNode first = json(sweep(ROOT, plan("tenant", t, "async", true)));
        JsonNode joined = json(sweep(ROOT, plan("tenant", t, "async", true)));

        assertThat(joined.path("job_id").asText()).isEqualTo(first.path("job_id").asText());
        assertThat(phases(t)).as("the joiner's intent is closed on the spot; the job's outcome is still to come")
            .containsExactly("started", "started", "finished");
        release.countDown();
        pollUntilEnded(ROOT, first.path("job_id").asText());
        assertThat(phases(t)).containsExactly("started", "started", "finished", "finished");
    }

    @Test
    void theAcceptMapAndTheBatchSizeReachTheJob() throws Exception {
        String token = tenantWithRows("as-plumb", 2);

        JsonNode body = finishedJob(token, plan("async", true, "accept", accept("keyless_notes", 4), "batch_size", 7))
            .path("body");

        assertThat(body.path("accepted").path("keyless_notes").asLong()).isEqualTo(4);
        assertThat(body.path("batch_size").asInt()).isEqualTo(7);
        assertThat(body.has("resume")).as("a dry run hands out no token: an apply counts for itself").isFalse();
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
