// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.EmbedderRouter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-s71lr, deliverable 2 — {@code GET /v1/status}. Hermetic: a bare
 * {@link HttpServer} bound to {@link StatusHandler} directly, no {@code
 * NexusService}/DataSource/Postgres involved at all (this handler has none of
 * those dependencies), so this suite needs no substrate the fast loop lacks.
 */
class StatusHandlerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private String baseUrl;
    private final HttpClient http = HttpClient.newHttpClient();

    private void start(StatusHandler handler) throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", handler);
        server.start();
        baseUrl = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
    }

    private JsonNode get() throws Exception {
        HttpResponse<String> resp = http.send(
                HttpRequest.newBuilder(URI.create(baseUrl + "/v1/status")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    @Test
    void noRouterNoSupplier_reportsUnknownModeAndNullActivity() throws Exception {
        start(new StatusHandler(null));
        JsonNode body = get();
        assertThat(body.get("embedding_mode").asText()).isEqualTo("unknown");
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
    }

    @Test
    void withRouterNoSupplier_reportsRealModeAndNullActivity() throws Exception {
        // A trivial local Embedder wired through EmbedderRouter's local-embedder
        // constructor resolves modeName() to "onnx-local" -- exactly the /version
        // handler's own contract, reused here.
        var localEmbedder = new dev.nexus.service.vectors.Embedder() {
            @Override public String modelToken() { return "test-local"; }
            @Override public List<float[]> embed(List<String> texts) { return List.of(); }
        };
        var router = new EmbedderRouter(localEmbedder, "document");
        start(new StatusHandler(router));

        JsonNode body = get();
        assertThat(body.get("embedding_mode").asText()).isEqualTo("onnx-local");
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
    }

    @Test
    void withSupplier_reportsRealSnapshotFields() throws Exception {
        EmbedActivitySnapshot fake = new EmbedActivitySnapshot(
                true, 1024L, 64L, 7.7, 230L, 0, 4);
        start(new StatusHandler(null, () -> fake));

        JsonNode body = get();
        JsonNode activity = body.get("local_embed_activity");
        assertThat(activity.isNull()).isFalse();
        assertThat(activity.get("active").asBoolean()).isTrue();
        assertThat(activity.get("chunks_done_total").asLong()).isEqualTo(1024L);
        assertThat(activity.get("sub_batches_total").asLong()).isEqualTo(64L);
        assertThat(activity.get("last_chunks_per_sec").asDouble()).isEqualTo(7.7);
        assertThat(activity.get("last_activity_age_ms").asLong()).isEqualTo(230L);
        assertThat(activity.get("queue_depth").asInt()).isEqualTo(0);
        assertThat(activity.get("thread_width").asInt()).isEqualTo(4);
    }

    @Test
    void supplierReturningNull_reportsNullActivityNotAFabricatedValue() throws Exception {
        // A supplier is wired, but its own answer is null (e.g. a real
        // Bge768Embedder that has never embedded anything yet still returns a
        // non-null snapshot per EmbedActivityTrackerTest, but this proves the
        // handler itself never fabricates a value when the supplier truly has
        // none to give).
        start(new StatusHandler(null, () -> null));
        JsonNode body = get();
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
    }

    @Test
    void nonGetMethodIsRejected() throws Exception {
        start(new StatusHandler(null));
        HttpResponse<String> resp = http.send(
                HttpRequest.newBuilder(URI.create(baseUrl + "/v1/status"))
                        .method("POST", HttpRequest.BodyPublishers.noBody()).build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(405);
    }
}
