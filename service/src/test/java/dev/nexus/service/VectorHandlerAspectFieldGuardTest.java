// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
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
 * RDR-156 Decision 5 (bead nexus-ubnwk), round-1 review fix (code-review-expert +
 * substantive-critic Critical): {@code POST /v1/vectors/search-aspect-scoped} with
 * {@code field="extras"} or {@code field="salient_sentences"} must 400 at the REAL
 * HTTP boundary — before this fix, {@code PgVectorRepository.ASPECT_SCOPED_FIELD_
 * ALLOWLIST} still listed both (stale, pre-narrowing), so {@link VectorHandler}'s
 * 400-guard (which reads that same Java set) let them through, the request reached
 * the SQL function, and the CASE's fallthrough-to-NULL silently returned an empty
 * result set with a 200 instead of rejecting the request.
 *
 * <p>Deliberately exercises the actual route/repository, not raw SQL — the
 * {@code CombinedQueryParityTest} GROUP 10 tests call
 * {@code nexus.search_aspect_scoped_<dim>} directly via hand-built SQL (same pattern
 * as the sibling combined-query shapes) and so never touch this Java layer at all,
 * which is exactly why the round-1 drift was invisible to that suite. Mirrors
 * {@link VectorHandlerCombinedQueryModelGuardTest}'s lightweight harness (Testcontainers
 * PG, full Liquibase changelog, a stub {@link Embedder}, {@code PgVectorRepository}
 * injected via the 5-arg {@link NexusService} overload, port 0, {@code PER_CLASS}) —
 * the guard throws before any embed call or DB touch, so no aspect/chunk seeding is
 * required for the 400 path.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerAspectFieldGuardTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "tok-aspect-field-guard-test-0123456789abcd";
    private static final String TENANT = "aspect-field-guard-tenant";
    private static final String COLL   = "knowledge__afg-a__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.setString(3, "aspect-field-guard-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Stub embedder: the field guard throws before this is ever invoked.
        var embedder = new StubEmbedder(1024);
        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        service = new NexusService(0, TOKEN, svcDs, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void searchAspectScoped_fieldExtras_returns400NotSilentEmptyResult() throws Exception {
        var resp = post("/v1/vectors/search-aspect-scoped", Map.of(
            "query",       "probe",
            "collections", List.of(COLL),
            "field",       "extras",
            "pattern",     "anything",
            "n_results",   5));

        assertThat(resp.statusCode())
            .as("field=extras must 400 — it is jsonb (aspects-003-type-hygiene.xml), "
                + "has no branch in the SQL CASE, and must never silently resolve to "
                + "an empty result set: %s", resp.body())
            .isEqualTo(400);
        assertThat(resp.body())
            .as("the rejection must name the offending field")
            .contains("extras");
    }

    @Test
    void searchAspectScoped_fieldSalientSentences_returns400NotSilentEmptyResult() throws Exception {
        var resp = post("/v1/vectors/search-aspect-scoped", Map.of(
            "query",       "probe",
            "collections", List.of(COLL),
            "field",       "salient_sentences",
            "pattern",     "anything",
            "n_results",   5));

        assertThat(resp.statusCode())
            .as("field=salient_sentences must 400 for the identical reason as "
                + "field=extras: %s", resp.body())
            .isEqualTo(400);
    }

    @Test
    void searchAspectScoped_knownField_doesNotHitThe400Guard() throws Exception {
        // Non-regression: a genuinely allowlisted field must NOT 400 on the field
        // check (it may still fail later in the pipeline for unrelated reasons —
        // e.g. no matching rows — but never via the 400-guard this test targets).
        var resp = post("/v1/vectors/search-aspect-scoped", Map.of(
            "query",       "probe",
            "collections", List.of(COLL),
            "field",       "proposed_method",
            "pattern",     "anything",
            "n_results",   5));

        assertThat(resp.statusCode())
            .as("field=proposed_method is allowlisted and must pass the field guard "
                + "(200 — an empty collection returns an empty result, not an error): %s",
                resp.body())
            .isEqualTo(200);
    }

    /** Minimal fixed-vector stub embedder — the field guard never reaches embed() here. */
    private static final class StubEmbedder implements Embedder {
        private final int dim;

        StubEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public String modelToken() {
            return "voyage-context-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new java.util.ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[dim];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), 0L);
        }
    }
}
