// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.RequestDeadlineExceededException;
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
 * nexus-8hdg9 phase 2 (write-path cancellation on client disconnect) --
 * HTTP-boundary contract for {@link RequestDeadlineExceededException}: it
 * must map to 503, not the generic 500 arm, and -- unlike {@code
 * VoyageTooManyTokensException} (422, deliberately OUTSIDE the client's
 * gateway-retry ladder) -- 503 IS one of the client's {@code
 * _GATEWAY_RETRY_CODES} {502,503,504}, since a request that ran out of its
 * own deadline is an honest slow-server signal the client should retry, not
 * a guaranteed-to-fail-again oversize body.
 *
 * <p>Mirrors {@code CatalogHandlerCollectionCountsTest}'s converted bootstrap
 * (Testcontainers PG, {@link PgContainerHelper#applyProductSchema} +
 * {@link PgContainerHelper#bootstrapServiceRole} + {@link
 * PgContainerHelper#seedServiceToken} -- Sam's no-raw-SQL-strings-in-Java
 * directive, nexus-zrcj7/nexus-cbo4a) with a stub {@link Embedder} that
 * throws the typed exception directly, {@code PgVectorRepository} injected
 * via the 5-arg {@link NexusService} overload, port 0, {@code PER_CLASS} --
 * this phase adds only the exception and its mapping, no embed-loop check
 * point raises it yet (phases 3/4, nexus-8hdg9.3/.4), so a stub embedder
 * throwing directly is the smallest fixture that exercises the real {@code
 * VectorHandler.handle} catch chain end-to-end over HTTP.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerDeadlineMappingTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN    = "tok-rdln-test-0123456789abcdef00000000";
    private static final String SVC_ROLE = "svc_rdln";
    private static final String SVC_PASS = "svc_rdln_pass";
    private static final String TENANT   = "rdln-tenant";
    private static final String COLLECTION = "code__rdln__voyage-code-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "rdln-test");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Stub embedder: always throws the typed exception directly -- VectorHandler's
        // catch chain is under test, not a real embed-loop check point (those are
        // phases 3/4, out of scope for this phase).
        var embedder = new ThrowingEmbedder();
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
    void upsertChunks_requestDeadlineExceeded_maps503_neverOpaque500() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", COLLECTION,
            "ids",        List.of(Chash.ofText("rdln-c1").toHex()),
            "documents",  List.of("chunk that will simulate an expired write-path deadline"),
            "metadatas",  List.of(Map.of())));

        assertThat(resp.statusCode())
            .as("must be 503 (an honest slow-server signal, one of the client's"
                + " _GATEWAY_RETRY_CODES) -- never the generic 500 arm (got body: %s)",
                resp.body())
            .isEqualTo(503);

        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        assertThat((String) body.get("error"))
            .as("the typed detail must reach the caller, not just the engine log")
            .contains("rdln-simulated-deadline-exceeded");
    }

    /** Always throws the typed exception, simulating an expired write-path deadline. */
    private static final class ThrowingEmbedder implements Embedder {
        @Override
        public String modelToken() {
            return "voyage-code-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            throw simulated();
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            throw simulated();
        }

        private static RequestDeadlineExceededException simulated() {
            return new RequestDeadlineExceededException(
                "rdln-simulated-deadline-exceeded: write-path deadline elapsed mid-embed");
        }
    }
}
