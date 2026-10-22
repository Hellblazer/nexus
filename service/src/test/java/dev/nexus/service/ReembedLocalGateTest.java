// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the re-embed runner is WIRED into {@code NexusService}, and only for
 * the local engine. {@code NX_ENGINE_LOCAL} is read through the constructor's env seam as the value
 * {@code 1} after trimming (as {@code NX_TEST_TENANT_RESET_ROUTE} is), so the off case is testable on port 0 without touching the
 * process environment; the supervisor test ({@code test_storage_service_daemon.py}) proves the daemon sets it.
 *
 * <p>The wiring half follows {@code NexusServiceReaperWiringTest}: a recording executor sees the schedule call, and
 * the test runs THE {@link Runnable} the executor was handed against a real Postgres, so a schedule turned into a
 * no-op, or a task body that differs from the runner's tick, fails here. The control routes on
 * {@code /v1/vectors/reembed} are registered always and answer 503 naming local mode when there is no runner (the
 * variable unset, or no embedder), never a 404; the last test group starts a real server on port 0 and pins that.
 *
 * <p>Run: {@code -Dtest=ReembedLocalGateTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedLocalGateTest {

    private static final String SVC_ROLE = "svc_reembed_gate_test";
    private static final String SVC_PASS = "svc_reembed_gate_test_pass";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINI = "minilm-l6-v2-384";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope scope;
    private EmbedderRouter router;
    private final List<NexusService> started = new ArrayList<>();

    /** An executor that records what is scheduled and executed on it and runs none of it. */
    private static final class RecordingExecutor extends ScheduledThreadPoolExecutor {
        record Registration(Runnable task, long initialDelay, long delay, TimeUnit unit) { }

        final List<Registration> fixedDelay = new CopyOnWriteArrayList<>();
        final List<Runnable> executed = new CopyOnWriteArrayList<>();

        RecordingExecutor() {
            super(1, r -> {
                Thread t = new Thread(r, "recording-reembed-executor");
                t.setDaemon(true);
                return t;
            });
        }

        @Override
        public ScheduledFuture<?> scheduleWithFixedDelay(Runnable command, long initialDelay, long delay,
                                                         TimeUnit unit) {
            fixedDelay.add(new Registration(command, initialDelay, delay, unit));
            return null;
        }

        @Override
        public void execute(Runnable command) {
            executed.add(command);
        }
    }

    static final class Fake implements Embedder {
        final String token;
        final int dim;

        Fake(String token, int dim) {
            this.token = token;
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                Random r = new Random(t.hashCode());
                float[] v = new float[dim];
                double norm = 0;
                for (int i = 0; i < dim; i++) {
                    v[i] = (float) r.nextGaussian();
                    norm += v[i] * v[i];
                }
                float n = (float) Math.sqrt(norm);
                for (int i = 0; i < dim; i++) v[i] /= n;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), texts.size());
        }

        @Override
        public String modelToken() {
            return token;
        }
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setConnectionTimeout(10_000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        router = new EmbedderRouter(new Fake(MINI, 384), "document");
    }

    @AfterAll
    void stopAll() {
        for (NexusService s : started) {
            try {
                s.stop();
            } catch (Exception ignored) {
                // never started
            }
        }
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private NexusService service(EmbedderRouter withRouter, Function<String, String> env, RecordingExecutor executor)
            throws Exception {
        NexusService s = new NexusService(0, "reembed-gate-token", svcDs, withRouter, null, null, null, null, null,
            env, () -> false, executor, null);
        started.add(s);
        return s;
    }

    private static Function<String, String> envWith(String value) {
        return name -> name.equals("NX_ENGINE_LOCAL") ? value : null;
    }

    // ── the variable is the gate ─────────────────────────────────────────────

    @Test
    void withoutTheVariableExactlyOne_noRunnerIsBuilt_nothingIsScheduled_andAKickDoesNothing() throws Exception {
        for (String value : new String[] {null, "", "0", "true", "yes", "11", "one"}) {
            var executor = new RecordingExecutor();
            NexusService s = service(router, envWith(value), executor);

            assertThat(s.reembedRunner()).as("NX_ENGINE_LOCAL=" + value).isNull();
            assertThat(s.reembedScheduledTask()).isNull();
            assertThat(executor.fixedDelay).isEmpty();
            s.kickReembed();
            assertThat(executor.executed).as("a kick without a runner runs nothing").isEmpty();
        }
    }

    @Test
    void withTheVariableExactlyOne_theRunnerIsScheduledOnItsOwnExecutor_withTheFixedDelay() throws Exception {
        var executor = new RecordingExecutor();
        NexusService s = service(router, envWith("1"), executor);

        assertThat(s.reembedRunner()).isNotNull();
        assertThat(executor.fixedDelay).hasSize(1);
        var registration = executor.fixedDelay.get(0);
        assertThat(registration.task()).as("the very Runnable the service exposes").isSameAs(s.reembedScheduledTask());
        assertThat(registration.unit()).isEqualTo(TimeUnit.SECONDS);
        assertThat(registration.initialDelay()).isEqualTo(5);
        assertThat(registration.delay()).isEqualTo(30);

        s.kickReembed();
        assertThat(executor.executed).containsExactly(s.reembedScheduledTask());
    }

    @Test
    void theVariableAloneIsNotEnough_aServiceWithNoEmbedderHasNothingToMoveDocumentsWith() throws Exception {
        var executor = new RecordingExecutor();
        NexusService s = service(null, envWith("1"), executor);

        assertThat(s.reembedRunner()).isNull();
        assertThat(executor.fixedDelay).isEmpty();
    }

    @Test
    void theEnvironmentTestIsTheExactString() {
        assertThat(NexusService.localEngineEnabled(envWith("1"))).isTrue();
        assertThat(NexusService.localEngineEnabled(envWith(" 1 "))).as("trimmed, as the tenant reset flag is").isTrue();
        for (String v : new String[] {null, "", "0", "true", "TRUE", "yes", "11"}) {
            assertThat(NexusService.localEngineEnabled(envWith(v))).as("value " + v).isFalse();
        }
    }

    // ── the control routes are refused, not absent ───────────────────────────

    private static final String GATE_TOKEN = "reembed-gate-routes-token";
    private static final String BASE = "/v1/vectors/reembed";

    private HttpResponse<String> call(NexusService s, String method, String path, String body) throws Exception {
        var builder = TestHttp.request("http://127.0.0.1:" + s.getPort() + BASE + path)
            .header("Authorization", "Bearer " + GATE_TOKEN).header("Content-Type", "application/json");
        builder = "GET".equals(method) ? builder.GET() : builder.POST(HttpRequest.BodyPublishers.ofString(body));
        return TestHttp.client().send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    private void seedGateToken() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), GATE_TOKEN, TENANT,
                "reembed-gate-routes");
        }
    }

    @Test
    void withoutTheVariableEveryReembedRouteAnswers503NamingLocalMode_neverA404() throws Exception {
        seedGateToken();
        UUID someJob = UUID.randomUUID();
        for (String value : new String[] {null, "", "0", "true"}) {
            NexusService s = service(router, envWith(value), new RecordingExecutor());
            s.start();
            String[][] requests = {
                {"POST", "/jobs", "{\"source_collection\":\"x\"}"}, {"POST", "/jobs", ""}, {"GET", "/jobs", ""},
                {"POST", "/jobs/" + someJob + "/pause", ""}, {"POST", "/jobs/" + someJob + "/resume", ""},
                {"POST", "/jobs/" + someJob + "/abort", ""}, {"POST", "/nope", ""}};
            for (String[] req : requests) {
                var r = call(s, req[0], req[1], req[2]);
                assertThat(r.statusCode()).as("NX_ENGINE_LOCAL=" + value + " " + req[0] + " " + req[1] + " " + r.body())
                    .isEqualTo(503);
                assertThat(r.body()).contains("local engine").contains("NX_ENGINE_LOCAL=1");
            }
        }
    }

    @Test
    void theVariableWithoutAnEmbedderIsRefusedToo_andTheMessageNamesTheEmbedder() throws Exception {
        seedGateToken();
        NexusService s = service(null, envWith("1"), new RecordingExecutor());
        s.start();

        var r = call(s, "POST", "/jobs", "{\"source_collection\":\"x\"}");

        assertThat(r.statusCode()).isEqualTo(503);
        assertThat(r.body()).contains("embedder");
    }

    @Test
    void withTheVariableTheRoutesAreReachedAndTheOtherVectorRoutesAreNotShadowed() throws Exception {
        seedGateToken();
        NexusService s = service(router, envWith("1"), new RecordingExecutor());
        s.start();

        var reached = call(s, "POST", "/jobs", "{}");
        assertThat(reached.statusCode()).as("the handler's own validation, not a 503 and not the vectors handler")
            .isEqualTo(400);
        assertThat(reached.body()).contains("source_collection");
        var missingJob = call(s, "POST", "/jobs/" + UUID.randomUUID() + "/abort", "");
        assertThat(missingJob.statusCode()).isEqualTo(404);

        var other = TestHttp.client().send(TestHttp.request("http://127.0.0.1:" + s.getPort() + "/v1/vectors/stats")
            .header("Authorization", "Bearer " + GATE_TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(other.body()).as("/v1/vectors/stats is still the vectors handler's").doesNotContain("re-embed");
    }

    // ── the scheduled task is the runner's tick ──────────────────────────────

    @Test
    void theScheduledTaskItselfRunsAJobToCompletion() throws Exception {
        String owner = "gate";
        String src = "knowledge__" + owner + "__" + BGE + "__v1";
        String tgt = "knowledge__" + owner + "__" + MINI + "__v1";
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, src);
            PgContainerHelper.insertCollection(ctx, TENANT, tgt);
            ctx.deleteFrom(EMBEDDING_PROFILE).where(EMBEDDING_PROFILE.TENANT_ID.eq(TENANT)).execute();
            ctx.insertInto(EMBEDDING_PROFILE, EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE,
                EMBEDDING_PROFILE.EMBEDDING_MODEL, EMBEDDING_PROFILE.DIMENSION)
                .values(TENANT, "knowledge", MINI, 384).execute();
        }
        var catalog = new CatalogRepository(scope);
        var writer = new CombinedWriteService(scope, catalog, new EmbedderRouter(new Fake(BGE, 768), "document"));
        List<String> docs = new ArrayList<>();
        for (int d = 0; d < 3; d++) {
            String docId = "gate.d" + d;
            docs.add(docId);
            catalog.upsertDocument(TENANT, Map.of("tumbler", docId, "title", "t" + d, "content_type", "knowledge",
                "corpus", "knowledge", "physical_collection", src, "chunk_count", 0));
            List<Map<String, Object>> chunks = new ArrayList<>();
            List<Map<String, Object>> rows = new ArrayList<>();
            for (int k = 0; k < 2; k++) {
                String text = "gate doc " + d + " chunk " + k;
                chunks.add(Map.of("chash", Chash.ofText(text).toHex(), "text", text, "metadata", Map.of()));
                rows.add(Map.of("position", k, "chash", Chash.ofText(text).toHex(), "chunk_index", k));
            }
            writer.writeManyCombined(TENANT, src, chunks, List.of(Map.of("doc_id", docId, "rows", rows)), null,
                false, false);
        }
        var jobs = new ReembedJobRepository(scope);
        Job job = jobs.start(TENANT, src, tgt, null, false, false);
        var executor = new RecordingExecutor();
        NexusService s = service(router, envWith("1"), executor);
        assertThat(s.reaperTenantsForTests()).as("the default tenant is always visited").contains(TENANT);

        // THE object the executor was handed. A schedule turned into a no-op leaves the job planned.
        executor.fixedDelay.get(0).task().run();

        Job done = jobs.get(TENANT, job.jobId()).orElseThrow();
        assertThat(done.state()).isEqualTo(ReembedJobRepository.DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(done.documentsMoved()).isEqualTo(3);
        assertThat(catalog.getCollection(TENANT, src)).isNull();
        for (String d : docs) {
            try (Connection su = pg.createConnection("")) {
                String physical = DSL.using(su, SQLDialect.POSTGRES)
                    .select(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .from(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS)
                    .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT))
                    .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TUMBLER.eq(d))
                    .fetchOne(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.PHYSICAL_COLLECTION);
                assertThat(physical).isEqualTo(tgt);
            }
        }
    }
}
