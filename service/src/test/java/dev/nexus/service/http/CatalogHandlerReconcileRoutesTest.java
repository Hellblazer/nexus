// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-193 Part C (nexus-u5m5g.3): the reconcile stage, commit and poll routes and {@code resolve_or_register}.
 *
 * <p>Runs the handler directly on PostgreSQL as the NOBYPASSRLS service role, with a real {@link EngineJobs}
 * registry. A job that must be seen running is started on the registry by the test (a latch holds it), under the
 * scope and fingerprint the commit route uses (the tenant, the owner, the run id), so the same-run, 409 and 503
 * answers are the registry's own. The failure cases drive the real commit: a run never staged, a poison row, a held
 * row lock past a short statement timeout, a job stopped before its commit statement.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerReconcileRoutesTest {

    private static final String SVC_ROLE = "svc_rc193_routes";
    private static final String SVC_PASS = "svc_rc193_routes_pass";
    private static final String OWNER = "1.1";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    EngineJobs<Void> jobs;
    CatalogHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;
    int tenantSeq = 0;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        jobs = EngineJobs.reconcile(System::currentTimeMillis);
        handler = new CatalogHandler(repo, null, jobs);
    }

    @AfterAll
    void stopAll() {
        if (jobs != null) jobs.shutdown();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String tenantWithOwner() {
        String t = "rc193http-" + (++tenantSeq);
        repo.upsertOwner(t, Map.of("tumbler_prefix", OWNER, "name", "own-" + OWNER, "owner_type", "repo",
            "repo_hash", "h-" + t, "repo_root", "/r"));
        return t;
    }

    // ── landing rows over the wire ──────────────────────────────────────

    private static Map<String, Object> row(int seq, String kind, String path) {
        var m = new LinkedHashMap<String, Object>();
        m.put("seq", seq);
        m.put("kind", kind);
        m.put("file_path", path);
        m.put("content_type", "code");
        m.put("title", path);
        m.put("physical_collection", "code__seed");
        m.put("head_hash", "H1");
        m.put("source_mtime", 1.0);
        m.put("content_hash", "");
        m.put("base_metadata", Map.of());
        m.put("stamp", Map.of());
        m.put("sensitive", false);
        return m;
    }

    private static Map<String, Object> present(int seq, String path) {
        return row(seq, "present", path);
    }

    private static List<Map<String, Object>> manyRows(int n) {
        var rows = new ArrayList<Map<String, Object>>();
        for (int i = 0; i < n; i++) rows.add(present(i, "src/f" + i + ".py"));
        return rows;
    }

    private static String stageBody(String owner, String run, String walk, int expected, List<Map<String, Object>> rows)
            throws Exception {
        var m = new LinkedHashMap<String, Object>();
        m.put("owner", owner);
        m.put("run_id", run);
        m.put("walk", walk);
        m.put("expected_rows", expected);
        m.put("rows", rows);
        return MAPPER.writeValueAsString(m);
    }

    private Resp stage(String t, String run, String walk, int expected, List<Map<String, Object>> rows) throws Exception {
        return call(t, "POST", "/v1/catalog/reconcile/stage", stageBody(OWNER, run, walk, expected, rows));
    }

    private Resp commit(CatalogHandler h, String t, String owner, String run) throws Exception {
        return call(h, t, "POST", "/v1/catalog/reconcile/commit",
            "{\"owner\":\"" + owner + "\",\"run_id\":\"" + run + "\"}");
    }

    private Resp commit(String t, String run) throws Exception {
        return commit(handler, t, OWNER, run);
    }

    private Resp poll(String t, String jobId) throws Exception {
        return call(t, "GET", "/v1/catalog/reconcile/" + jobId, "");
    }

    /** Polls until the job leaves {@code running}; the last poll answer. */
    private JsonNode awaitEnd(CatalogHandler h, String t, String jobId) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(90);
        while (true) {
            Resp r = call(h, t, "GET", "/v1/catalog/reconcile/" + jobId, "");
            assertThat(r.status).as(r.body).isEqualTo(200);
            JsonNode j = r.json();
            if (!"running".equals(j.get("status").asText())) return j;
            assertThat(System.nanoTime()).as("job still running: " + r.body).isLessThan(deadline);
            Thread.sleep(25);
        }
    }

    private JsonNode awaitEnd(String t, String jobId) throws Exception {
        return awaitEnd(handler, t, jobId);
    }

    /** Starts a job on the registry that holds its scope until the returned latch opens. */
    private EngineJobs.Started<Void> hold(EngineJobs<Void> registry, String t, String owner, String fingerprint,
                                          CountDownLatch release) {
        return registry.start(t, owner, fingerprint, (job, stage) -> {
            release.await(60, TimeUnit.SECONDS);
            return new EngineJobs.Outcome(200, Map.of());
        });
    }

    // ── stage ───────────────────────────────────────────────────────────

    @Test
    void stage_takesAPage_andCountsIt() throws Exception {
        String t = tenantWithOwner();
        Resp r = stage(t, "s1", "complete", 2, List.of(present(0, "a.py"), present(1, "b.py")));
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("staged").asInt()).isEqualTo(2);
        assertThat(stagedRows(t, "s1")).isEqualTo(2);
    }

    @Test
    void stage_capsAPageAt1000Rows() throws Exception {
        String t = tenantWithOwner();
        Resp over = stage(t, "s-over", "complete", 1001, manyRows(1001));
        assertThat(over.status).as(over.body).isEqualTo(400);
        assertThat(over.json().get("error").asText()).contains("1000");
        assertThat(stagedRows(t, "s-over")).as("a refused page stages nothing").isZero();

        Resp full = stage(t, "s-full", "complete", 1000, manyRows(1000));
        assertThat(full.status).as(full.body).isEqualTo(200);
        assertThat(full.json().get("staged").asInt()).isEqualTo(1000);
        assertThat(stagedRows(t, "s-full")).isEqualTo(1000);
    }

    @Test
    void stage_laterPage_mustMatchTheHeadersWalkAndExpectedRows() throws Exception {
        String t = tenantWithOwner();
        assertThat(stage(t, "s2", "complete", 3, List.of(present(0, "a.py"))).status).isEqualTo(200);

        Resp walk = stage(t, "s2", "partial", 3, List.of(present(1, "b.py")));
        assertThat(walk.status).as(walk.body).isEqualTo(400);
        assertThat(walk.json().get("error").asText()).contains("walk");

        Resp expected = stage(t, "s2", "complete", 4, List.of(present(1, "b.py")));
        assertThat(expected.status).as(expected.body).isEqualTo(400);
        assertThat(expected.json().get("error").asText()).contains("expected_rows");

        Resp ok = stage(t, "s2", "complete", 3, List.of(present(1, "b.py")));
        assertThat(ok.status).as(ok.body).isEqualTo(200);
        assertThat(stagedRows(t, "s2")).as("the two refused pages staged nothing").isEqualTo(2);
    }

    @Test
    void stage_aDuplicateFilePath_is400NamingIt_inOnePageAndAcrossPages() throws Exception {
        String t = tenantWithOwner();
        Resp samePage = stage(t, "s3", "complete", 2, List.of(present(0, "dup.py"), present(1, "dup.py")));
        assertThat(samePage.status).as(samePage.body).isEqualTo(400);
        assertThat(samePage.json().get("error").asText()).contains("dup.py");

        assertThat(stage(t, "s4", "complete", 2, List.of(present(0, "x.py"))).status).isEqualTo(200);
        Resp acrossPages = stage(t, "s4", "complete", 2, List.of(present(1, "x.py")));
        assertThat(acrossPages.status).as(acrossPages.body).isEqualTo(400);
        assertThat(acrossPages.json().get("error").asText()).contains("x.py");
    }

    @Test
    void stage_reapsStaleRunsFirst() throws Exception {
        String t = tenantWithOwner();
        assertThat(stage(t, "old", "complete", 1, List.of(present(0, "a.py"))).status).isEqualTo(200);
        tenantScope.withTenant(t, ctx -> ctx.update(CATALOG_RECONCILE_RUNS)
            .set(CATALOG_RECONCILE_RUNS.CREATED_AT, java.time.OffsetDateTime.now().minusHours(48))
            .where(CATALOG_RECONCILE_RUNS.TENANT_ID.eq(t).and(CATALOG_RECONCILE_RUNS.RUN_ID.eq("old"))).execute());

        assertThat(stage(t, "new", "complete", 1, List.of(present(0, "b.py"))).status).isEqualTo(200);

        assertThat(stagedRows(t, "old")).as("the stale run's rows are gone").isZero();
        assertThat(runCount(t, "old")).as("and its header").isZero();
        assertThat(stagedRows(t, "new")).isEqualTo(1);
    }

    @Test
    void stage_refusesABodyThatIsNotARunPage_andAGet() throws Exception {
        String t = tenantWithOwner();
        for (String body : List.of(
                "{\"run_id\":\"r\",\"walk\":\"complete\",\"expected_rows\":0,\"rows\":[]}",
                "{\"owner\":\"1.1\",\"walk\":\"complete\",\"expected_rows\":0,\"rows\":[]}",
                "{\"owner\":\"1.1\",\"run_id\":\"r\",\"walk\":\"sometimes\",\"expected_rows\":0,\"rows\":[]}",
                "{\"owner\":\"1.1\",\"run_id\":\"r\",\"walk\":\"complete\",\"rows\":[]}",
                "{\"owner\":\"1.1\",\"run_id\":\"r\",\"walk\":\"complete\",\"expected_rows\":0}",
                "{\"owner\":\"1.1\",\"run_id\":\"r\",\"walk\":\"complete\",\"expected_rows\":1,\"rows\":[\"a.py\"]}")) {
            Resp r = call(t, "POST", "/v1/catalog/reconcile/stage", body);
            assertThat(r.status).as(body + " -> " + r.body).isEqualTo(400);
        }
        assertThat(call(t, "GET", "/v1/catalog/reconcile/stage", "").status).isEqualTo(405);
    }

    // ── commit ──────────────────────────────────────────────────────────

    @Test
    void commit_is202WithAJobId_andThePollEndsSucceededWithTheOutcome() throws Exception {
        String t = tenantWithOwner();
        assertThat(stage(t, "c1", "complete", 2, List.of(present(0, "a.py"), present(1, "b.py"))).status).isEqualTo(200);

        Resp r = commit(t, "c1");
        assertThat(r.status).as(r.body).isEqualTo(202);
        assertThat(r.headers.getFirst("Cache-Control")).isEqualTo("no-store");
        String jobId = r.json().get("job_id").asText();
        assertThat(jobId).isNotBlank();

        JsonNode end = awaitEnd(t, jobId);
        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("succeeded");
        JsonNode rows = end.get("outcome").get("rows");
        assertThat(rows).hasSize(2);
        assertThat(rows.get(0).get("action").asText()).isEqualTo("created");
        assertThat(rows.get(0).get("tumbler").asText()).startsWith(OWNER + ".");
        assertThat(liveDocs(t)).as("the commit wrote the documents").isEqualTo(2);
        assertThat(stagedRows(t, "c1")).as("and cleared the landing rows").isZero();
    }

    @Test
    void commit_aResubmitAfterTheJobEnded_getsTheStoredOutcomeAndWritesNothing() throws Exception {
        String t = tenantWithOwner();
        assertThat(stage(t, "c2", "complete", 1, List.of(present(0, "a.py"))).status).isEqualTo(200);
        JsonNode first = awaitEnd(t, commit(t, "c2").json().get("job_id").asText());
        assertThat(first.get("status").asText()).isEqualTo("succeeded");

        Resp again = commit(t, "c2");
        assertThat(again.status).as(again.body).isEqualTo(202);
        JsonNode second = awaitEnd(t, again.json().get("job_id").asText());
        assertThat(second.get("status").asText()).isEqualTo("succeeded");
        assertThat(second.get("outcome")).isEqualTo(first.get("outcome"));
        assertThat(liveDocs(t)).isEqualTo(1);
    }

    @Test
    void commit_theSameRunWhileItsCommitRuns_getsTheRunningJob() throws Exception {
        String t = tenantWithOwner();
        var release = new CountDownLatch(1);
        try {
            var running = hold(jobs, t, OWNER, "run-same", release);
            Resp r = commit(t, "run-same");
            assertThat(r.status).as(r.body).isEqualTo(202);
            assertThat(r.json().get("job_id").asText()).isEqualTo(running.job().id());
        } finally {
            release.countDown();
        }
    }

    @Test
    void commit_anotherRunOfTheSameOwner_is409NamingTheRunningJob() throws Exception {
        String t = tenantWithOwner();
        var release = new CountDownLatch(1);
        try {
            var running = hold(jobs, t, OWNER, "run-first", release);
            Resp r = commit(t, "run-second");
            assertThat(r.status).as(r.body).isEqualTo(409);
            assertThat(r.json().get("job_id").asText()).isEqualTo(running.job().id());
            assertThat(r.json().get("error").asText()).contains(running.job().id());
        } finally {
            release.countDown();
        }
    }

    @Test
    void commit_anotherOwnersRun_doesNotWaitOnTheFirst() throws Exception {
        String t = tenantWithOwner();
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.2", "name", "own-1.2", "owner_type", "repo",
            "repo_hash", "h2-" + t, "repo_root", "/r2"));
        var release = new CountDownLatch(1);
        try {
            hold(jobs, t, "1.2", "run-other-owner", release);
            assertThat(stage(t, "c3", "complete", 1, List.of(present(0, "a.py"))).status).isEqualTo(200);
            Resp r = commit(t, "c3");
            assertThat(r.status).as(r.body).isEqualTo(202);
        } finally {
            release.countDown();
        }
    }

    @Test
    void commit_whenTheRegistryIsFull_is503() throws Exception {
        var tiny = new EngineJobs<Void>("rc193-busy-", System::currentTimeMillis,
            new EngineJobs.Bounds(1, 1, 8, Duration.ofMinutes(1), Duration.ofMinutes(1)), EngineJobs.defaultFailure());
        var h = new CatalogHandler(repo, null, tiny);
        String t = tenantWithOwner();
        var release = new CountDownLatch(1);
        try {
            hold(tiny, t, "9.9", "run-elsewhere", release);   // takes the one worker
            hold(tiny, t, "9.8", "run-queued", release);      // takes the one queue place
            Resp r = commit(h, t, OWNER, "run-blocked");
            assertThat(r.status).as(r.body).isEqualTo(503);
            assertThat(r.json().get("error").asText()).isNotBlank();
        } finally {
            release.countDown();
            tiny.shutdown();
        }
    }

    @Test
    void commit_refusesABodyWithoutOwnerOrRunId_andAGet() throws Exception {
        String t = tenantWithOwner();
        assertThat(call(t, "POST", "/v1/catalog/reconcile/commit", "{\"run_id\":\"r\"}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/reconcile/commit", "{\"owner\":\"1.1\"}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/reconcile/commit", "{\"owner\":\"1.1\",\"run_id\":\"\"}").status).isEqualTo(400);
        assertThat(call(t, "GET", "/v1/catalog/reconcile/commit", "").status).isEqualTo(405);
    }

    @Test
    void commit_withoutARegistry_is503() throws Exception {
        var bare = new CatalogHandler(repo);
        String t = tenantWithOwner();
        assertThat(commit(bare, t, OWNER, "r").status).isEqualTo(503);
    }

    // ── poll ────────────────────────────────────────────────────────────

    @Test
    void poll_aRunningJob_saysRunning() throws Exception {
        String t = tenantWithOwner();
        var release = new CountDownLatch(1);
        try {
            var running = hold(jobs, t, OWNER, "run-poll", release);
            Resp r = poll(t, running.job().id());
            assertThat(r.status).as(r.body).isEqualTo(200);
            assertThat(r.json().get("status").asText()).isEqualTo("running");
            assertThat(r.json().get("job_id").asText()).isEqualTo(running.job().id());
            assertThat(r.headers.getFirst("Cache-Control")).isEqualTo("no-store");
        } finally {
            release.countDown();
        }
    }

    @Test
    void poll_aRunNeverStaged_endsFailedNamingWhy() throws Exception {
        String t = tenantWithOwner();
        JsonNode end = awaitEnd(t, commit(t, "never-staged").json().get("job_id").asText());
        assertThat(end.get("status").asText()).isEqualTo("failed");
        assertThat(end.get("reason").asText()).isEqualTo("run_not_found");
        assertThat(end.get("error").asText()).contains("never-staged");
    }

    @Test
    void poll_aShortStage_endsFailedIncompleteStage_andWritesNothing() throws Exception {
        String t = tenantWithOwner();
        assertThat(stage(t, "short", "complete", 3, List.of(present(0, "a.py"))).status).isEqualTo(200);
        JsonNode end = awaitEnd(t, commit(t, "short").json().get("job_id").asText());
        assertThat(end.get("status").asText()).isEqualTo("failed");
        assertThat(end.get("reason").asText()).isEqualTo("incomplete_stage");
        assertThat(liveDocs(t)).isZero();
    }

    @Test
    void poll_aPoisonRow_endsFailedNamingItsFilePath_andWritesNothing() throws Exception {
        String t = tenantWithOwner();
        poisonConstraint(true);
        try {
            var bad = present(1, "poison.py");
            bad.put("head_hash", "BAD");
            assertThat(stage(t, "poison", "complete", 3, List.of(present(0, "good1.py"), bad, present(2, "good2.py")))
                .status).isEqualTo(200);
            JsonNode end = awaitEnd(t, commit(t, "poison").json().get("job_id").asText());
            assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("failed");
            assertThat(end.get("error").asText()).contains("poison.py")
                .contains("fix or remove that file, then re-run nx index repo");
            assertThat(liveDocs(t)).as("all or nothing").isZero();
            assertThat(stagedRows(t, "poison")).as("the run stays staged").isEqualTo(3);
        } finally {
            poisonConstraint(false);
        }
    }

    @Test
    void poll_aJobStoppedBeforeItsCommitStatement_endsFailed_andTheRunStaysStaged() throws Exception {
        var stopping = EngineJobs.reconcile(System::currentTimeMillis);
        stopping.maxRuntimeForTests(Duration.ZERO);   // every job is past its cap the moment a worker takes it
        var h = new CatalogHandler(repo, null, stopping);
        try {
            String t = tenantWithOwner();
            assertThat(stage(t, "stop", "complete", 1, List.of(present(0, "a.py"))).status).isEqualTo(200);
            JsonNode end = awaitEnd(h, t, commit(h, t, OWNER, "stop").json().get("job_id").asText());
            assertThat(end.get("status").asText()).isEqualTo("failed");
            assertThat(end.get("reason").asText()).isEqualTo("job_stopped");
            assertThat(end.get("error").asText()).contains("wall_clock_cap");
            assertThat(liveDocs(t)).isZero();
            assertThat(runState(t, "stop")).isEqualTo("staged");
        } finally {
            stopping.shutdown();
        }
    }

    @Test
    void poll_aCommitHeldPastItsStatementTimeout_endsFailedCommitTimeout_andWritesNothing() throws Exception {
        var h = new CatalogHandler(repo, null, EngineJobs.reconcile(System::currentTimeMillis), Duration.ofSeconds(2));
        String t = tenantWithOwner();
        String doc = repo.registerDocument(t, OWNER, Map.of("title", "held.py", "file_path", "held.py",
            "content_type", "code", "physical_collection", "code__seed", "head_hash", "H0"));
        assertThat(stage(t, "slow", "complete", 1, List.of(present(0, "held.py"))).status).isEqualTo(200);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            // hold the row the commit's update needs; the superuser connection is not subject to row-level security
            DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc))).forUpdate().fetch();
            JsonNode end = awaitEnd(h, t, commit(h, t, OWNER, "slow").json().get("job_id").asText());
            su.rollback();
            assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("failed");
            assertThat(end.get("reason").asText()).isEqualTo("commit_timeout");
        }
        assertThat(runState(t, "slow")).as("a cancelled commit rolled back whole").isEqualTo("staged");
    }

    @Test
    void poll_anUnknownId_is404() throws Exception {
        String t = tenantWithOwner();
        assertThat(poll(t, "no-such-job").status).isEqualTo(404);
        assertThat(poll(t, "").status).isEqualTo(404);
    }

    @Test
    void poll_anotherTenantsJob_is404_andTheOwnerStillSeesIt() throws Exception {
        String t = tenantWithOwner();
        String other = tenantWithOwner();
        var release = new CountDownLatch(1);
        try {
            var running = hold(jobs, t, OWNER, "run-theirs", release);
            Resp stranger = poll(other, running.job().id());
            assertThat(stranger.status).as(stranger.body).isEqualTo(404);
            assertThat(stranger.body).as("it says nothing of the other tenant's job").doesNotContain(t);
            assertThat(poll(t, running.job().id()).status).isEqualTo(200);
        } finally {
            release.countDown();
        }
    }

    @Test
    void poll_isGetOnly() throws Exception {
        String t = tenantWithOwner();
        assertThat(call(t, "POST", "/v1/catalog/reconcile/some-id", "{}").status).isEqualTo(405);
    }

    // ── resolve_or_register ─────────────────────────────────────────────

    private Resp resolve(String t, Map<String, Object> body) throws Exception {
        return call(t, "POST", "/v1/catalog/doc/resolve_or_register", MAPPER.writeValueAsString(body));
    }

    private static Map<String, Object> req(Object... kv) {
        var m = new LinkedHashMap<String, Object>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private String curatorPrefix(String t, String name) {
        var owners = repo.ownersByName(t, name);
        assertThat(owners).as("owner " + name).hasSize(1);
        assertThat(owners.get(0).get("owner_type")).isEqualTo("curator");
        return (String) owners.get(0).get("tumbler_prefix");
    }

    private Record docBy(String t, String tumbler) {
        return tenantScope.withTenant(t, ctx -> ctx.select().from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).fetchOne());
    }

    @Test
    void resolve_aNewSourceUri_registersUnderAFreshCuratorOwner_created() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        Resp r = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/t1", "title", "T1", "year", 2026));
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("created").asBoolean()).isTrue();
        assertThat(r.json().has("foreign_owner")).isFalse();
        String tumbler = r.json().get("tumbler").asText();
        assertThat(tumbler).startsWith(curatorPrefix(t, "knowledge") + ".");
        Record d = docBy(t, tumbler);
        assertThat(d.get(CATALOG_DOCUMENTS.TITLE)).isEqualTo("T1");
        assertThat(d.get(CATALOG_DOCUMENTS.YEAR)).isEqualTo(2026);
        assertThat(d.get(CATALOG_DOCUMENTS.SOURCE_URI)).isEqualTo("nx-note://c1/t1");
    }

    @Test
    void resolve_theSameSourceUriAgain_returnsTheSameTumbler_notCreated() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        String first = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/t2", "title", "T2"))
            .json().get("tumbler").asText();
        Resp again = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/t2", "title", "T2"));
        assertThat(again.status).as(again.body).isEqualTo(200);
        assertThat(again.json().get("tumbler").asText()).isEqualTo(first);
        assertThat(again.json().get("created").asBoolean()).isFalse();
        assertThat(docCount(t)).isEqualTo(1);
    }

    @Test
    void resolve_aFilePath_isKeyedByItsDerivedSourceUri() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        Resp first = resolve(t, req("owner_name", "standalone-docs", "file_path", "/work/a/b.md", "title", "B"));
        assertThat(first.json().get("created").asBoolean()).isTrue();
        Record d = docBy(t, first.json().get("tumbler").asText());
        assertThat(d.get(CATALOG_DOCUMENTS.SOURCE_URI)).isEqualTo("file:///work/a/b.md");
        Resp again = resolve(t, req("owner_name", "standalone-docs", "file_path", "/work/a/b.md", "title", "B"));
        assertThat(again.json().get("tumbler").asText()).isEqualTo(first.json().get("tumbler").asText());
        assertThat(again.json().get("created").asBoolean()).isFalse();
    }

    @Test
    void resolve_aMatchOnAnEmptyTitleAndYear_backfillsThem_andNeverOverwritesAnExistingOne() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        String prefix = registerCurator(t, "knowledge");
        String bare = repo.registerDocument(t, prefix, Map.of("source_uri", "nx-note://c1/bare", "title", "",
            "content_type", "knowledge", "physical_collection", "knowledge__c1"));
        assertThat(docBy(t, bare).get(CATALOG_DOCUMENTS.YEAR)).isZero();

        Resp r = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/bare", "title", "Filled", "year", 1999));
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("tumbler").asText()).isEqualTo(bare);
        assertThat(r.json().get("created").asBoolean()).isFalse();
        assertThat(docBy(t, bare).get(CATALOG_DOCUMENTS.TITLE)).isEqualTo("Filled");
        assertThat(docBy(t, bare).get(CATALOG_DOCUMENTS.YEAR)).isEqualTo(1999);

        Resp other = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/bare", "title", "Other", "year", 2001));
        assertThat(other.json().get("tumbler").asText()).isEqualTo(bare);
        assertThat(docBy(t, bare).get(CATALOG_DOCUMENTS.TITLE)).as("a curated title is kept").isEqualTo("Filled");
        assertThat(docBy(t, bare).get(CATALOG_DOCUMENTS.YEAR)).as("a curated year is kept").isEqualTo(1999);
    }

    @Test
    void resolve_aSourceUriHeldByAnotherOwner_isForeignOwner_andNothingIsAdoptedOrWritten() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        String theirs = registerCurator(t, "someone-elses");
        String held = repo.registerDocument(t, theirs, Map.of("source_uri", "nx-note://c1/held", "title", "",
            "content_type", "knowledge", "physical_collection", "knowledge__c1"));

        Resp r = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/held", "title", "Mine", "year", 2020));
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("tumbler").asText()).isEqualTo(held);
        assertThat(r.json().get("foreign_owner").asBoolean()).isTrue();
        assertThat(r.json().has("created")).isFalse();
        assertThat(docCount(t)).as("no second document").isEqualTo(1);
        assertThat(docBy(t, held).get(CATALOG_DOCUMENTS.TITLE)).as("the other owner's row is not backfilled").isEmpty();
    }

    @Test
    void resolve_titleFallback_matchesAGhostOnly_andStampsItsSourceUri() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        String prefix = registerCurator(t, "knowledge");
        String ghost = repo.registerDocument(t, prefix, Map.of("title", "Ghost Note", "content_type", "knowledge",
            "physical_collection", "knowledge__c1", "chunk_count", 0));
        String populated = repo.registerDocument(t, prefix, Map.of("title", "Populated Note", "content_type", "knowledge",
            "physical_collection", "knowledge__c1", "chunk_count", 3));

        Resp viaGhost = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/ghost",
            "title", "Ghost Note", "title_fallback", true));
        assertThat(viaGhost.status).as(viaGhost.body).isEqualTo(200);
        assertThat(viaGhost.json().get("tumbler").asText()).isEqualTo(ghost);
        assertThat(viaGhost.json().get("created").asBoolean()).isFalse();
        assertThat(docBy(t, ghost).get(CATALOG_DOCUMENTS.SOURCE_URI)).isEqualTo("nx-note://c1/ghost");

        Resp viaPopulated = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/populated",
            "title", "Populated Note", "title_fallback", true));
        assertThat(viaPopulated.json().get("created").asBoolean()).as("a populated row is never adopted by title").isTrue();
        assertThat(viaPopulated.json().get("tumbler").asText()).isNotEqualTo(populated);
        assertThat(docBy(t, populated).get(CATALOG_DOCUMENTS.SOURCE_URI)).isEmpty();
    }

    @Test
    void resolve_withoutTitleFallback_aGhostIsNotMatchedByTitle() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        String prefix = registerCurator(t, "knowledge");
        String ghost = repo.registerDocument(t, prefix, Map.of("title", "Ghost Two", "content_type", "knowledge",
            "physical_collection", "knowledge__c1", "chunk_count", 0));
        Resp r = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/ghost2", "title", "Ghost Two"));
        assertThat(r.json().get("created").asBoolean()).isTrue();
        assertThat(r.json().get("tumbler").asText()).isNotEqualTo(ghost);
    }

    @Test
    void resolve_titleFallback_neverAdoptsAnotherOwnersGhost() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        String theirs = registerCurator(t, "someone-elses");
        String ghost = repo.registerDocument(t, theirs, Map.of("title", "Ghost Three", "content_type", "knowledge",
            "physical_collection", "knowledge__c1", "chunk_count", 0));
        Resp r = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/ghost3",
            "title", "Ghost Three", "title_fallback", true));
        assertThat(r.json().get("created").asBoolean()).isTrue();
        assertThat(r.json().get("tumbler").asText()).isNotEqualTo(ghost);
    }

    @Test
    void resolve_theCuratorKeyNamesTheOwnerToo() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        Resp r = resolve(t, req("curator", "standalone-pdfs", "source_uri", "file:///p/x.pdf", "title", "X"));
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("tumbler").asText()).startsWith(curatorPrefix(t, "standalone-pdfs") + ".");
    }

    @Test
    void resolve_isKeyedByTheOwnerToo_aRepoOwnerOfTheSameNameIsNotACurator() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.1", "name", "knowledge", "owner_type", "repo",
            "repo_hash", "hh-" + t, "repo_root", "/r"));
        Resp r = resolve(t, req("owner_name", "knowledge", "source_uri", "nx-note://c1/same-name", "title", "N"));
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("tumbler").asText()).as("not under the repo owner 1.1").doesNotStartWith("1.1.");
    }

    @Test
    void resolve_refusesABodyItCannotKey_andAGet() throws Exception {
        String t = "rc193http-" + (++tenantSeq);
        for (Map<String, Object> body : List.of(
                req("source_uri", "nx-note://c/x", "title", "T"),
                req("owner_name", "", "source_uri", "nx-note://c/x", "title", "T"),
                req("owner_name", "knowledge", "title", "T"),
                req("owner_name", "knowledge", "file_path", "relative/only.md", "title", "T"),
                req("owner_name", "knowledge", "source_uri", "nx-note://c/x", "title", 5),
                req("owner_name", "knowledge", "source_uri", "nx-note://c/x", "title", "T", "year", "late"),
                req("owner_name", "knowledge", "source_uri", "nx-note://c/x", "title", "T", "title_fallback", "yes"))) {
            Resp r = resolve(t, body);
            assertThat(r.status).as(body + " -> " + r.body).isEqualTo(400);
        }
        assertThat(call(t, "GET", "/v1/catalog/doc/resolve_or_register", "").status).isEqualTo(405);
    }

    private String registerCurator(String t, String name) {
        repo.upsertOwner(t, Map.of("name", name, "owner_type", "curator"));
        return curatorPrefix(t, name);
    }

    // ── reading the database ────────────────────────────────────────────

    private int stagedRows(String t, String run) {
        return tenantScope.withTenant(t, ctx -> ctx.fetchCount(CATALOG_RECONCILE_ROWS,
            CATALOG_RECONCILE_ROWS.TENANT_ID.eq(t).and(CATALOG_RECONCILE_ROWS.RUN_ID.eq(run))));
    }

    private int runCount(String t, String run) {
        return tenantScope.withTenant(t, ctx -> ctx.fetchCount(CATALOG_RECONCILE_RUNS,
            CATALOG_RECONCILE_RUNS.TENANT_ID.eq(t).and(CATALOG_RECONCILE_RUNS.RUN_ID.eq(run))));
    }

    private String runState(String t, String run) {
        return tenantScope.withTenant(t, ctx -> ctx.select(CATALOG_RECONCILE_RUNS.STATE).from(CATALOG_RECONCILE_RUNS)
            .where(CATALOG_RECONCILE_RUNS.TENANT_ID.eq(t).and(CATALOG_RECONCILE_RUNS.RUN_ID.eq(run)))
            .fetchOne(CATALOG_RECONCILE_RUNS.STATE));
    }

    private int liveDocs(String t) {
        return tenantScope.withTenant(t, ctx -> ctx.fetchCount(CATALOG_DOCUMENTS,
            CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.startsWith(OWNER + "."))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
    }

    private int docCount(String t) {
        return tenantScope.withTenant(t, ctx -> ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(t)));
    }

    /** A CHECK that refuses exactly the row (file_path poison.py, head_hash BAD), on insert and on update alike. */
    private void poisonConstraint(boolean on) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            if (on) {
                ctx.alterTable(CATALOG_DOCUMENTS).add(DSL.constraint("rc193_routes_poison").check(
                    DSL.not(CATALOG_DOCUMENTS.FILE_PATH.eq("poison.py").and(CATALOG_DOCUMENTS.HEAD_HASH.eq("BAD"))))).execute();
            } else {
                ctx.alterTable(CATALOG_DOCUMENTS).dropConstraintIfExists("rc193_routes_poison").execute();
            }
        }
    }

    // ── the wire ────────────────────────────────────────────────────────

    private record Resp(int status, String body, Headers headers) {
        JsonNode json() throws Exception { return MAPPER.readTree(body); }
    }

    private Resp call(String tenant, String method, String path, String body) throws Exception {
        return call(handler, tenant, method, path, body);
    }

    private Resp call(CatalogHandler h, String tenant, String method, String path, String body) throws Exception {
        CapturingExchange ex = new CapturingExchange(method, URI.create(path), body);
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            h.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString(), ex.responseHeaders);
    }

    /** Minimal {@link HttpExchange} that captures the response status, headers and body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
