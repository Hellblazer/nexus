// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.http.ChunkKeysAdminHandler;
import dev.nexus.service.http.ChunkKeysSweepJobs;
import dev.nexus.service.vectors.ChunkKeyPlan;
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
        copy.remove("resume");   // only the job hands out a continuation (asserted by its own tests)
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

    private static Map<String, Object> asMap(JsonNode node) {
        return MAPPER.convertValue(node, new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() { });
    }

    private JsonNode finishedJob(String token, Map<String, Object> startBody) throws Exception {
        HttpResponse<String> started = sweep(token, startBody);
        assertThat(started.statusCode()).as(started.body()).isEqualTo(202);
        return pollUntilEnded(token, json(started).path("job_id").asText());
    }

    @Test
    void anApplyCarryingTheJobsContinuationNeverEntersTheCensusLatch() throws Exception {
        String token = tenantWithRows("as-tok", 5);
        AtomicInteger census = countingSweep();
        JsonNode ended = finishedJob(token, plan("async", true));
        JsonNode resume = ended.path("body").path("resume");
        assertThat(resume.path("sig").asText()).as("a clean dry run hands out a continuation").isNotBlank();
        assertThat(resume.path("origin").path("job_id").asText()).isEqualTo(ended.path("job_id").asText());
        int afterDryRun = census.get();
        assertThat(afterDryRun).as("the dry run itself counted").isPositive();

        HttpResponse<String> applied = sweep(token, plan("dry_run", false, "resume", asMap(resume)));

        assertThat(applied.statusCode()).as(applied.body()).isEqualTo(200);
        JsonNode body = json(applied);
        assertThat(body.path("rows_swept").asLong()).isEqualTo(5);
        assertThat(body.path("resume_ignored").asBoolean()).isFalse();
        assertThat(census.get()).as("the apply's first call did not enter the census").isEqualTo(afterDryRun);
        assertThat(retiredRowCount("as-tok", conformant("as-tok"))).isZero();
    }

    @Test
    void aDryRunThatIsBlockedHandsOutNoContinuation_andOneAcceptedUnderTheMapDoes() throws Exception {
        String tenant = "as-nob";
        String token = tenantWithRows(tenant, 2);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCatalogDocument(DSL.using(su, SQLDialect.POSTGRES), tenant, "nob.1");
        }
        countingSweep();

        JsonNode blocked = finishedJob(token, plan("async", true)).path("body");
        assertThat(blocked.path("blocking")).as("a document with no collection blocks").isNotEmpty();
        assertThat(blocked.has("resume")).as("nothing may be applied on a blocked dry run's word").isFalse();

        JsonNode accepted = finishedJob(token,
            plan("async", true, "accept", accept("homeless_documents", 1, "generated_nulls", 1))).path("body");
        assertThat(accepted.path("blocking")).isEmpty();
        assertThat(accepted.path("resume").path("origin").path("accepted").path("homeless_documents").asLong())
            .isEqualTo(1);
    }

    @Test
    void aContinuationWithAnotherAcceptMapIsRefusedAs400ResumeMismatch_andNothingIsSwept() throws Exception {
        String token = tenantWithRows("as-acc", 3);
        AtomicInteger census = countingSweep();
        JsonNode resume = finishedJob(token, plan("async", true)).path("body").path("resume");
        int before = census.get();

        HttpResponse<String> r = sweep(token, plan("dry_run", false, "resume", asMap(resume),
            "accept", accept("keyless_notes", 5)));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(400);
        assertThat(json(r).path("reason").asText()).isEqualTo("resume_mismatch");
        assertThat(census.get()).as("refused, not re-counted").isEqualTo(before);
        assertThat(retiredRowCount("as-acc", conformant("as-acc"))).isEqualTo(3);
        assertThat(sweep(token, plan("dry_run", false, "resume", asMap(resume))).statusCode())
            .as("positive control: the same accept map applies").isEqualTo(200);
    }

    @Test
    void aContinuationOfAnotherPlanIsRefused() throws Exception {
        String tenant = "as-plan";
        tenantWithRows(tenant, 2);
        ChunkKeySweep sw = new ChunkKeySweep(tenantScope, (t, c, b) -> 0L);
        Map<String, Object> p = plan();
        @SuppressWarnings("unchecked")
        ChunkKeyPlan mine = ChunkKeyPlan.of((List<String>) p.get("allowed_keys"), (List<String>) p.get("bypass_prefixes"),
            (List<String>) p.get("quarantine_implies"));
        var report = sw.dryRun(tenant, mine, Map.of(), 100, false);
        ChunkKeySweep.Resume token = sw.issueDryRunResume(tenant, "job-1", mine, report);
        List<String> fewer = new ArrayList<>(mine.allowed());
        fewer.remove(fewer.size() - 1);
        ChunkKeyPlan other = new ChunkKeyPlan(fewer, mine.rowKeys(), mine.bypassPrefixes(), mine.quarantineImplies());

        assertThat(other.fingerprint()).isNotEqualTo(mine.fingerprint());
        assertThatThrownBy(() -> sw.apply(tenant, other, Map.of(), null, 100, 5, Duration.ofSeconds(5), token))
            .isInstanceOf(ChunkKeySweep.ResumeMismatch.class);
        assertThat(retiredRowCount(tenant, conformant(tenant))).as("nothing swept").isEqualTo(2);
        assertThat(sw.apply(tenant, mine, Map.of(), null, 100, 5, Duration.ofSeconds(5), token).rowsSwept())
            .as("positive control: the plan it was made under applies").isEqualTo(2);
    }

    @Test
    void aContinuationFromBeforeAnEngineRestartIsIgnored_theNewInstanceHasAnotherKey() throws Exception {
        String tenant = "as-restart";
        tenantWithRows(tenant, 2);
        Map<String, Object> p = plan();
        @SuppressWarnings("unchecked")
        ChunkKeyPlan mine = ChunkKeyPlan.of((List<String>) p.get("allowed_keys"), (List<String>) p.get("bypass_prefixes"),
            (List<String>) p.get("quarantine_implies"));
        ChunkKeySweep before = new ChunkKeySweep(tenantScope, (t, c, b) -> 0L);
        ChunkKeySweep.Resume token = before.issueDryRunResume(tenant, "job-1", mine,
            before.dryRun(tenant, mine, Map.of(), 100, false));
        AtomicInteger census = new AtomicInteger();
        ChunkKeySweep after = new ChunkKeySweep(tenantScope, (t, c, b) -> {
            census.incrementAndGet();
            return 0L;
        });

        ChunkKeySweep.Applied applied = after.apply(tenant, mine, Map.of(), null, 100, 5, Duration.ofSeconds(5), token);

        assertThat(applied.resumeIgnored()).as("another instance, another key: the token is not its own").isTrue();
        assertThat(census.get()).as("so it counted for itself").isPositive();
        assertThat(applied.rowsSwept()).isEqualTo(2);
    }

    @Test
    void anEditedForgedOrOtherTenantsContinuationIsIgnored_andTheApplyRecountsAndChecksItself() throws Exception {
        AtomicInteger census = countingSweep();
        // Each variant gets its own tenant: a recount that sweeps the rows would leave nothing for the next to census.
        Map<String, java.util.function.UnaryOperator<ObjectNode>> variants = new LinkedHashMap<>();
        variants.put("rows inflated", t -> {
            ((ObjectNode) t.path("origin").path("rows")).put(conformant("as-forge-0"), 999_999);
            return t;
        });
        // The attack the accept map under the signature stops: raise the accept map in the token AND in the request, so
        // the two agree and the residue checks the dry run made would be skipped for a map nobody dry-ran.
        variants.put("accept map widened", t -> {
            ((ObjectNode) t.path("origin").path("accepted")).put("keyless_notes", 99);
            return t;
        });
        variants.put("precondition counts edited", t -> {
            ((ObjectNode) t.path("origin").path("preconditions")).put("keyless_notes", 7);
            return t;
        });
        variants.put("signature forged", t -> {
            t.put("sig", "00".repeat(32));
            return t;
        });
        variants.put("signature removed", t -> {
            t.remove("sig");
            return t;
        });
        int n = 0;
        for (var variant : variants.entrySet()) {
            String tn = "as-forge-" + n++;
            String tok = tenantWithRows(tn, 4);
            ObjectNode bad = variant.getValue().apply(finishedJob(tok, plan("async", true)).path("body").path("resume").deepCopy());
            int before = census.get();

            JsonNode body = json(sweep(tok, plan("dry_run", false, "max_batches", 1, "resume", asMap(bad),
                "accept", variant.getKey().equals("accept map widened") ? accept("keyless_notes", 99) : accept())));

            assertThat(body.path("resume_ignored").asBoolean()).as(variant.getKey()).isTrue();
            assertThat(census.get()).as("%s: the call counted for itself", variant.getKey()).isGreaterThan(before);
        }
        // Another tenant's continuation is not this tenant's (the tenant is under the signature).
        String owner = tenantWithRows("as-forge-own", 2);
        String other = tenantWithRows("as-forge-other", 2);
        JsonNode resume = finishedJob(owner, plan("async", true)).path("body").path("resume");
        int before = census.get();
        JsonNode crossed = json(sweep(other, plan("dry_run", false, "resume", asMap(resume))));
        assertThat(crossed.path("resume_ignored").asBoolean()).isTrue();
        assertThat(crossed.path("tenant").asText()).isEqualTo("as-forge-other");
        assertThat(census.get()).isGreaterThan(before);
    }

    @Test
    void aContinuationLapsesWithItsJob_aTenantResetAndAnApplyThatChangedRowsBothEndIt() throws Exception {
        String token = tenantWithRows("as-lapse", 3);
        AtomicInteger census = countingSweep();
        JsonNode resume = finishedJob(token, plan("async", true)).path("body").path("resume");

        service.chunkKeysJobsForTests().evictTenant("as-lapse");   // the tenant reset route's evictor
        int before = census.get();
        JsonNode lapsed = json(sweep(token, plan("dry_run", false, "max_batches", 1, "resume", asMap(resume))));
        assertThat(lapsed.path("resume_ignored").asBoolean()).as("the job is gone, so is its word").isTrue();
        assertThat(census.get()).isGreaterThan(before);

        // An apply that changed rows drops the finished dry runs of the tenant: a second token cannot be replayed.
        String t2 = tenantWithRows("as-lapse2", 3);
        JsonNode first = finishedJob(t2, plan("async", true)).path("body").path("resume");
        JsonNode second = json(sweep(t2, plan("dry_run", false, "batch_size", 1, "max_batches", 1, "resume", asMap(first))));
        assertThat(second.path("rows_swept").asLong()).as("one batch of one row: two rows are left to count").isEqualTo(1);
        int mid = census.get();
        JsonNode replay = json(sweep(t2, plan("dry_run", false, "resume", asMap(first))));
        assertThat(replay.path("resume_ignored").asBoolean()).isTrue();
        assertThat(census.get()).as("the replay counted for itself").isGreaterThan(mid);
    }

    @Test
    void aRootApplyFromTheJobsContinuationRecordsThatItDidNotRecountInItsOutcomeRow() throws Exception {
        String t = "as-rootapply";
        tenantWithRows(t, 2);
        countingSweep();
        JsonNode resume = finishedJob(ROOT, plan("tenant", t, "async", true)).path("body").path("resume");

        HttpResponse<String> r = sweep(ROOT, plan("tenant", t, "dry_run", false, "resume", asMap(resume)));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        try (Connection su = pg.createConnection("")) {
            var outcome = DSL.using(su, SQLDialect.POSTGRES).select(GC_AUDIT.DETAILS).from(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(t)).and(GC_AUDIT.OPERATION.eq("retire_keys_sweep"))
                .orderBy(GC_AUDIT.ID.desc()).limit(1).fetchOne().get(GC_AUDIT.DETAILS).data();
            JsonNode counts = MAPPER.readTree(outcome).path("counts");
            assertThat(counts.path("from_dry_run_job").asText()).isNotBlank();
            assertThat(counts.has("preconditions_not_recounted")).isTrue();
        }
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

    // ---- a rerun is handed the finished job ---------------------------------------------------------------------

    @Test
    void aRerunInsideRetentionIsHandedTheFinishedJob_nothingIsRecounted_andAnApplyEndsThatMemory() throws Exception {
        String token = tenantWithRows("as-rerun", 3);
        AtomicInteger census = countingSweep();
        JsonNode first = finishedJob(token, plan("async", true));
        int counted = census.get();

        HttpResponse<String> again = sweep(token, plan("async", true));
        assertThat(again.statusCode()).isEqualTo(202);
        assertThat(json(again).path("job_id").asText()).isEqualTo(first.path("job_id").asText());
        assertThat(json(again).path("state").asText()).isEqualTo("done");
        assertThat(census.get()).as("no recount").isEqualTo(counted);

        // A different request is a different job.
        JsonNode other = finishedJob(token, plan("async", true, "batch_size", 7));
        assertThat(other.path("job_id").asText()).isNotEqualTo(first.path("job_id").asText());

        assertThat(sweep(token, plan("dry_run", false)).statusCode()).isEqualTo(200);   // rows change
        JsonNode after = finishedJob(token, plan("async", true));
        assertThat(after.path("job_id").asText()).as("the old counts describe a tenant that is gone")
            .isNotEqualTo(first.path("job_id").asText());
        assertThat(after.path("body").path("totals").path("sweepable_rows").asLong()).isZero();
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
        assertThat(body.path("resume").path("origin").path("accepted").path("keyless_notes").asLong()).isEqualTo(4);
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
