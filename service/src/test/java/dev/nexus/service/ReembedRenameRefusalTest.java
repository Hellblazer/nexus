// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
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
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.db.ReembedJobRepository.ABORTED;
import static dev.nexus.service.db.ReembedJobRepository.CUTOVER;
import static dev.nexus.service.db.ReembedJobRepository.FAILED;
import static dev.nexus.service.db.ReembedJobRepository.PAUSED;
import static dev.nexus.service.db.ReembedJobRepository.PLANNED;
import static dev.nexus.service.db.ReembedJobRepository.RUNNING;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg, ruling 4 of {@code plan-nexus-f98bg-rulings}): a collection that a
 * re-embed job in a lock state holds as its source or its target is not renamed underneath it. The refusal is a
 * 409 that names the job, from {@code POST /v1/catalog/collections/rename}, over HTTP on a real Postgres as
 * {@code CatalogHandlerRenameTest} drives it. A job that has finished holds nothing.
 *
 * <p>The handler's own earlier guards answer first where they apply (a missing source is 404, a live target 409
 * "already exists"), so each case here is one the handler would otherwise let through: the held name is the
 * registered source, a registered target, or an unregistered new name.
 *
 * <p>Run: {@code -Dtest=ReembedRenameRefusalTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedRenameRefusalTest {

    private static final String TOKEN = "reembed-rename-refusal-token-123";
    private static final String SVC_ROLE = "svc_reembed_rename";
    private static final String SVC_PASS = "svc_reembed_rename_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() { };

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private NexusService service;
    private HttpClient http;
    private ObjectMapper mapper;
    private ReembedJobRepository jobs;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
        jobs = new ReembedJobRepository(new TenantScope(svcDs));
        // The tenant's first request runs the once-per-process ghost sweep, which deletes registered-but-empty
        // collections: burn it before any collection below is registered (see CatalogHandlerRenameTest).
        http.send(HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String name(String prefix) {
        return "rnref" + seq.incrementAndGet() + "__" + prefix;
    }

    private void register(String... names) throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String n : names) {
                PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, n);
            }
        }
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> rename(String from, String to) throws Exception {
        return post("/v1/catalog/collections/rename", "{\"old_name\":\"" + from + "\",\"new_name\":\"" + to + "\"}");
    }

    private HttpResponse<String> rehome(String source, String target) throws Exception {
        return post("/v1/catalog/collections/rehome", "{\"source\":\"" + source + "\",\"target\":\"" + target + "\"}");
    }

    /** The satellite and statistics routes that rewrite a collection name in {@code {old, new}} form. */
    private static final java.util.List<String> NAME_REWRITING_ROUTES = java.util.List.of(
        "/v1/chash/rename_collection",
        "/v1/taxonomy/rename_collection",
        "/v1/telemetry/rename_collection",
        "/v1/aspects/rename_collection",
        "/v1/aspects/highlights/rename_collection",
        "/v1/aspects/queue/rename_collection");

    private void assert409Naming(HttpResponse<String> resp, Job job) throws Exception {
        assertThat(resp.statusCode()).isEqualTo(409);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("job_id")).isEqualTo(job.jobId().toString());
        assertThat(String.valueOf(body.get("error"))).contains("re-embed job").contains(job.jobId().toString())
            .contains(job.sourceCollection()).contains(job.targetCollection());
    }

    @Test
    void renamingTheSourceOfAJobInAnyLockState_isRefused409NamingTheJob() throws Exception {
        for (String state : ReembedJobRepository.LOCK_STATES) {
            String src = name("src");
            String tgt = name("tgt");
            String elsewhere = name("elsewhere");
            register(src);
            Job job = inState(src, tgt, state);

            assert409Naming(rename(src, elsewhere), job);
        }
    }

    @Test
    void renamingTheRegisteredTargetOfAJob_isRefused409NamingTheJob() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        register(tgt);
        Job job = jobs.start(TENANT, src, tgt, null, false, false);

        assert409Naming(rename(tgt, name("elsewhere")), job);
    }

    @Test
    void renamingOntoAnUnregisteredNameAJobHolds_isRefused409NamingTheJob() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        String free = name("free");
        register(free);
        Job job = jobs.start(TENANT, src, tgt, null, false, false);

        assert409Naming(rename(free, tgt), job);
        assert409Naming(rename(free, src), job);
    }

    @Test
    void aCollectionNoJobHolds_renamesAsBefore_whileAnotherJobHoldsItsNeighbours() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        String free = name("free");
        String renamed = name("renamed");
        register(free);
        jobs.start(TENANT, src, tgt, null, false, false);

        var resp = rename(free, renamed);

        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T)).containsKey("renamed");
    }

    @Test
    void onceTheJobHasFinished_theRenameIsAllowed() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        String renamed = name("renamed");
        register(src);
        Job job = jobs.start(TENANT, src, tgt, null, false, false);
        assert409Naming(rename(src, renamed), job);

        jobs.applyTransition(TENANT, job.jobId(), PLANNED, null, ABORTED, Transition.NONE).orElseThrow();

        assertThat(rename(src, renamed).statusCode()).isEqualTo(200);
    }

    // ── rehome (C3 fix round: the rename guard did not cover it) ─────────────

    @Test
    void rehomingTheSourceOfAJobInAnyLockState_isRefused409NamingTheJob_andMovesNothing() throws Exception {
        for (String state : ReembedJobRepository.LOCK_STATES) {
            String src = name("src");
            String tgt = name("tgt");
            String elsewhere = name("elsewhere");
            register(src, elsewhere);
            Job job = inState(src, tgt, state);

            // the job's source is the collection being moved OUT of
            assert409Naming(rehome(src, elsewhere), job);
        }
    }

    @Test
    void rehomingOntoTheTargetOfAJob_isRefused409NamingTheJob() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        String free = name("free");
        register(tgt, free);
        Job job = jobs.start(TENANT, src, tgt, null, false, false);

        // the job's target is the collection being moved ONTO
        assert409Naming(rehome(free, tgt), job);
    }

    @Test
    void aRehomeNoJobHolds_isUnchanged_whileAnotherJobHoldsItsNeighbours() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        String a = name("a");
        String b = name("b");
        register(a, b);
        jobs.start(TENANT, src, tgt, null, false, false);

        var resp = rehome(a, b);

        assertThat(resp.statusCode()).as(resp.body()).isNotEqualTo(409);
        assertThat(resp.body()).doesNotContain("re-embed job");
    }

    // ── the other routes that rewrite a collection name ──────────────────────

    @Test
    void everyOtherNameRewritingRoute_refusesTheSourceOfAJob_409NamingTheJob() throws Exception {
        for (String route : NAME_REWRITING_ROUTES) {
            String src = name("src");
            String tgt = name("tgt");
            register(src);
            Job job = jobs.start(TENANT, src, tgt, null, false, false);

            var resp = post(route, "{\"old\":\"" + src + "\",\"new\":\"" + name("elsewhere") + "\"}");

            assertThat(resp.statusCode()).as(route).isEqualTo(409);
            assert409Naming(resp, job);
        }
    }

    @Test
    void everyOtherNameRewritingRoute_refusesARenameOntoTheTargetOfAJob() throws Exception {
        for (String route : NAME_REWRITING_ROUTES) {
            String src = name("src");
            String tgt = name("tgt");
            String free = name("free");
            register(free);
            Job job = jobs.start(TENANT, src, tgt, null, false, false);

            assert409Naming(post(route, "{\"old\":\"" + free + "\",\"new\":\"" + tgt + "\"}"), job);
        }
    }

    @Test
    void theOtherRoutesAnswerNormallyOnceTheJobHasFinished() throws Exception {
        String src = name("src");
        String tgt = name("tgt");
        register(src);
        Job job = jobs.start(TENANT, src, tgt, null, false, false);
        jobs.applyTransition(TENANT, job.jobId(), PLANNED, null, ABORTED, Transition.NONE).orElseThrow();

        for (String route : NAME_REWRITING_ROUTES) {
            String moved = name("moved");
            register(moved);
            var resp = post(route, "{\"old\":\"" + src + "\",\"new\":\"" + moved + "\"}");
            assertThat(resp.statusCode()).as(route + " after the job finished: " + resp.body()).isNotEqualTo(409);
            assertThat(resp.body()).doesNotContain("re-embed job");
        }
    }

    private Job inState(String src, String tgt, String state) {
        Job job = jobs.start(TENANT, src, tgt, null, false, false);
        if (state.equals(PLANNED)) {
            return job;
        }
        Job running = jobs.applyTransition(TENANT, job.jobId(), PLANNED, null, RUNNING, Transition.NONE).orElseThrow();
        return switch (state) {
            case RUNNING -> running;
            case PAUSED -> jobs.applyTransition(TENANT, job.jobId(), RUNNING, null, PAUSED, Transition.NONE).orElseThrow();
            case CUTOVER -> jobs.applyTransition(TENANT, job.jobId(), RUNNING, null, CUTOVER, Transition.NONE).orElseThrow();
            case FAILED -> jobs.applyTransition(TENANT, job.jobId(), RUNNING, null, FAILED,
                Transition.failed("batch_failed", "x")).orElseThrow();
            default -> throw new IllegalArgumentException(state);
        };
    }
}
