// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.vectors.BootPlan;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.EmbeddingProfile;
import dev.nexus.service.vectors.LocalModelProbe;
import dev.nexus.service.vectors.Reranker;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.SQLException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 2 Step 4 (bead nexus-un7vj) — {@code GET /version} and {@code GET /v1/status} report the boot
 * posture (profile, reranker, degraded-boot description) beside the embedding mode, in the dual shape and the
 * two single-family shapes. Hermetic: bare {@link HttpServer}s bound to the handlers, no Postgres. The version
 * handler's schema read is pointed at a pool that refuses, which this suite does not assert on (the
 * {@code schema_error} path is its own contract).
 */
class EmbedPostureHandlersTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String BGE = "bge-base-en-v15-768";

    private HttpServer server;
    private final HttpClient http = HttpClient.newHttpClient();
    private final List<EmbedderRouter> routers = new java.util.ArrayList<>();

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
        routers.forEach(EmbedderRouter::close);
    }

    private static Embedder fakeBge() {
        return new Embedder() {
            @Override public String modelToken() { return BGE; }
            @Override public List<float[]> embed(List<String> texts) { return List.of(); }
        };
    }

    private EmbedderRouter router(Embedder local, String key, EmbeddingProfile profile) {
        var r = new EmbedderRouter(local, key, "document", profile);
        routers.add(r);
        return r;
    }

    private static DataSource refusingPool() {
        return (DataSource) Proxy.newProxyInstance(EmbedPostureHandlersTest.class.getClassLoader(),
                new Class<?>[] {DataSource.class}, (proxy, method, args) -> {
                    if (method.getName().equals("getConnection")) throw new SQLException("no pool in this test");
                    throw new UnsupportedOperationException(method.getName());
                });
    }

    private JsonNode get(String path, HttpHandler handler) throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext(path, handler);
        server.start();
        HttpResponse<String> resp = http.send(HttpRequest.newBuilder(URI.create(
                "http://127.0.0.1:" + server.getAddress().getPort() + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private static Reranker reranker(String token) {
        return new Reranker() {
            @Override public String modelToken() { return token; }
            @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) { return List.of(); }
        };
    }

    private static LocalModelProbe.Presence presence(boolean bge, boolean key, boolean crossEncoder) {
        return new LocalModelProbe.Presence(bge, false, key, "bge-detail",
                bge ? List.of() : List.of("/models/bge/model.onnx"), crossEncoder, "ce", List.of());
    }

    /** The posture a boot with this presence and profile reports, with the reranker it would have built. */
    private static EmbedPosture posture(boolean bge, boolean key, EmbeddingProfile profile) {
        var plan = BootPlan.decide(presence(bge, key, true), profile);
        return EmbedPosture.of(plan, reranker(plan.buildsCrossEncoder() ? "ms-marco-minilm-l6-v2" : "rerank-2.5"));
    }

    // ── /v1/status ─────────────────────────────────────────────────────────────────────────────

    @Test
    void status_dualShape_reportsDualModeTheProfileTheRerankerAndNoDegradation() throws Exception {
        var handler = new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE),
                () -> new EmbedActivitySnapshot(true, 7L, 1L, 2.0, 5L, 0, 4, 0L, 0L))
                .embedPosture(() -> posture(true, true, EmbeddingProfile.VOYAGE));

        JsonNode body = get("/v1/status", handler);

        assertThat(body.get("embedding_mode").asText()).isEqualTo("dual");
        assertThat(body.get("embedding_profile").asText()).isEqualTo("voyage");
        assertThat(body.get("reranker").asText()).isEqualTo("rerank-2.5");
        assertThat(body.get("embedding_degraded").isNull()).isTrue();
        assertThat(body.get("local_embed_activity").get("chunks_done_total").asLong())
                .as("local_embed_activity is reported whenever bge is built, dual included").isEqualTo(7L);
    }

    @Test
    void status_aKeyedBgeProfileWithNoBgeFiles_reportsVoyageOnlyModeBgeProfileAndTheDegradation() throws Exception {
        var handler = new StatusHandler(router(null, "pa-key", EmbeddingProfile.BGE))
                .embedPosture(() -> posture(false, true, EmbeddingProfile.BGE));

        JsonNode body = get("/v1/status", handler);

        assertThat(body.get("embedding_mode").asText()).isEqualTo("voyage");
        assertThat(body.get("embedding_profile").asText()).isEqualTo("bge");
        assertThat(body.get("reranker").asText()).as("no rerank text goes to Voyage")
                .isEqualTo("ms-marco-minilm-l6-v2");
        JsonNode degraded = body.get("embedding_degraded");
        assertThat(degraded.get("chosen").asText()).isEqualTo("bge");
        assertThat(degraded.get("serving").asText()).isEqualTo("voyage");
        assertThat(degraded.get("missing").asText()).contains("/models/bge/model.onnx");
        assertThat(degraded.get("remedy").asText()).contains("`nx init --service`");
        assertThat(body.get("local_embed_activity").isNull()).isTrue();
    }

    @Test
    void status_aVoyageProfileWithNoKey_reportsOnnxLocalModeAndTheKeyAsMissing() throws Exception {
        var handler = new StatusHandler(router(fakeBge(), null, EmbeddingProfile.VOYAGE))
                .embedPosture(() -> posture(true, false, EmbeddingProfile.VOYAGE));

        JsonNode body = get("/v1/status", handler);

        assertThat(body.get("embedding_mode").asText()).isEqualTo("onnx-local");
        assertThat(body.get("embedding_profile").asText()).isEqualTo("voyage");
        assertThat(body.get("reranker").asText()).isEqualTo("ms-marco-minilm-l6-v2");
        assertThat(body.get("embedding_degraded").get("chosen").asText()).isEqualTo("voyage");
        assertThat(body.get("embedding_degraded").get("missing").asText()).contains("NX_VOYAGE_API_KEY");
    }

    @Test
    void status_withNoPostureBound_omitsTheThreeKeys_anOlderWiringReadsAsCannotTell() throws Exception {
        JsonNode body = get("/v1/status", new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.has("embedding_profile")).isFalse();
        assertThat(body.has("reranker")).isFalse();
        assertThat(body.has("embedding_degraded")).isFalse();
        assertThat(body.get("embedding_mode").asText()).isEqualTo("dual");
    }

    @Test
    void status_aSupplierThatReturnsNull_omitsTheThreeKeys() throws Exception {
        JsonNode body = get("/v1/status", new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE))
                .embedPosture(() -> null));
        assertThat(body.has("embedding_profile")).isFalse();
    }

    // ── /version ───────────────────────────────────────────────────────────────────────────────

    @Test
    void version_dualShape_reportsDualModeBothModelFamiliesTheProfileAndTheReranker() throws Exception {
        var router = router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE);
        var handler = new VersionHandler(refusingPool(), router)
                .embedPosture(() -> posture(true, true, EmbeddingProfile.VOYAGE));

        JsonNode body = get("/version", handler);

        assertThat(body.get("embedding_mode").asText()).isEqualTo("dual");
        List<String> models = new java.util.ArrayList<>();
        body.get("embedding_models").forEach(n -> models.add(n.asText()));
        assertThat(models).contains(BGE, "voyage-code-3", "voyage-context-3");
        assertThat(body.get("embedding_profile").asText()).isEqualTo("voyage");
        assertThat(body.get("reranker").asText()).isEqualTo("rerank-2.5");
        assertThat(body.get("embedding_degraded").isNull()).isTrue();
    }

    @Test
    void version_aDegradedBoot_carriesTheDegradationSoADoctorRowCanCompareItToEmbeddingModels() throws Exception {
        var router = router(null, "pa-key", EmbeddingProfile.BGE);
        var handler = new VersionHandler(refusingPool(), router)
                .embedPosture(() -> posture(false, true, EmbeddingProfile.BGE));

        JsonNode body = get("/version", handler);

        List<String> models = new java.util.ArrayList<>();
        body.get("embedding_models").forEach(n -> models.add(n.asText()));
        assertThat(models).as("only Voyage is served").doesNotContain(BGE);
        assertThat(body.get("embedding_profile").asText()).as("the profile still names the unserved family")
                .isEqualTo("bge");
        assertThat(body.get("embedding_degraded").get("chosen").asText()).isEqualTo("bge");
        assertThat(body.get("embedding_degraded").get("remedy").asText()).contains("`nx init --service`");
    }

    @Test
    void version_bgeOnlyShape_reportsOnnxLocal() throws Exception {
        var router = router(fakeBge(), null, EmbeddingProfile.BGE);
        var handler = new VersionHandler(refusingPool(), router)
                .embedPosture(() -> posture(true, false, EmbeddingProfile.BGE));

        JsonNode body = get("/version", handler);

        assertThat(body.get("embedding_mode").asText()).isEqualTo("onnx-local");
        assertThat(body.get("embedding_profile").asText()).isEqualTo("bge");
        assertThat(body.get("embedding_degraded").isNull()).isTrue();
    }

    @Test
    void version_withNoPostureBound_omitsTheThreeKeys() throws Exception {
        JsonNode body = get("/version", new VersionHandler(refusingPool(),
                router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.has("embedding_profile")).isFalse();
        assertThat(body.has("reranker")).isFalse();
        assertThat(body.has("embedding_degraded")).isFalse();
        assertThat(body.get("embedding_mode").asText()).isEqualTo("dual");
    }

    @Test
    void theTwoRoutesRenderTheSameThreeKeys() throws Exception {
        EmbedPosture posture = posture(false, true, EmbeddingProfile.BGE);
        JsonNode status = get("/v1/status", new StatusHandler(router(null, "pa-key", EmbeddingProfile.BGE))
                .embedPosture(() -> posture));
        server.stop(0);
        JsonNode version = get("/version", new VersionHandler(refusingPool(),
                router(null, "pa-key", EmbeddingProfile.BGE)).embedPosture(() -> posture));
        for (String key : List.of("embedding_profile", "reranker", "embedding_degraded")) {
            assertThat(version.get(key)).as(key).isEqualTo(status.get(key));
        }
    }
}
