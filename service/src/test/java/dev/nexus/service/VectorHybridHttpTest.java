// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
 * suites ({@code PgVectorHybridSearchContractTest}, {@code HybridParityIntegrationTest});
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }
        // Bind one token per tenant (server-side tenant resolution under test).
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            for (var bound : List.of(Map.entry(TOKEN_A, TENANT_A),
                                     Map.entry(TOKEN_B, TENANT_B))) {
                ps.setString(1, TokenHashing.sha256Hex(bound.getKey()));
                ps.setString(2, bound.getValue());
                ps.setString(3, "hybrid-http-test");
                ps.executeUpdate();
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
        // TOKEN_B is bound to TENANT_B, which owns no rows: the same request body
        // must return zero rows. RLS + server-resolved tenant is the boundary; there
        // is no client-supplied field that can widen it.
        var resp = post(service, TOKEN_B,
            Map.of("query", Q, "collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode()).isEqualTo(200);
        List<?> rows = MAPPER.readValue(resp.body(), List.class);
        assertThat(rows)
            .as("a bearer bound to another tenant sees exactly 0 rows")
            .isEmpty();
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

    @Test
    void hybridSearch_missingQuery_returns400() throws Exception {
        var resp = post(service, TOKEN_A,
            Map.of("collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode()).as("missing 'query' is a client error").isEqualTo(400);
    }
}
