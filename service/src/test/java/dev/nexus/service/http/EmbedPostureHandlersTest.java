// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.vectors.AdmissionControlledReranker;
import dev.nexus.service.vectors.BootPlan;
import dev.nexus.service.vectors.CrossEncoderReranker;
import dev.nexus.service.vectors.EmbedActivitySnapshot;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.EmbeddingProfile;
import dev.nexus.service.vectors.LocalModelProbe;
import dev.nexus.service.vectors.LocalOnnxAdmission;
import dev.nexus.service.vectors.Reranker;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import javax.sql.DataSource;
import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.SQLException;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

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
        return reranker(token, new AtomicBoolean(true));
    }

    private static Reranker reranker(String token, AtomicBoolean available) {
        return new Reranker() {
            @Override public String modelToken() { return token; }
            @Override public boolean available() { return available.get(); }
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
        assertThat(degraded.get("missing").asText()).as("by role, no absolute path on the wire")
                .isEqualTo("the bge-768 model file is missing");
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

    @Test
    void status_rerankerAvailable_isLive_namesTheRerankerEitherWay_andFlipsWithoutARestart() throws Exception {
        var present = new AtomicBoolean(false);
        var plan = BootPlan.decide(presence(true, true, false), EmbeddingProfile.BGE);
        EmbedPosture posture = EmbedPosture.of(plan, reranker("ms-marco-minilm-l6-v2", present));
        var handler = new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.BGE)).embedPosture(() -> posture);

        JsonNode absent = get("/v1/status", handler);
        assertThat(absent.get("reranker").asText()).as("the chosen reranker is still named")
                .isEqualTo("ms-marco-minilm-l6-v2");
        assertThat(absent.get("reranker_available").isBoolean()).isTrue();
        assertThat(absent.get("reranker_available").asBoolean()).as("files absent").isFalse();
        server.stop(0);

        present.set(true);   // the same posture object, the same handler: no rebuild, no restart
        JsonNode loaded = get("/v1/status", handler);
        assertThat(loaded.get("reranker_available").asBoolean()).as("files appeared").isTrue();
        assertThat(loaded.get("reranker").asText()).isEqualTo("ms-marco-minilm-l6-v2");
    }

    @Test
    void reranker_available_isABareBoolean_andAddsNoPathOrProbeTextToTheWire() throws Exception {
        var plan = BootPlan.decide(presence(true, true, false), EmbeddingProfile.BGE);
        var handler = new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.BGE))
                .embedPosture(() -> EmbedPosture.of(plan, reranker("ms-marco-minilm-l6-v2", new AtomicBoolean(false))));

        String raw = get("/v1/status", handler).toString();

        assertThat(raw).contains("\"reranker_available\":false");
        assertThat(raw).doesNotContain("model.onnx").doesNotContain("tokenizer.json").doesNotContain("/models/");
    }

    @Test
    void theReranker_asBootPlanBuildsIt_overMissingFiles_rendersRerankerAvailableFalse_andTrueOncePresent(
            @TempDir Path dir) throws Exception {
        // The production path: BootPlan.buildReranker wraps the cross-encoder in AdmissionControlledReranker,
        // and EmbedPosture.of asks that wrapper. Nothing in this test stubs available().
        var plan = BootPlan.decide(presence(true, true, false), EmbeddingProfile.BGE);
        Path model = dir.resolve("onnx/model.onnx");
        Path tokenizer = dir.resolve("onnx/tokenizer.json");
        Reranker built = plan.buildReranker(
                () -> { throw new AssertionError("the Voyage reranker must not be built"); },
                () -> new CrossEncoderReranker(model.toString(), tokenizer.toString()),
                new LocalOnnxAdmission(1, 1000));
        assertThat(built).isInstanceOf(AdmissionControlledReranker.class);

        EmbedPosture posture = EmbedPosture.of(plan, built);
        var handler = new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.BGE)).embedPosture(() -> posture);

        String raw = get("/v1/status", handler).toString();
        assertThat(raw).contains("\"reranker\":\"ms-marco-minilm-l6-v2\"").contains("\"reranker_available\":false");
        server.stop(0);

        Files.createDirectories(model.getParent());
        Files.writeString(model, "m");
        Files.writeString(tokenizer, "t");
        assertThat(get("/v1/status", handler).get("reranker_available").asBoolean())
                .as("the same wrapped reranker, files now present").isTrue();
    }

    @Test
    void aPostureWithNoReranker_reportsNullAndNotAvailable() throws Exception {
        var plan = BootPlan.decide(presence(true, true, true), EmbeddingProfile.BGE);
        var handler = new StatusHandler(router(fakeBge(), "pa-key", EmbeddingProfile.BGE))
                .embedPosture(() -> EmbedPosture.of(plan, null));

        JsonNode body = get("/v1/status", handler);

        assertThat(body.get("reranker").isNull()).isTrue();
        assertThat(body.get("reranker_available").asBoolean()).isFalse();
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
    void version_throughTheRealHandler_advertisesTheRunSummaryCapabilityBesideTheOlderTwo() throws Exception {
        // RDR-232 P3 (nexus-jypmj.3.4): the fragment method is unit-tested in
        // VersionHandlerReleaseVersionTest; this proves the handler's body actually calls it.
        JsonNode body = get("/version", new VersionHandler(refusingPool(),
                router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.get("nx_answer_run_summary_supported").asBoolean()).isTrue();
        assertThat(body.get("nx_answer_steps_supported").asBoolean()).isTrue();
        assertThat(body.get("nx_answer_run_complete_supported").asBoolean()).isTrue();
    }

    @Test
    void version_throughTheRealHandler_advertisesTheGraphScopeFilterCapability() throws Exception {
        // nexus-jypmj.1.13: the fragment method is unit-tested in VersionHandlerReleaseVersionTest; this
        // proves the handler's body actually calls it.
        JsonNode body = get("/version", new VersionHandler(refusingPool(),
                router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.get("catalog_graph_scope_filter_supported").asBoolean()).isTrue();
        // nexus-jypmj.18: the node-walk flag rides the same handler body.
        assertThat(body.get("catalog_graph_node_walk_supported").asBoolean()).isTrue();
    }

    @Test
    void version_throughTheRealHandler_advertisesTheManifestRowLimitAndOutOfStepCostCapabilities() throws Exception {
        // nexus-jypmj.6 and nexus-jypmj.5: the fragment methods are unit-tested in
        // VersionHandlerReleaseVersionTest; this proves the handler's body actually calls them.
        JsonNode body = get("/version", new VersionHandler(refusingPool(),
                router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.get("catalog_manifest_row_limit_supported").asBoolean()).isTrue();
        assertThat(body.get("nx_answer_out_of_step_cost_supported").asBoolean()).isTrue();
    }

    @Test
    void version_throughTheRealHandler_advertisesTheSearchRowOwnerSpanCapability() throws Exception {
        // nexus-6pfgb.26: the fragment method is unit-tested in VersionHandlerReleaseVersionTest; this proves
        // the handler's body actually calls it.
        JsonNode body = get("/version", new VersionHandler(refusingPool(),
                router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.get("search_row_owner_span_supported").asBoolean()).isTrue();
    }

    @Test
    void version_throughTheRealHandler_advertisesTheChashAllowCapability() throws Exception {
        // nexus-jypmj: the fragment method is unit-tested in VersionHandlerReleaseVersionTest; this proves the
        // handler's body actually calls it.
        JsonNode body = get("/version", new VersionHandler(refusingPool(),
                router(fakeBge(), "pa-key", EmbeddingProfile.VOYAGE)));
        assertThat(body.get("vector_search_chash_allow_supported").asBoolean()).isTrue();
    }

    @Test
    void theTwoRoutesRenderTheSameThreeKeys() throws Exception {
        EmbedPosture posture = posture(false, true, EmbeddingProfile.BGE);
        JsonNode status = get("/v1/status", new StatusHandler(router(null, "pa-key", EmbeddingProfile.BGE))
                .embedPosture(() -> posture));
        server.stop(0);
        JsonNode version = get("/version", new VersionHandler(refusingPool(),
                router(null, "pa-key", EmbeddingProfile.BGE)).embedPosture(() -> posture));
        for (String key : List.of("embedding_profile", "reranker", "reranker_available", "embedding_degraded")) {
            assertThat(version.get(key)).as(key).isEqualTo(status.get(key));
        }
    }
}
