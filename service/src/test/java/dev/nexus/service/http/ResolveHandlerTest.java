// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
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
 * RDR-169 G3 (bead nexus-aphki) — HTTP-boundary contract for
 * {@code POST /v1/vectors/resolve} ({@link ResolveHandler}), wired live from
 * {@link NexusService}'s constructor into a real {@link
 * dev.nexus.service.resolver.UriSchemeResolverRegistry} backed by a real
 * {@link PgVectorRepository}.
 *
 * <p>Mirrors {@code VectorHandlerUpsertReferenceOnlyTest}'s bootstrap
 * (Testcontainers PG, {@link PgContainerHelper#applyProductSchema} +
 * {@link PgContainerHelper#bootstrapServiceRole} + {@link
 * PgContainerHelper#seedServiceToken} — no raw SQL strings in Java, nexus-cbo4a),
 * {@code PgVectorRepository} injected via the 5-arg {@link NexusService}
 * overload, port 0, {@code PER_CLASS}. Two tenants (two bearer tokens, one
 * shared service role/datasource — RLS, not connection separation, is what is
 * under test for tenant isolation).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ResolveHandlerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN_A  = "tok-resolve-tenant-a-0123456789abcdef00000";
    private static final String TOKEN_B  = "tok-resolve-tenant-b-0123456789abcdef00000";
    private static final String SVC_ROLE = "svc_resolve";
    private static final String SVC_PASS = "svc_resolve_pass";
    private static final String TENANT_A = "resolve-tenant-a";
    private static final String TENANT_B = "resolve-tenant-b";
    private static final String COLLECTION = "knowledge__resolve-owner__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    PgVectorRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN_A, TENANT_A, "resolve-test-a");
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN_B, TENANT_B, "resolve-test-b");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        Embedder embedder = new StubEmbedder(1024);
        repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        // 5-arg overload: pgVectorRepository non-null, so NexusService's constructor
        // registers "chroma" alongside the unconditional "https" (see NexusService's
        // /v1/vectors/resolve wiring comment) — this is the live-wiring path under test.
        service = new NexusService(0, TOKEN_A, svcDs, null, repo);
        service.start();
        http = HttpClient.newHttpClient();

        // RDR-204 Phase 1 (bead nexus-ft04v.3): burn the per-tenant ghost sweep on
        // each tenant's FIRST request before registering any collection (measured
        // ordering trap, see VectorHandlerDeadlineMappingTest's identical comment).
        for (String token : List.of(TOKEN_A, TOKEN_B)) {
            var warmup = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list"))
                .header("Authorization", "Bearer " + token)
                .GET().build();
            http.send(warmup, HttpResponse.BodyHandlers.ofString());
        }

        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION);
            PgContainerHelper.insertCollection(ctx, TENANT_B, COLLECTION);
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String token, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/vectors/resolve"))
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonBody(HttpResponse<String> resp) throws Exception {
        return MAPPER.readValue(resp.body(), Map.class);
    }

    // -------------------------------------------------------------------------
    // chroma:// resolves a real seeded chunk's text
    // -------------------------------------------------------------------------

    @Test
    void chromaSourceUri_resolvesRealSeededChunk() throws Exception {
        String chash = Chash.ofText("resolve-chroma-direct").toHex();
        repo.upsertChunks(TENANT_A, COLLECTION,
            List.of(chash), List.of("full content seeded for direct chroma resolution"),
            List.of(Map.of()));

        String uri = "chroma://" + COLLECTION + "/" + chash;
        var resp = post(TOKEN_A, Map.of("source_uri", uri));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("content")).isEqualTo("full content seeded for direct chroma resolution");
        assertThat(body.get("source_uri")).isEqualTo(uri);
        // source_uri form: no row was looked up, so no retention to report.
        assertThat(body).doesNotContainKey("retention");
    }

    // -------------------------------------------------------------------------
    // (collection, chash) lookup of a reference-only row resolves the FULL
    // chunk its metadata.source_uri points at, and reports its OWN retention
    // -------------------------------------------------------------------------

    @Test
    void chashLookup_ofReferenceOnlyRow_resolvesTargetContent_retentionReferenceOnly() throws Exception {
        String fullChash = Chash.ofText("resolve-full-target").toHex();
        repo.upsertChunks(TENANT_A, COLLECTION,
            List.of(fullChash), List.of("the real content a reference-only row points at"),
            List.of(Map.of()));
        String fullUri = "chroma://" + COLLECTION + "/" + fullChash;

        String refChash = Chash.ofText("resolve-reference-only-row").toHex();
        repo.upsertReferenceOnlyChunk(TENANT_A, COLLECTION, refChash,
            StubEmbedder.unitVector(1024),
            Map.of("source_uri", fullUri));

        var resp = post(TOKEN_A, Map.of("collection", COLLECTION, "chash", refChash));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("content")).isEqualTo("the real content a reference-only row points at");
        assertThat(body.get("source_uri")).isEqualTo(fullUri);
        assertThat(body.get("retention")).isEqualTo("reference-only");
    }

    // -------------------------------------------------------------------------
    // Unregistered scheme -> 422 naming it
    // -------------------------------------------------------------------------

    @Test
    @SuppressWarnings("unchecked")
    void unregisteredScheme_returns422_namingItAndTheRegisteredSet() throws Exception {
        var resp = post(TOKEN_A, Map.of("source_uri", "file:///etc/passwd"));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(422);
        Map<String, Object> body = jsonBody(resp);
        assertThat((String) body.get("error")).contains("file");
        assertThat(body.get("scheme")).isEqualTo("file");
        assertThat((List<String>) body.get("registered_schemes"))
            .contains("chroma", "https");
    }

    // -------------------------------------------------------------------------
    // Unknown chash -> 404
    // -------------------------------------------------------------------------

    @Test
    void unknownChash_returns404() throws Exception {
        String chash = Chash.ofText("resolve-does-not-exist").toHex();
        var resp = post(TOKEN_A, Map.of("collection", COLLECTION, "chash", chash));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(404);
    }

    // -------------------------------------------------------------------------
    // Tenant isolation: tenant A cannot resolve a chash of tenant B
    // -------------------------------------------------------------------------

    @Test
    void tenantA_cannotResolveTenantBsChash() throws Exception {
        String chash = Chash.ofText("resolve-cross-tenant-chash").toHex();
        repo.upsertChunks(TENANT_B, COLLECTION,
            List.of(chash), List.of("tenant B's private content"),
            List.of(Map.of()));

        var resp = post(TOKEN_A, Map.of("collection", COLLECTION, "chash", chash));

        assertThat(resp.statusCode())
            .as("RLS must make tenant B's row invisible to tenant A's lookup (got body: %s)",
                resp.body())
            .isEqualTo(404);
    }

    /**
     * Deterministic, dependency-free embedder: every text maps to the same unit
     * vector. This test never asserts on vector similarity/ranking — only on
     * presence/absence and content by exact chash — so a constant vector is
     * sufficient and keeps this file free of any cross-package dependency on the
     * {@code dev.nexus.service} package's package-private {@code FakeEmbedder}.
     */
    private static final class StubEmbedder implements Embedder {

        private final int dim;

        StubEmbedder(int dim) {
            this.dim = dim;
        }

        static float[] unitVector(int dim) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return v;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new java.util.ArrayList<>(texts.size());
            for (String ignored : texts) {
                out.add(unitVector(dim));
            }
            return out;
        }

        @Override
        public void close() {
            // no resources
        }
    }
}
