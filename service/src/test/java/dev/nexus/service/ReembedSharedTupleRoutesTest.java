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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-p2m1x.28: the re-embed start and plan refuse two collections that share a lookup tuple (content type, owner,
 * model), through a real {@code NexusService} on port 0 over a real Postgres, as {@code ReembedRoutesTest} sets them
 * up (its fixtures, copied: they are private there). The shapes: a source with a live twin; a live target a job
 * from a source with the same tuple moved documents into; and the flows that must NOT be refused (a sibling on
 * another model, a job that moved nothing, a source restarted into its own target).
 *
 * <p>Run: {@code -Dtest=ReembedSharedTupleRoutesTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedSharedTupleRoutesTest {

    private static final String SVC_ROLE = "svc_reembed_shared_tuple_test";
    private static final String SVC_PASS = "svc_reembed_shared_tuple_test_pass";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINI = "minilm-l6-v2-384";
    private static final String TYPE = "knowledge";
    private static final String BASE = "/v1/vectors/reembed";
    private static final java.util.function.Supplier<List<String>> ALL_SERVED =
        () -> List.of(BGE, MINI, "voyage-context-3", "voyage-code-3");

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
                Thread t = new Thread(r, "recording-reembed-shared-tuple-executor");
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
        service = new NexusService(0, "reembed-shared-tuple-root", svcDs,
            new EmbedderRouter(new Fake(MINI, 384), "document"), null, null, null, null, null,
            name -> name.equals("NX_ENGINE_LOCAL") ? "1" : null, () -> false, executor, null);
        service.start();
        // One fake embedder serves one model; the routes read the served models from here so a test can serve the
        // three the other tests use. The real router is exercised by theRealRouterIsWhatTheRoutesReadByDefault.
        service.setReembedServedModelsForTests(ALL_SERVED);
        http = TestHttp.client();
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
        String tenant = "reembed-shared-tuple-" + tag + n;
        String token = "reembed-shared-tuple-token-" + tag + n;
        su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, token, tenant, "reembed-shared-tuple-test");
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
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + BASE + path)
            .header("Authorization", "Bearer " + t.token())
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(Tn t, String path) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + BASE + path)
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
    // ── the refusals ─────────────────────────────────────────────────────────

    private static final String VC3 = "voyage-context-3";

    /** A job of {@code t}'s src into tgt, ended in {@code state}, with {@code moved} documents recorded as moved. */
    private UUID leg(Tn t, String state, long moved) throws Exception {
        UUID first = id(startOk(t, t.src()));
        advance(t, first, "running", state);
        su(ctx -> ctx.update(REEMBED_JOBS).set(REEMBED_JOBS.DOCUMENTS_MOVED, moved)
            .where(REEMBED_JOBS.TENANT_ID.eq(t.tenant())).and(REEMBED_JOBS.JOB_ID.eq(first)).execute());
        return first;
    }

    /** A cutover's supersede of the first source (a kept source), so it is no live twin of anything. */
    private void supersedeSrc(Tn t) {
        assertThat(catalog.supersedeCollection(t.tenant(), t.src(), t.tgt(), null)).isEqualTo(1);
    }

    /** A cutover's default delete of the first source: its registry row is gone. */
    private void deleteSrcRow(Tn t) {
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant()))
            .and(CATALOG_COLLECTIONS.NAME.eq(t.src())).execute());
    }

    /** A collection of another stem on the tenant's owner and the first source's model (the shared-curator twin). */
    private String twin(Tn t) {
        String twin = TYPE + "__wow-addon-dev__" + BGE + "__v1";
        register(t, twin);
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, t.owner())
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant())).and(CATALOG_COLLECTIONS.NAME.eq(twin)).execute());
        return twin;
    }

    @Test
    void aSourceSharingItsTupleWithAnotherLiveCollectionIsRefusedByPlanAndStart_namingBothAndTheExit() throws Exception {
        Tn t = tenant("sharedtuple");
        String mate = twin(t);

        for (String path : List.of("/jobs", "/plan")) {
            var r = post(t, path, startBody(t.src()));
            assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(409);
            assertThat(json(r).get("error").toString()).contains(t.src()).contains(mate).contains("same owner")
                .contains("client cannot change a collection's owner in place").contains("engine data move")
                .contains("rename that re-homes the collection").contains("re-owned or superseded");
        }
        assertThat(post(t, "/jobs", startBody(mate)).statusCode()).as("from the other side too").isEqualTo(409);
        assertThat(jobs.listAll(t.tenant())).as("the refusals wrote no job").isEmpty();
    }

    /** The names of every collection the tenant has, whatever its state. */
    private List<String> collectionNames(Tn t) {
        return catalog.listCollections(t.tenant(), TYPE, null).stream().map(r -> String.valueOf(r.get("name")))
            .sorted().toList();
    }

    @Test
    void aSharedTupleRefusalWhoseTargetIsAbsentComesBeforeAnyRegistration_onPlanAndStart() throws Exception {
        Tn t = tenant("absenttarget");
        String mate = twin(t);
        // No row on the profile-model tuple: a start that got past the refusal would have to register one.
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant()))
            .and(CATALOG_COLLECTIONS.NAME.eq(t.tgt())).execute());
        List<String> before = collectionNames(t);
        assertThat(before).as("the fixture: two sources on one tuple and nothing on the target's").containsExactly(
            t.src(), mate).doesNotContain(t.tgt());
        String fresh = name(t, MINI, 2);

        for (String path : List.of("/plan", "/jobs")) {
            var r = post(t, path, "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\"" + fresh
                + "\"}");
            assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(409);
            assertThat(json(r).get("error").toString()).contains(t.src()).contains(mate).contains("same owner");
            assertThat(collectionNames(t)).as(path + ": the tenant's collection list is unchanged").isEqualTo(before);
        }
        assertThat(jobs.listAll(t.tenant())).isEmpty();
    }

    @Test
    void aTwinOfAMovedSourceIsRefusedByPlanAndStart_theFirstSourceSupersededOrItsRowGone() throws Exception {
        for (boolean rowGone : new boolean[] {false, true}) {
            Tn t = tenant("twin" + rowGone);
            UUID first = leg(t, "aborted", 3);
            supersedeSrc(t);
            if (rowGone) {
                deleteSrcRow(t);
            }
            String twin = twin(t);

            for (String path : List.of("/jobs", "/plan")) {
                var r = post(t, path, startBody(twin));
                assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(409);
                Map<String, Object> body = json(r);
                assertThat(body.get("error").toString()).contains(twin).contains(t.tgt()).contains(t.src())
                    .contains("the job log shows").contains(first.toString()).contains("3 document(s) moved")
                    .contains("engine data move").contains("rename that re-homes the collection")
                    .contains(twin + " is re-owned, which gives it a different target tuple")
                    .contains("re-owning " + t.src() + " does not take its documents out of the target")
                    .contains("earlier version of " + twin).doesNotContain("holds documents");
                assertThat(body).containsEntry("origin_source", t.src()).containsEntry("collection", t.tgt());
            }
            assertThat(jobs.listAll(t.tenant())).as("rowGone=" + rowGone + ": no job from the refused start")
                .hasSize(1);
            assertThat(catalog.getCollection(t.tenant(), t.tgt())).as("the target is as it was")
                .containsEntry("superseded_by", "").containsEntry("owner_id", t.owner())
                .containsEntry("embedding_model", MINI);
        }
    }

    @Test
    void aTwinOfAMovedSourceIsStillRefusedWhenTheMovedSourceWasReOwnedSince_planAndStart() throws Exception {
        Tn t = tenant("reowned");
        leg(t, "aborted", 3);
        supersedeSrc(t);
        // The first source is re-owned after its job ran: its documents are still in the target.
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, "somewhere-else")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant())).and(CATALOG_COLLECTIONS.NAME.eq(t.src())).execute());
        String twin = twin(t);

        for (String path : List.of("/plan", "/jobs")) {
            var r = post(t, path, startBody(twin));
            assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(409);
            assertThat(json(r).get("error").toString()).contains(twin).contains(t.tgt()).contains(t.src())
                .contains("the job log shows");
        }
        assertThat(jobs.listAll(t.tenant())).as("the refused starts wrote no job").hasSize(1);
    }

    @Test
    void aSiblingOnAnotherModelOfTheSameOwnerIsNotRefusedAfterTheFirstMoved_rowKeptOrGone() throws Exception {
        for (boolean rowGone : new boolean[] {false, true}) {
            Tn t = tenant("sibling" + rowGone);
            leg(t, "done", 3);
            supersedeSrc(t);
            if (rowGone) {
                deleteSrcRow(t);
            }
            // One owner with docs in two models, both re-embedded into the profile model's tuple.
            String sibling = TYPE + "__" + t.owner() + "__" + VC3 + "__v1";
            register(t, sibling);

            for (String path : List.of("/plan", "/jobs")) {
                var r = post(t, path, startBody(sibling));
                assertThat(r.statusCode()).as("rowGone=" + rowGone + " " + path + " " + r.body())
                    .isIn(200, 201);
                assertThat(json(r)).containsEntry("target_collection", t.tgt());
            }
        }
    }

    @Test
    void aV2OfAnAlreadyMovedSourceIsRefusedByPlanAndStart_aDeliberateConservativeRefusal() throws Exception {
        Tn t = tenant("foreign");
        leg(t, "aborted", 3);
        // The first source left the tuple (superseded, as a cutover leaves it); a v2 of it arrives on the tuple.
        supersedeSrc(t);
        String second = name(t, BGE, 2);
        register(t, second);

        for (String path : List.of("/plan", "/jobs")) {
            var r = post(t, path, startBody(second));
            assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(409);
            assertThat(json(r).get("error").toString()).contains(second).contains(t.tgt()).contains(t.src())
                .contains("the job log shows").contains("earlier version of " + second);
        }
        assertThat(jobs.listAll(t.tenant())).as("the refused starts wrote no job").hasSize(1);
    }

    @Test
    void aJobThatMovedNoDocumentIsNotAnOrigin_planThenStart() throws Exception {
        Tn t = tenant("zerocopy");
        leg(t, "aborted", 0);
        supersedeSrc(t);
        String twin = twin(t);

        var plan = post(t, "/plan", startBody(twin));
        assertThat(plan.statusCode()).as("an aborted zero-copy job put nothing in the target: " + plan.body())
            .isEqualTo(200);
        assertThat(json(plan)).containsEntry("target_collection", t.tgt()).containsEntry("target_action", "existing");
        assertThat(jobs.listAll(t.tenant())).as("the plan wrote nothing").hasSize(1);

        var start = post(t, "/jobs", startBody(twin));
        assertThat(start.statusCode()).as(start.body()).isEqualTo(201);
        assertThat(json(start)).containsEntry("target_collection", t.tgt())
            .containsEntry("target_action", "existing");
    }

    @Test
    void aSourceRestartedIntoItsOwnEarlierTargetIsNotRefused_planThenStart() throws Exception {
        Tn t = tenant("sameorigin");
        leg(t, "aborted", 3);

        var plan = post(t, "/plan", startBody(t.src()));
        assertThat(plan.statusCode()).as(plan.body()).isEqualTo(200);
        assertThat(json(plan)).containsEntry("target_collection", t.tgt()).containsEntry("target_action", "existing");
        assertThat(jobs.listAll(t.tenant())).as("the plan wrote nothing").hasSize(1);

        var again = post(t, "/jobs", startBody(t.src()));
        assertThat(again.statusCode()).as(again.body()).isEqualTo(201);
        assertThat(json(again)).containsEntry("target_collection", t.tgt());
    }
}
