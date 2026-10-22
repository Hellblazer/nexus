// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.reembed.ReembedStateMachine;
import dev.nexus.service.reembed.ReembedStateMachine.Event;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
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
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the re-embed control routes on {@code /v1/vectors/reembed}, through a real
 * {@code NexusService} on port 0 over a real Postgres (the service role, so RLS applies), with the local-engine
 * variable on. The runner's executor is a recording one that runs nothing, so a job stays in the state the test puts
 * it in and a wake-up is a counted call; the transitions that a runner would make are made through the repository,
 * as a runner does. {@code ReembedLocalGateTest} covers the routes answering 503 without the variable.
 *
 * <p>Every test builds its own tenant (a token, a source and target pair, the profile naming the target's model), so
 * tests share a database without sharing a source lock or a job.
 *
 * <p>Run: {@code -Dtest=ReembedRoutesTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedRoutesTest {

    private static final String SVC_ROLE = "svc_reembed_routes_test";
    private static final String SVC_PASS = "svc_reembed_routes_test_pass";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINI = "minilm-l6-v2-384";
    private static final String TYPE = "knowledge";
    private static final String BASE = "/v1/vectors/reembed";

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() { };

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope scope;
    private CatalogRepository catalog;
    private ReembedJobRepository jobs;
    private NexusService service;
    private RecordingExecutor executor;
    private HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();

    /** An executor that records what is executed on it and runs none of it. */
    private static final class RecordingExecutor extends ScheduledThreadPoolExecutor {
        final List<Runnable> executed = new CopyOnWriteArrayList<>();

        RecordingExecutor() {
            super(1, r -> {
                Thread t = new Thread(r, "recording-reembed-routes-executor");
                t.setDaemon(true);
                return t;
            });
        }

        @Override
        public ScheduledFuture<?> scheduleWithFixedDelay(Runnable command, long initialDelay, long delay,
                                                         TimeUnit unit) {
            return null;
        }

        @Override
        public void execute(Runnable command) {
            executed.add(command);
        }
    }

    private static final class Fake implements Embedder {
        private final String token;
        private final int dim;

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

    /** A tenant with a token, a source (bge) and a target (minilm) sibling, and the profile naming minilm. */
    private record Tn(String tenant, String token, String owner, String src, String tgt) {
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
        catalog = new CatalogRepository(scope);
        jobs = new ReembedJobRepository(scope);
        executor = new RecordingExecutor();
        service = new NexusService(0, "reembed-routes-root", svcDs,
            new EmbedderRouter(new Fake(MINI, 384), "document"), null, null, null, null, null,
            name -> name.equals("NX_ENGINE_LOCAL") ? "1" : null, () -> false, executor, null);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── substrate helpers ────────────────────────────────────────────────────

    private <T> T su(Function<DSLContext, T> f) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return f.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private void setProfile(String tenant, String model, int dimension) {
        su(ctx -> {
            ctx.deleteFrom(EMBEDDING_PROFILE).where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
                .and(EMBEDDING_PROFILE.CONTENT_TYPE.eq(TYPE)).execute();
            return ctx.insertInto(EMBEDDING_PROFILE, EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE,
                    EMBEDDING_PROFILE.EMBEDDING_MODEL, EMBEDDING_PROFILE.DIMENSION)
                .values(tenant, TYPE, model, dimension).execute();
        });
    }

    private String name(Tn t, String model, int version) {
        return TYPE + "__" + t.owner() + "__" + model + "__v" + version;
    }

    /** Register {@code collection} for the tenant (the model comes from the conformant name). */
    private void register(Tn t, String collection) {
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, t.tenant(), collection);
            return null;
        });
    }

    private Tn tenant(String tag) {
        int n = seq.incrementAndGet();
        String tenant = "reembed-routes-" + tag + n;
        String token = "reembed-routes-token-" + tag + n;
        su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, token, tenant, "reembed-routes-test");
            return null;
        });
        String owner = "rt" + tag + n;
        Tn t = new Tn(tenant, token, owner, TYPE + "__" + owner + "__" + BGE + "__v1",
            TYPE + "__" + owner + "__" + MINI + "__v1");
        register(t, t.src());
        register(t, t.tgt());
        setProfile(tenant, MINI, 384);
        // The engine sweeps registered-but-empty "ghost" collections on a tenant's first authenticated request
        // (RDR-204 P1), and these two hold no chunk: mark the sweep done so the request finds them.
        catalog.setMeta(tenant, "rdr204_ghost_sweep_v1", "done");
        return t;
    }

    // ── http helpers ─────────────────────────────────────────────────────────

    private HttpResponse<String> post(Tn t, String path, String body) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + service.getPort() + BASE + path))
            .header("Authorization", "Bearer " + t.token())
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(Tn t, String path) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + service.getPort() + BASE + path))
            .header("Authorization", "Bearer " + t.token()).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> json(HttpResponse<String> r) throws Exception {
        return MAPPER.readValue(r.body(), MAP_T);
    }

    private static String startBody(String source) {
        return "{\"source_collection\":\"" + source + "\"}";
    }

    private Map<String, Object> startOk(Tn t, String source) throws Exception {
        var r = post(t, "/jobs", startBody(source));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(201);
        return json(r);
    }

    private static UUID id(Map<String, Object> job) {
        return UUID.fromString((String) job.get("job_id"));
    }

    private Job job(Tn t, UUID id) {
        return jobs.get(t.tenant(), id).orElseThrow();
    }

    /** Move a job through {@code states} by the repository's compare-and-set, as the runner would. */
    private Job advance(Tn t, UUID id, String... states) {
        Job j = job(t, id);
        for (String to : states) {
            Transition change = ReembedJobRepository.FAILED.equals(to)
                ? Transition.failed(ReembedStateMachine.failureReason(Event.BATCH_FAILED), "test failure")
                : ReembedJobRepository.DONE.equals(to) ? Transition.disposition("kept_by_flag") : Transition.NONE;
            j = jobs.applyTransition(t.tenant(), id, j.state(), j.cutoverStep(), to, change)
                .orElseThrow(() -> new AssertionError(job(t, id).state() + " -> " + to + " matched no row"));
        }
        return j;
    }

    private int wakeUps() {
        return executor.executed.size();
    }

    // ── start: the route, the lock, the body ─────────────────────────────────

    @Test
    void startUsesTheLiveSiblingAndAnswers201WithTheRowAndWakesTheRunner() throws Exception {
        Tn t = tenant("live");
        int before = wakeUps();

        var r = post(t, "/jobs", "{\"source_collection\":\"" + t.src()
            + "\",\"token_cap\":5000,\"keep_source\":true,\"force_delete\":false}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(201);
        Map<String, Object> body = json(r);
        assertThat(body).containsEntry("state", "planned").containsEntry("source_collection", t.src())
            .containsEntry("target_collection", t.tgt()).containsEntry("target_action", "existing")
            .containsEntry("keep_source", true).containsEntry("force_delete", false)
            .containsEntry("documents_moved", 0).containsEntry("pass_number", 1);
        assertThat(((Number) body.get("token_cap")).longValue()).isEqualTo(5000L);
        Job row = job(t, id(body));
        assertThat(row.state()).isEqualTo("planned");
        assertThat(row.tokenCap()).isEqualTo(5000L);
        assertThat(row.keepSource()).isTrue();
        assertThat(wakeUps()).as("the start woke the runner once").isEqualTo(before + 1);
        assertThat(get(t, "/jobs").statusCode()).as("a GET on the start route is not a start").isEqualTo(405);
    }

    @Test
    void theRouteIsReachedNotShadowedByTheVectorsContext() throws Exception {
        Tn t = tenant("ctx");
        var unknown = post(t, "/nope", "{}");
        assertThat(unknown.statusCode()).isEqualTo(404);
        assertThat(json(unknown).get("error").toString()).contains("/nope");
        var noBody = post(t, "/jobs", "");
        assertThat(noBody.statusCode()).as("the handler's own validation, not the vectors handler's").isEqualTo(400);
        assertThat(json(noBody).get("error").toString()).contains("source_collection");
    }

    @Test
    void twoStartsOnOneSourceAreRefused_andAResentStartIs409NamingTheExistingJob() throws Exception {
        Tn t = tenant("twice");
        Map<String, Object> first = startOk(t, t.src());
        int before = wakeUps();

        var resent = post(t, "/jobs", startBody(t.src()));
        assertThat(resent.statusCode()).isEqualTo(409);
        Map<String, Object> refusal = json(resent);
        assertThat(refusal).containsEntry("job_id", first.get("job_id")).containsEntry("state", "planned");
        assertThat(refusal.get("error").toString()).contains((String) first.get("job_id"));

        // A start that differs in every other field meets the same lock.
        var other = post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"keep_source\":true,"
            + "\"token_cap\":10}");
        assertThat(other.statusCode()).isEqualTo(409);
        assertThat(json(other)).containsEntry("job_id", first.get("job_id"));

        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).hasSize(1);
        assertThat(wakeUps()).as("a refused start wakes nothing").isEqualTo(before);
    }

    @Test
    void aSecondStartWhileTheJobIsFailedIsRefusedNamingIt() throws Exception {
        Tn t = tenant("failed");
        UUID jobId = id(startOk(t, t.src()));
        advance(t, jobId, "running", "failed");

        var r = post(t, "/jobs", startBody(t.src()));

        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r)).containsEntry("job_id", jobId.toString()).containsEntry("state", "failed");
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).hasSize(1);
    }

    @Test
    void aSourceThatIsAnotherJobsTargetIsRefusedNamingThatJob() throws Exception {
        Tn t = tenant("intosrc");
        UUID jobId = id(startOk(t, t.src()));

        // The target of the running job, as a source: the job is filling it.
        setProfile(t.tenant(), BGE, 768);
        var r = post(t, "/jobs", startBody(t.tgt()));

        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r)).containsEntry("job_id", jobId.toString());
    }

    @Test
    void bodyValidationIs400AndAnAbsentSourceIs404() throws Exception {
        Tn t = tenant("valid");
        assertThat(post(t, "/jobs", "{not json").statusCode()).isEqualTo(400);
        assertThat(post(t, "/jobs", "[1]").statusCode()).as("an array is not a body").isEqualTo(400);
        assertThat(post(t, "/jobs", "{}").statusCode()).isEqualTo(400);
        assertThat(post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"keep_sorce\":true}").statusCode())
            .as("a misspelt field is refused, not ignored").isEqualTo(400);
        assertThat(post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"token_cap\":0}").statusCode())
            .isEqualTo(400);
        assertThat(post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"token_cap\":\"9\"}").statusCode())
            .isEqualTo(400);
        assertThat(post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"keep_source\":\"yes\"}")
            .statusCode()).isEqualTo(400);
        assertThat(post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\""
            + t.src() + "\"}").statusCode()).as("target equal to source").isEqualTo(400);
        var absent = post(t, "/jobs", startBody("knowledge__nobody__" + BGE + "__v1"));
        assertThat(absent.statusCode()).isEqualTo(404);
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();
    }

    @Test
    void aSourceAlreadyOnTheProfileModelIsRefused() throws Exception {
        Tn t = tenant("same");
        setProfile(t.tenant(), BGE, 768);
        var r = post(t, "/jobs", startBody(t.src()));
        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r).get("error").toString()).contains("already holds the profile model");
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();
    }

    // ── start: the target the client names ───────────────────────────────────

    @Test
    void anAbsentTargetIsRegisteredFromTheClientsNameAndTheProfileFixesItsModel() throws Exception {
        Tn t = tenant("reg");
        String fresh = name(t, MINI, 2);
        su(ctx -> ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant()))
            .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(t.tgt())).execute());
        assertThat(catalog.getCollection(t.tenant(), fresh)).isNull();
        // The client's name says one model; the profile says another. The profile gate fixes the registered model.
        String misleading = TYPE + "__" + t.owner() + "__voyage-context-3__v1";

        var r = post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\"" + misleading
            + "\"}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(201);
        assertThat(json(r)).containsEntry("target_collection", misleading).containsEntry("target_action", "registered");
        Map<String, Object> row = catalog.getCollection(t.tenant(), misleading);
        assertThat(row).containsEntry("embedding_model", MINI).containsEntry("content_type", TYPE)
            .containsEntry("owner_id", t.owner()).containsEntry("superseded_by", "");
    }

    @Test
    void anAbsentTargetWithNoNameIs400_andNothingIsRegistered() throws Exception {
        Tn t = tenant("noname");
        su(ctx -> ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant()))
            .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(t.tgt())).execute());

        var r = post(t, "/jobs", startBody(t.src()));

        assertThat(r.statusCode()).isEqualTo(400);
        assertThat(json(r).get("error").toString()).contains("target_collection");
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();
    }

    @Test
    void aClientNamedTargetThatIsRegisteredWithAnotherTupleIs409_andIsNotRepointed() throws Exception {
        Tn t = tenant("mismatch");
        su(ctx -> ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant()))
            .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(t.tgt())).execute());
        // The name the client wants is already a bge collection (another tuple), superseded by something else.
        String taken = name(t, MINI, 7);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, t.tenant(), taken, BGE);
            return null;
        });
        catalog.supersedeCollection(t.tenant(), taken, t.src(), null);

        var r = post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\"" + taken
            + "\"}");

        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r).get("error").toString()).contains(taken).contains(BGE);
        assertThat(catalog.getCollection(t.tenant(), taken)).as("the row is as it was, not repointed or revived")
            .containsEntry("embedding_model", BGE).containsEntry("superseded_by", t.src());
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();
    }

    // ── start: reviving a superseded target ──────────────────────────────────

    /** A to B done (or aborted) with A superseded by B, as a cutover leaves it; then the profile names A's model. */
    private UUID earlierLeg(Tn t, String how) throws Exception {
        UUID first = id(startOk(t, t.src()));
        if ("done".equals(how)) {
            advance(t, first, "running", "cutover");
            assertThat(jobs.setCutoverStep(t.tenant(), first, "superseded")).isTrue();
            assertThat(catalog.supersedeCollection(t.tenant(), t.src(), t.tgt(), null)).isEqualTo(1);
            advance(t, first, "done");
        } else {
            advance(t, first, "running", "aborted");
            assertThat(catalog.supersedeCollection(t.tenant(), t.src(), t.tgt(), null)).isEqualTo(1);
        }
        setProfile(t.tenant(), BGE, 768);
        return first;
    }

    @Test
    void aSupersededTargetNamingTheSourceIsRevived_andTheEarlierDoneJobIsMarkedRevived() throws Exception {
        Tn t = tenant("revdone");
        UUID first = earlierLeg(t, "done");
        assertThat(catalog.getCollection(t.tenant(), t.src())).containsEntry("superseded_by", t.tgt());

        var r = post(t, "/jobs", startBody(t.tgt()));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(201);
        assertThat(json(r)).containsEntry("target_collection", t.src()).containsEntry("target_action", "revived")
            .containsEntry("revived_job_id", first.toString());
        assertThat(catalog.getCollection(t.tenant(), t.src())).as("the supersession is cleared")
            .containsEntry("superseded_by", "");
        Job earlier = job(t, first);
        assertThat(earlier.state()).as("state is unchanged").isEqualTo("done");
        assertThat(earlier.disposition()).isEqualTo("revived");
    }

    @Test
    void anAbortedEarlierJobIsMarkedRevivedToo_itsStateUnchanged() throws Exception {
        Tn t = tenant("revabort");
        UUID first = earlierLeg(t, "aborted");

        var r = post(t, "/jobs", startBody(t.tgt()));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(201);
        assertThat(json(r)).containsEntry("target_action", "revived").containsEntry("revived_job_id", first.toString());
        Job earlier = job(t, first);
        assertThat(earlier.state()).isEqualTo("aborted");
        assertThat(earlier.disposition()).isEqualTo("revived");
        assertThat(catalog.getCollection(t.tenant(), t.src())).containsEntry("superseded_by", "");
    }

    @Test
    void aFailedEarlierJobHoldsTheLock_soTheWayBackIsRefusedNamingIt_andNothingIsRevived() throws Exception {
        Tn t = tenant("revfailed");
        UUID first = id(startOk(t, t.src()));
        advance(t, first, "running", "failed");
        setProfile(t.tenant(), BGE, 768);

        var r = post(t, "/jobs", startBody(t.tgt()));

        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r)).containsEntry("job_id", first.toString()).containsEntry("state", "failed");
        assertThat(job(t, first).disposition()).isNull();
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).hasSize(1);
    }

    @Test
    void aTargetSupersededByAnotherNameIsRefusedNamingTheRowAndItsSuccessor() throws Exception {
        Tn t = tenant("revother");
        String third = name(t, MINI, 2);
        register(t, third);
        setProfile(t.tenant(), BGE, 768);
        // The bge sibling A is superseded by a third collection, not by the source B of the new job.
        assertThat(catalog.supersedeCollection(t.tenant(), t.src(), third, null)).isEqualTo(1);

        var r = post(t, "/jobs", startBody(t.tgt()));

        assertThat(r.statusCode()).isEqualTo(409);
        Map<String, Object> body = json(r);
        assertThat(body.get("error").toString()).contains(t.src()).contains(third);
        assertThat(body).containsEntry("collection", t.src());
        assertThat(catalog.getCollection(t.tenant(), t.src())).as("not revived").containsEntry("superseded_by", third);
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();
    }

    @Test
    void aReviveWhoseStartLostIsHealedByTheRetry_theEarlierJobIsMarkedRevivedOnTheLiveBranch() throws Exception {
        Tn t = tenant("revheal");
        UUID first = earlierLeg(t, "done");
        // The first attempt got as far as the upsert that clears the supersession and then lost the start (a lock
        // held, a connection gone, a crash before the mark): the target is live, the earlier job is not yet marked.
        catalog.upsertCollection(t.tenant(), Map.of("name", t.src(), "content_type", TYPE, "owner_id", t.owner()));
        assertThat(catalog.getCollection(t.tenant(), t.src())).containsEntry("superseded_by", "");
        assertThat(job(t, first).disposition()).as("the earlier job is not yet marked").isNotEqualTo("revived");

        var retry = post(t, "/jobs", startBody(t.tgt()));

        assertThat(retry.statusCode()).as(retry.body()).isEqualTo(201);
        assertThat(json(retry)).containsEntry("target_collection", t.src()).containsEntry("target_action", "existing")
            .containsEntry("revived_job_id", first.toString());
        Job earlier = job(t, first);
        assertThat(earlier.state()).as("state is unchanged").isEqualTo("done");
        assertThat(earlier.disposition()).isEqualTo("revived");

        // A later start on the live target finds the earlier job already marked and touches nothing.
        UUID second = id(json(retry));
        advance(t, second, "running", "aborted");
        Job marked = job(t, first);
        var again = post(t, "/jobs", startBody(t.tgt()));
        assertThat(again.statusCode()).as(again.body()).isEqualTo(201);
        assertThat(json(again)).containsEntry("target_action", "existing").doesNotContainKey("revived_job_id");
        assertThat(job(t, first).updatedAt()).as("an already revived job is not written again")
            .isEqualTo(marked.updatedAt());
    }

    // ── pause, resume, abort ─────────────────────────────────────────────────

    @Test
    void resumeOfAFailedJobSucceedsUnderItsOwnLock_wakesTheRunner_andClearsTheFailure() throws Exception {
        Tn t = tenant("resume");
        UUID jobId = id(startOk(t, t.src()));
        advance(t, jobId, "running", "failed");
        assertThat(job(t, jobId).failureReason()).isEqualTo("batch_failed");
        int before = wakeUps();

        var r = post(t, "/jobs/" + jobId + "/resume", "");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        Map<String, Object> body = json(r);
        assertThat(body).containsEntry("state", "running").containsEntry("failure_reason", null)
            .doesNotContainKey("unchanged");
        assertThat(job(t, jobId).state()).isEqualTo("running");
        assertThat(wakeUps()).isEqualTo(before + 1);
    }

    @Test
    void aResumeMayRaiseTheTokenCap_butNeverLowerItNorAddOne() throws Exception {
        Tn t = tenant("cap");
        UUID jobId = id(json(post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"token_cap\":100}")));
        advance(t, jobId, "running", "failed");

        var lower = post(t, "/jobs/" + jobId + "/resume", "{\"token_cap\":50}");
        assertThat(lower.statusCode()).isEqualTo(409);
        assertThat(job(t, jobId).state()).as("a refused resume changes nothing").isEqualTo("failed");
        assertThat(job(t, jobId).tokenCap()).isEqualTo(100L);

        var raise = post(t, "/jobs/" + jobId + "/resume", "{\"token_cap\":250}");
        assertThat(raise.statusCode()).as(raise.body()).isEqualTo(200);
        assertThat(((Number) json(raise).get("token_cap")).longValue()).isEqualTo(250L);
        assertThat(job(t, jobId).tokenCap()).isEqualTo(250L);
        assertThat(job(t, jobId).state()).isEqualTo("running");

        // A job with no cap has none to raise.
        Tn u = tenant("nocap");
        UUID uncapped = id(startOk(u, u.src()));
        advance(u, uncapped, "running", "failed");
        var add = post(u, "/jobs/" + uncapped + "/resume", "{\"token_cap\":10}");
        assertThat(add.statusCode()).isEqualTo(409);
        assertThat(job(u, uncapped).state()).isEqualTo("failed");

        // token_cap is a resume field only.
        assertThat(post(t, "/jobs/" + jobId + "/pause", "{\"token_cap\":300}").statusCode()).isEqualTo(400);
    }

    @Test
    void abortIsAcceptedFromPausedAndFailed() throws Exception {
        Tn paused = tenant("abpaused");
        UUID p = id(startOk(paused, paused.src()));
        advance(paused, p, "running", "paused");
        var rp = post(paused, "/jobs/" + p + "/abort", "");
        assertThat(rp.statusCode()).as(rp.body()).isEqualTo(200);
        assertThat(json(rp)).containsEntry("state", "aborted");
        assertThat(job(paused, p).finishedAt()).as("abort releases the lock and stamps the end").isNotNull();
        assertThat(jobs.listByStates(paused.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();

        Tn failed = tenant("abfailed");
        UUID f = id(startOk(failed, failed.src()));
        advance(failed, f, "running", "failed");
        var rf = post(failed, "/jobs/" + f + "/abort", "");
        assertThat(rf.statusCode()).isEqualTo(200);
        Job after = job(failed, f);
        assertThat(after.state()).isEqualTo("aborted");
        assertThat(after.failureReason()).as("an aborted failed job still says why it failed").isEqualTo("batch_failed");
    }

    @Test
    void abortIsAcceptedInCutoverBeforeTheDeleteAndRefusedAfterIt() throws Exception {
        Tn before = tenant("abcut1");
        UUID b = id(startOk(before, before.src()));
        advance(before, b, "running", "cutover");
        assertThat(jobs.setCutoverStep(before.tenant(), b, "superseded")).isTrue();
        var ok = post(before, "/jobs/" + b + "/abort", "");
        assertThat(ok.statusCode()).as(ok.body()).isEqualTo(200);
        assertThat(job(before, b).state()).isEqualTo("aborted");

        Tn after = tenant("abcut2");
        UUID a = id(startOk(after, after.src()));
        advance(after, a, "running", "cutover");
        assertThat(jobs.setCutoverStep(after.tenant(), a, "superseded")).isTrue();
        assertThat(jobs.setCutoverStep(after.tenant(), a, "delete_started")).isTrue();
        Job stored = job(after, a);

        var refused = post(after, "/jobs/" + a + "/abort", "");

        assertThat(refused.statusCode()).isEqualTo(409);
        assertThat(json(refused)).containsEntry("state", "cutover").containsEntry("job_id", a.toString());
        assertThat(json(refused).get("error").toString()).contains("delete step");
        Job unchanged = job(after, a);
        assertThat(unchanged.state()).isEqualTo("cutover");
        assertThat(unchanged.updatedAt()).isEqualTo(stored.updatedAt());
    }

    @Test
    void resentPauseResumeAndAbortChangeNothing_theUpdatedAtStaysEqual() throws Exception {
        Tn t = tenant("resend");
        UUID jobId = id(startOk(t, t.src()));
        advance(t, jobId, "running");
        int wakes = wakeUps();

        var pause = post(t, "/jobs/" + jobId + "/pause", "");
        assertThat(pause.statusCode()).isEqualTo(200);
        assertThat(json(pause)).containsEntry("state", "paused").doesNotContainKey("unchanged");
        Job paused = job(t, jobId);
        var pauseAgain = post(t, "/jobs/" + jobId + "/pause", "{}");
        assertThat(pauseAgain.statusCode()).isEqualTo(200);
        assertThat(json(pauseAgain)).containsEntry("state", "paused").containsEntry("unchanged", true)
            .containsEntry("updated_at", json(pause).get("updated_at"));
        assertThat(job(t, jobId).updatedAt()).isEqualTo(paused.updatedAt());

        var resume = post(t, "/jobs/" + jobId + "/resume", "");
        assertThat(json(resume)).containsEntry("state", "running").doesNotContainKey("unchanged");
        Job running = job(t, jobId);
        var resumeAgain = post(t, "/jobs/" + jobId + "/resume", "");
        assertThat(resumeAgain.statusCode()).isEqualTo(200);
        assertThat(json(resumeAgain)).containsEntry("unchanged", true).containsEntry("state", "running");
        assertThat(job(t, jobId).updatedAt()).isEqualTo(running.updatedAt());
        assertThat(wakeUps()).as("only the applied resume woke the runner").isEqualTo(wakes + 1);

        var abort = post(t, "/jobs/" + jobId + "/abort", "");
        assertThat(json(abort)).containsEntry("state", "aborted").doesNotContainKey("unchanged");
        Job aborted = job(t, jobId);
        var abortAgain = post(t, "/jobs/" + jobId + "/abort", "");
        assertThat(abortAgain.statusCode()).isEqualTo(200);
        assertThat(json(abortAgain)).containsEntry("unchanged", true).containsEntry("state", "aborted");
        Job still = job(t, jobId);
        assertThat(still.updatedAt()).isEqualTo(aborted.updatedAt());
        assertThat(still.finishedAt()).isEqualTo(aborted.finishedAt());
    }

    @Test
    void aTransitionTheStateMachineRefusesIs409NamingTheState() throws Exception {
        Tn t = tenant("refuse");
        UUID jobId = id(startOk(t, t.src()));

        var planned = post(t, "/jobs/" + jobId + "/pause", "");
        assertThat(planned.statusCode()).isEqualTo(409);
        assertThat(json(planned)).containsEntry("state", "planned").containsEntry("job_id", jobId.toString());
        assertThat(job(t, jobId).state()).isEqualTo("planned");

        advance(t, jobId, "running", "aborted");
        var resume = post(t, "/jobs/" + jobId + "/resume", "");
        assertThat(resume.statusCode()).isEqualTo(409);
        assertThat(json(resume)).containsEntry("state", "aborted");
        assertThat(json(resume).get("error").toString()).contains("aborted");
    }

    @Test
    void controlRoutesValidateTheIdAndKnowTheirVerbs() throws Exception {
        Tn t = tenant("ctl");
        assertThat(post(t, "/jobs/not-a-uuid/pause", "").statusCode()).isEqualTo(400);
        assertThat(post(t, "/jobs/" + UUID.randomUUID() + "/pause", "").statusCode()).isEqualTo(404);
        UUID jobId = id(startOk(t, t.src()));
        assertThat(post(t, "/jobs/" + jobId + "/explode", "").statusCode()).isEqualTo(404);
        assertThat(post(t, "/jobs/" + jobId, "").statusCode()).isEqualTo(404);
        assertThat(get(t, "/jobs/" + jobId + "/pause").statusCode()).isEqualTo(405);
    }

    @Test
    void everyRouteNeedsAValidToken_aMissingOrInvalidOneIs401AndNothingChanges() throws Exception {
        Tn t = tenant("auth");
        UUID jobId = id(startOk(t, t.src()));
        advance(t, jobId, "running");
        int wakes = wakeUps();
        String startBody = startBody(t.src());
        record Probe(String path, String body) { }
        List<Probe> probes = List.of(new Probe("/jobs", startBody),
            new Probe("/jobs/" + jobId + "/pause", ""), new Probe("/jobs/" + jobId + "/resume", ""),
            new Probe("/jobs/" + jobId + "/abort", ""));

        for (Probe p : probes) {
            var none = http.send(HttpRequest.newBuilder(
                    URI.create("http://127.0.0.1:" + service.getPort() + BASE + p.path()))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(p.body())).build(), HttpResponse.BodyHandlers.ofString());
            assertThat(none.statusCode()).as("no token: " + p.path()).isEqualTo(401);
            var bad = http.send(HttpRequest.newBuilder(
                    URI.create("http://127.0.0.1:" + service.getPort() + BASE + p.path()))
                .header("Authorization", "Bearer not-a-token-" + t.token())
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(p.body())).build(), HttpResponse.BodyHandlers.ofString());
            assertThat(bad.statusCode()).as("invalid token: " + p.path()).isEqualTo(401);
        }

        assertThat(job(t, jobId).state()).as("no unauthenticated request changed the job").isEqualTo("running");
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).hasSize(1);
        assertThat(wakeUps()).as("and none woke the runner").isEqualTo(wakes);
    }

    @Test
    void aJobPaused_thenTheProfileChanged_thenResumed_isFailedProfileChangedByTheNextTick() throws Exception {
        Tn t = tenant("pausedprofile");
        UUID jobId = id(startOk(t, t.src()));
        advance(t, jobId, "running");
        var pause = post(t, "/jobs/" + jobId + "/pause", "");
        assertThat(pause.statusCode()).as(pause.body()).isEqualTo(200);
        assertThat(json(pause)).containsEntry("state", "paused");

        // The tenant's model for the content type is no longer the target's.
        setProfile(t.tenant(), BGE, 768);
        int wakes = wakeUps();
        var resume = post(t, "/jobs/" + jobId + "/resume", "");

        // The route does not re-check the profile: it answers running and wakes the runner, whose drive fails the job.
        assertThat(resume.statusCode()).as(resume.body()).isEqualTo(200);
        assertThat(json(resume)).containsEntry("state", "running");
        assertThat(wakeUps()).isEqualTo(wakes + 1);

        // The wake-up runs the runner's tick; here the executor only records it, so run a runner over this tenant
        // alone (the service's own would visit every other test's tenants too). No batch is reached, so the
        // writer is not needed.
        new dev.nexus.service.reembed.ReembedRunner(jobs, new dev.nexus.service.db.ReembedSourceReader(scope),
            catalog, null, () -> List.of(t.tenant()), dev.nexus.service.reembed.ReembedRunner.Settings.DEFAULT)
            .tick();

        Job failed = job(t, jobId);
        assertThat(failed.state()).isEqualTo("failed");
        assertThat(failed.failureReason()).isEqualTo("profile_changed");
        assertThat(failed.lastError()).contains(BGE).contains(t.tgt());
        assertThat(failed.documentsMoved()).isZero();
        // The failed job holds its lock, and a resume of it is the same re-check again.
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).hasSize(1);
    }

    @Test
    void aTenantCannotReachAnotherTenantsJob() throws Exception {
        Tn mine = tenant("mine");
        Tn theirs = tenant("theirs");
        UUID jobId = id(startOk(theirs, theirs.src()));
        advance(theirs, jobId, "running");

        var r = post(mine, "/jobs/" + jobId + "/abort", "");

        assertThat(r.statusCode()).as("RLS hides the row: not found, not forbidden").isEqualTo(404);
        assertThat(job(theirs, jobId).state()).isEqualTo("running");
    }
}
