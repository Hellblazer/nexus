// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.http.EmbedPosture;
import dev.nexus.service.vectors.EmbeddingProfile;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 2 Step 4 (bead nexus-un7vj): the boot posture reaches the wire through the REAL
 * {@code NexusService} routes. {@code EmbedPostureHandlersTest} proves the handlers render a supplied posture;
 * this proves the service hands BOTH its {@code /version} and {@code /v1/status} handlers the late-bound source:
 * the three keys are absent until {@code Main} calls {@code embedPosture(...)}, present on both routes after, and
 * a re-bound posture (not a copy taken at binding time) is served on the next request.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceEmbedPostureWiringTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String[] KEYS = {"embedding_profile", "reranker", "embedding_degraded"};

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, "embed-posture-wiring-token", ds);
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (service != null) {
            try {
                service.stop();
            } catch (Exception ignored) {
                // best effort
            }
        }
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private JsonNode get(String path) throws Exception {
        HttpResponse<String> resp = http.send(
            HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + service.getPort() + path)).GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as(path).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    @Test
    void theThreeKeysAreAbsentOnBothRoutesUntilMainBindsAPosture_thenServedFromTheBoundOne() throws Exception {
        for (String path : new String[] {"/version", "/v1/status"}) {
            JsonNode before = get(path);
            for (String key : KEYS) {
                assertThat(before.has(key)).as("%s unbound: %s absent (cannot tell)", path, key).isFalse();
            }
        }

        service.embedPosture(new EmbedPosture("voyage", "rerank-2.5", null));
        for (String path : new String[] {"/version", "/v1/status"}) {
            JsonNode body = get(path);
            assertThat(body.get("embedding_profile").asText()).as(path).isEqualTo("voyage");
            assertThat(body.get("reranker").asText()).as(path).isEqualTo("rerank-2.5");
            assertThat(body.has("embedding_degraded")).as(path).isTrue();
            assertThat(body.get("embedding_degraded").isNull()).as(path).isTrue();
        }

        var degradation = new EmbeddingProfile.Degradation(EmbeddingProfile.BGE,
            "the bge-768 model files are missing", "the bge model files are missing: /x/model.onnx",
            "run `nx init --service`, then restart the engine", EmbeddingProfile.VOYAGE, "bge");
        service.embedPosture(new EmbedPosture("bge", "ms-marco-minilm-l6-v2", degradation));
        for (String path : new String[] {"/version", "/v1/status"}) {
            JsonNode body = get(path);
            assertThat(body.get("embedding_profile").asText()).as("%s re-bound, read per request", path).isEqualTo("bge");
            assertThat(body.get("reranker").asText()).as(path).isEqualTo("ms-marco-minilm-l6-v2");
            assertThat(body.at("/embedding_degraded/chosen").asText()).as(path).isEqualTo("bge");
            assertThat(body.at("/embedding_degraded/missing").asText()).as(path)
                .isEqualTo("the bge-768 model files are missing");
            assertThat(body.toString()).as("%s: the log-only detail never reaches the wire", path)
                .doesNotContain("/x/model.onnx");
        }

        service.embedPosture(null);
        for (String path : new String[] {"/version", "/v1/status"}) {
            assertThat(get(path).has("embedding_profile")).as("%s unbound again", path).isFalse();
        }
    }
}
