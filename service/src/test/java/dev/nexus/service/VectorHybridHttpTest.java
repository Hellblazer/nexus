// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.VectorHandler;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Liquibase;
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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-155 P3.2 (bead nexus-eap5l): {@code POST /v1/vectors/hybrid-search} HTTP seam.
 *
 * <p>This is the wiring test for the validation seam the conexus xr7.8.9 go-live gate
 * drives: the pgvector hybrid fusion exposed through the EXISTING /v1/vectors surface
 * (no new public surface). The fusion semantics themselves are locked by the P3.1
 * suites ({@code PgVectorHybridSearchContractTest}, plus the Chroma-era
 * {@code HybridParityIntegrationTest}, deleted at RDR-155 P4b);
 * this class pins only the HTTP envelope:
 * <ul>
 *   <li>Route exists, request body matches /search, response is the flat row list.
 *   <li>Tenant is SERVER-RESOLVED from the bearer token (RLS boundary) — a token bound
 *       to another tenant sees zero rows, and no client-supplied header can widen it.
 *   <li>503 when no PgVectorRepository is wired (the /embed absent-backend pattern;
 *       since the RDR-155 P4a.2 serving cutover this applies to every vector route).
 *   <li>400 on a malformed body (missing query).
 * </ul>
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, full master changelog, port 0,
 * nexus_svc pool, FakeEmbedder vectors.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHybridHttpTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN_A = "hybrid-http-token-a-0123456789abcdef";
    private static final String TOKEN_B = "hybrid-http-token-b-0123456789abcdef";
    private static final String TENANT_A = "hyb-http-tenant-a";
    private static final String TENANT_B = "hyb-http-tenant-b";

    private static final String COL = "knowledge__httph__voyage-context-3__v1";
    private static final String Q   = "tenant isolation policy";

    /** Canonical 64-hex chash fixtures (RDR-180: full digest, not a hand-padded id). */
    private static final String HH_C1 = dev.nexus.service.db.Chash.ofText("hh-c1").toHex();
    private static final String HH_C2 = dev.nexus.service.db.Chash.ofText("hh-c2").toHex();
    private static final String HH_C3 = dev.nexus.service.db.Chash.ofText("hh-c3").toHex();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository pgRepo;
    NexusService service;        // hybrid-wired
    NexusService serviceNoPg;    // no pgvector backend — /hybrid-search must 503
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // nexus_svc role before Liquibase (grants changeset is fail-loud without it).
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        // Bind one token per tenant (server-side tenant resolution under test).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (var bound : List.of(Map.entry(TOKEN_A, TENANT_A),
                                     Map.entry(TOKEN_B, TENANT_B))) {
                PgContainerHelper.seedServiceToken(
                    dsl, bound.getKey(), bound.getValue(), "hybrid-http-test");
            }
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder.register(Q, 1.0f, 0.0f);
        embedder.register("the tenant isolation policy guards every row", 1.0f, 0.0f);
        embedder.register("tenant isolation policy enforcement in postgres", 0.8f, 0.6f);
        embedder.register("quantum entanglement spectroscopy experiment", 0.995f, 0.0998749f);
        pgRepo = new PgVectorRepository(tenantScope, embedder, embedder);
        // RDR-204 Phase 1 (bead nexus-ft04v.7): chunks_collection_fk is a REAL,
        // always-enforced FK now -- PgVectorRepository's stub-insert is retired.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, COL);
        }
        pgRepo.upsertChunks(TENANT_A, COL,
            List.of(HH_C1, HH_C2, HH_C3),
            List.of("the tenant isolation policy guards every row",
                    "tenant isolation policy enforcement in postgres",
                    "quantum entanglement spectroscopy experiment"),
            List.of(Map.of("kind", "hh"), Map.of("kind", "hh"), Map.of("kind", "hh")));

        service = new NexusService(0, TOKEN_A, svcDs, null, pgRepo);
        service.start();

        // No-pgvector service (RDR-155 P4a.2: the vectors context is always
        // registered; absent backend answers 503 per route, never 404/NPE).
        serviceNoPg = new NexusService(0, TOKEN_A, svcDs);
        serviceNoPg.start();

        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service     != null) service.stop();
        if (serviceNoPg != null) serviceNoPg.stop();
        if (svcDs       != null) svcDs.close();
        if (pg          != null) pg.stop();
    }

    private HttpResponse<String> post(NexusService svc, String token, Object body)
            throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + svc.getPort() + "/v1/vectors/hybrid-search"))
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void hybridSearch_overHttp_returnsFusedRows() throws Exception {
        var resp = post(service, TOKEN_A,
            Map.of("query", Q, "collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode()).as("hybrid-search 200").isEqualTo(200);
        List<Map<String, Object>> rows = MAPPER.readValue(resp.body(), List.class);
        assertThat(rows.stream().map(r -> r.get("id")).toList())
            .as("text-gated rows ranked by distance; the no-text-signal row hh-c3 "
                + "(vector-closer than hh-c2) is excluded")
            .containsExactly(HH_C1, HH_C2);
        assertThat(rows.get(0).get("kind"))
            .as("metadata flattens into HTTP rows exactly like /search")
            .isEqualTo("hh");
    }

    @Test
    void hybridSearch_tenantResolvedServerSide_fromBearer() throws Exception {
        // TOKEN_B is bound to TENANT_B, which owns no catalog_collections row for COL
        // (only TENANT_A does). RDR-204 Phase 2 (bead nexus-ft04v.16, coordinator
        // ruling): dispatch now resolves the collection's row through
        // CollectionRegistry BEFORE the RLS-scoped query ever runs, so this is now a
        // 422 (UnregisteredCollectionException) naming COL, not the old silent
        // empty-row-list result — there is still no client-supplied field that can
        // widen visibility across the RLS boundary; the boundary itself just fails
        // loud now instead of silently.
        var resp = post(service, TOKEN_B,
            Map.of("query", Q, "collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode())
            .as("a bearer bound to another tenant with no row for COL fails loud, 422")
            .isEqualTo(422);
        assertThat(resp.body()).contains(COL);
    }

    @Test
    void hybridSearch_withoutPgRepo_returns503() throws Exception {
        var resp = post(serviceNoPg, TOKEN_A,
            Map.of("query", Q, "collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode())
            .as("no pgvector backend: hybrid-search is explicitly not configured (the "
                + "/embed absent-backend pattern), never a silent fallback")
            .isEqualTo(503);
        assertThat(resp.body()).contains("not configured");
    }

    /**
     * RDR-204 Phase 2 fix round 2 (nexus-ft04v.16 fix round 2, S1 -- diff-scoped
     * critic Significant finding, coordinator design change on the second pass):
     * a fan-out over a registered + a never-registered collection is not
     * aborted -- the registered hits still come back -- and the dropped name is
     * visible to the HTTP caller as the {@code X-Nexus-Skipped-Collections}
     * response HEADER, never a body field. The shipped 7.37.0 client unwraps a
     * body object envelope only when {@code rerank=true}; with rerank off it
     * treats the payload as the bare list, so the body stays a bare JSON array
     * in EVERY case, skip or no skip -- verified here by parsing the body
     * directly as a {@code List} even when a name was dropped.
     */
    @Test
    void hybridSearch_overHttp_fanOutSkipsUnregistered_headerNamesDropped_bodyStaysBareArray()
            throws Exception {
        String ghost = "knowledge__httph-ghost__voyage-context-3__v1";
        var resp = post(service, TOKEN_A,
            Map.of("query", Q, "collections", List.of(COL, ghost), "n_results", 10));

        assertThat(resp.statusCode())
            .as("a fan-out with one dropped name still succeeds (got: %s)", resp.body())
            .isEqualTo(200);
        assertThat(resp.headers().firstValue(VectorHandler.SKIPPED_COLLECTIONS_HEADER))
            .as("the dropped name must be visible via the header")
            .contains(ghost);
        List<Map<String, Object>> rows = MAPPER.readValue(resp.body(), List.class);
        assertThat(rows.stream().map(r -> r.get("id")).toList())
            .as("the body stays a bare array -- the registered collection's fused "
                + "hits must still come back, with no envelope wrapping")
            .containsExactly(HH_C1, HH_C2);
    }

    /**
     * Header ABSENT (not empty) when nothing was dropped -- the common case,
     * which {@link #hybridSearch_overHttp_returnsFusedRows} above already
     * exercises for the body shape; this pins the header side of the same call.
     */
    @Test
    void hybridSearch_overHttp_fullyRegisteredFanOut_headerAbsent() throws Exception {
        var resp = post(service, TOKEN_A,
            Map.of("query", Q, "collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(resp.headers().firstValue(VectorHandler.SKIPPED_COLLECTIONS_HEADER))
            .as("nothing was dropped -- the header must be absent, not empty")
            .isEmpty();
    }

    @Test
    void hybridSearch_missingQuery_returns400() throws Exception {
        var resp = post(service, TOKEN_A,
            Map.of("collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode()).as("missing 'query' is a client error").isEqualTo(400);
    }
}
