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
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.6 (bead nexus-p2m1x.26): the label rule at the re-embed target. A new target label the rule refuses is a
 * 400 on start and on plan, and nothing is registered; an existing target keeps its label whatever it is, used as-is
 * when live and refused for what it is when retired. The fixture is ReembedRoutesTest's (a real {@code NexusService}
 * on port 0 over a real Postgres as the service role); these tests sit apart so that file stays under its file-size
 * ceiling (scripts/file-size-exceptions.txt).
 *
 * <p>Run: {@code -Dtest=ReembedLabelRuleRoutesTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedLabelRuleRoutesTest {

    private static final String SVC_ROLE = "svc_reembed_label_rule_test";
    private static final String SVC_PASS = "svc_reembed_label_rule_test_pass";
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

    private void unregister(Tn t, String name) {
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant()))
            .and(CATALOG_COLLECTIONS.NAME.eq(name)).execute());
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

    // ── start: the route, the lock, the body ─────────────────────────────────

    @Test
    void aTargetLabelTheLabelRuleRefusesIs400_onStartAndOnPlan_andNothingIsRegistered() throws Exception {
        // RDR-229 P3.6: a re-embed target is a user-supplied label the engine registers. It is conformant in shape
        // (so requireRegistrable's version read passes) and breaks the label rule.
        Tn t = tenant("labelrule");
        unregister(t, t.tgt());
        for (String bad : new String[] {TYPE + "__" + t.owner() + "/x__" + MINI + "__v1",
                                        TYPE + "__" + "\u00e9".repeat(130) + "__" + MINI + "__v1"}) {
            for (String path : new String[] {"/jobs", "/plan"}) {
                var r = post(t, path, "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\"" + bad
                    + "\"}");
                assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(400);
                assertThat(json(r).get("error").toString()).contains("collection label");
            }
            assertThat(catalog.getCollection(t.tenant(), bad)).as("nothing registered").isNull();
        }
        assertThat(jobs.listByStates(t.tenant(), ReembedJobRepository.LOCK_STATES)).isEmpty();
    }

    @Test
    void anExistingLiveTargetThatBreaksTheLabelRuleIsUsedAsIs() throws Exception {
        // RDR-229 item 8: the rule gates a NEW label. A live target registered under an old install's label ('/')
        // is the job's target as it stands.
        Tn t = tenant("oldlabel");
        unregister(t, t.tgt());
        String legacy = TYPE + "__" + t.owner() + "/x__" + MINI + "__v1";
        register(t, legacy);
        // the seed helper reads a name's tuple with a regex that does not take a '/', so state the tuple outright
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, TYPE)
            .set(CATALOG_COLLECTIONS.OWNER_ID, t.owner()).set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, MINI)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant())).and(CATALOG_COLLECTIONS.NAME.eq(legacy)).execute());

        var r = post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\"" + legacy
            + "\"}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(201);
        assertThat(json(r)).containsEntry("target_collection", legacy).containsEntry("target_action", "existing");
    }

    @Test
    void anExistingRetiredTargetIsRefusedForWhatItIs_notForItsLabel() throws Exception {
        Tn t = tenant("oldretired");
        unregister(t, t.tgt());
        String legacy = TYPE + "__" + t.owner() + "/y__" + MINI + "__v1";
        register(t, legacy);
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "someone-else")
            .set(DSL.field(DSL.name("superseded_by_id"), Long.class), DSL.field(DSL.name("collection_id"), Long.class))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t.tenant())).and(CATALOG_COLLECTIONS.NAME.eq(legacy)).execute());

        var r = post(t, "/jobs", "{\"source_collection\":\"" + t.src() + "\",\"target_collection\":\"" + legacy
            + "\"}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
    }
}
