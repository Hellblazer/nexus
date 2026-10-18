// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-210 Phase 2 Step 1 (bead nexus-uaovy) — the router takes any PRESENT SET of
 * embedder families: bge only ({@code onnx-local}), Voyage only ({@code voyage}) or
 * both ({@code dual}).
 *
 * <p>Model-free and DB-less, like {@link EmbedderRouterBge768Test}: a {@code FakeBge}
 * stands in for the 416MB model, and {@link #mark} seeds the {@link CollectionRegistry}
 * cache so {@code resolveEmbedderStrict}'s {@code TenantScope} argument (read only on
 * a cache miss) is never dereferenced — {@code null} is passed deliberately to prove it.
 */
class EmbedderRouterPresentSetTest {

    private static final String TENANT = "embedder-router-present-set-tenant";

    private static final String BGE_TOKEN = "bge-base-en-v15-768";
    private static final String BGE_COLLECTION = "docs__nexus__bge-base-en-v15-768__v1";
    private static final String VOYAGE_COLLECTION = "docs__nexus__voyage-context-3__v1";

    private static final class FakeBge implements Embedder {
        boolean closed;
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() { return BGE_TOKEN; }
        @Override public void close() { closed = true; }
    }

    private static void mark(String collection, String embeddingModel, int dimension) {
        CollectionRegistry.markKnown(TENANT, collection,
            new CollectionRow("unknown", TENANT, embeddingModel, dimension, "live"));
    }

    private static EmbedderRouter bgeOnly()    { return new EmbedderRouter(new FakeBge(), null, "document"); }
    private static EmbedderRouter voyageOnly() { return new EmbedderRouter(null, "dummy-key", "document"); }
    private static EmbedderRouter dual()       { return new EmbedderRouter(new FakeBge(), "dummy-key", "document"); }

    // ── the three shapes ────────────────────────────────────────────────────────

    @Test
    void bgeOnly_isOnnxLocal_andAdvertisesOnlyBge() {
        EmbedderRouter r = bgeOnly();
        assertThat(r.modeName()).isEqualTo("onnx-local");
        assertThat(r.availableModels()).containsExactly(BGE_TOKEN);
    }

    @Test
    void voyageOnly_isVoyage_andAdvertisesOnlyVoyage() {
        EmbedderRouter r = voyageOnly();
        assertThat(r.modeName()).isEqualTo("voyage");
        assertThat(r.availableModels())
            .containsExactlyInAnyOrder("voyage-3", "voyage-code-3", "voyage-context-3");
    }

    @Test
    void both_isDual_andAdvertisesBothFamilies() {
        EmbedderRouter r = dual();
        assertThat(r.modeName()).isEqualTo("dual");
        assertThat(r.availableModels())
            .containsExactlyInAnyOrder(BGE_TOKEN, "voyage-3", "voyage-code-3", "voyage-context-3");
    }

    @Test
    void twoArgAndStringFormsKeepTheirShapes() {
        // The two pre-existing constructors keep their meaning.
        assertThat(new EmbedderRouter(new FakeBge(), "document").modeName()).isEqualTo("onnx-local");
        assertThat(new EmbedderRouter("dummy-key", "document").modeName()).isEqualTo("voyage");
    }

    @Test
    void neitherFamily_isRefusedAtConstruction() {
        assertThatThrownBy(() -> new EmbedderRouter(null, null, "document"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new EmbedderRouter(null, "  ", "document"))
            .as("a blank key is no key")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void blankKeyWithLocal_isBgeOnly() {
        assertThat(new EmbedderRouter(new FakeBge(), "", "document").modeName()).isEqualTo("onnx-local");
    }

    // ── resolveEmbedderStrict: a bge row and a Voyage row ───────────────────────

    @Test
    void dual_routesABgeRow_toBge_andAVoyageRow_toCce() {
        mark(BGE_COLLECTION, BGE_TOKEN, 768);
        mark(VOYAGE_COLLECTION, "voyage-context-3", 1024);
        EmbedderRouter r = dual();
        assertThat(r.resolveEmbedderStrict(null, TENANT, BGE_COLLECTION).modelToken()).isEqualTo(BGE_TOKEN);
        assertThat(r.resolveEmbedderStrict(null, TENANT, VOYAGE_COLLECTION)).isInstanceOf(CceEmbedder.class);
    }

    @Test
    void dual_routesVoyageCodeAndVoyage3Rows_byTheirOwnModel() {
        mark("code__nexus__voyage-code-3__v1", "voyage-code-3", 1024);
        mark("knowledge__x__voyage-3__v1", "voyage-3", 1024);
        EmbedderRouter r = dual();
        assertThat(r.resolveEmbedderStrict(null, TENANT, "code__nexus__voyage-code-3__v1"))
            .isInstanceOf(VoyageEmbedder.class);
        assertThat(r.resolveEmbedderStrict(null, TENANT, "knowledge__x__voyage-3__v1").modelToken())
            .isEqualTo("voyage-3");
    }

    @Test
    void dual_stillRefusesAModelNeitherFamilyServes() {
        mark("docs__x__minilm-l6-v2-384__v1", "minilm-l6-v2-384", 384);
        assertThatThrownBy(() -> dual().resolveEmbedderStrict(null, TENANT, "docs__x__minilm-l6-v2-384__v1"))
            .isInstanceOf(EmbeddingModelUnavailableException.class)
            .hasMessageContaining("minilm-l6-v2-384")
            .hasMessageContaining("dual");
    }

    // ── the refusal text for the family that is not present ─────────────────────

    @Test
    void bgeOnly_refusesAVoyageRow_naming_theKeyRemedy() {
        mark(VOYAGE_COLLECTION, "voyage-context-3", 1024);
        assertThatThrownBy(() -> bgeOnly().resolveEmbedderStrict(null, TENANT, VOYAGE_COLLECTION))
            .isInstanceOf(EmbeddingModelUnavailableException.class)
            .hasMessageContaining("voyage-context-3")
            .hasMessageContaining("onnx-local")
            .hasMessageContaining("NX_VOYAGE_API_KEY");
    }

    @Test
    void voyageOnly_refusesABgeRow_naming_theModelFileRemedy() {
        mark(BGE_COLLECTION, BGE_TOKEN, 768);
        assertThatThrownBy(() -> voyageOnly().resolveEmbedderStrict(null, TENANT, BGE_COLLECTION))
            .isInstanceOf(EmbeddingModelUnavailableException.class)
            .hasMessageContaining(BGE_TOKEN)
            .hasMessageContaining("voyage")
            .hasMessageContaining("nx init --service");
    }

    @Test
    void resolveEmbedderByModel_followsThePresentSet() {
        assertThat(dual().resolveEmbedderByModel(BGE_TOKEN).modelToken()).isEqualTo(BGE_TOKEN);
        assertThat(dual().resolveEmbedderByModel("voyage-code-3")).isInstanceOf(VoyageEmbedder.class);
        assertThatThrownBy(() -> voyageOnly().resolveEmbedderByModel(BGE_TOKEN))
            .isInstanceOf(EmbeddingModelUnavailableException.class);
        assertThatThrownBy(() -> bgeOnly().resolveEmbedderByModel("voyage-code-3"))
            .isInstanceOf(EmbeddingModelUnavailableException.class);
    }

    // ── the plain embed() default and close() ───────────────────────────────────

    @Test
    void plainEmbed_usesTheLocalEmbedderWhenOnePresent_andRefusesWhenNone() {
        assertThat(dual().embed(List.of("x"))).hasSize(1);
        assertThatThrownBy(() -> voyageOnly().embed(List.of("x")))
            .isInstanceOf(EmbeddingModelUnavailableException.class);
    }

    @Test
    void close_closesTheLocalEmbedder_inDual() {
        FakeBge bge = new FakeBge();
        new EmbedderRouter(bge, "dummy-key", "document").close();
        assertThat(bge.closed).isTrue();
    }

    // ── profile tokens: keyed means Voyage until the profile variable lands (P2.3) ──

    @Test
    void contentTypeModelTokens_keepToday_keyedMeansVoyage_keylessMeansBge() {
        assertThat(dual().contentTypeModelTokens())
            .containsEntry("knowledge", "voyage-context-3").containsEntry("code", "voyage-code-3");
        assertThat(bgeOnly().contentTypeModelTokens())
            .containsEntry("knowledge", BGE_TOKEN).containsEntry("code", BGE_TOKEN);
        assertThat(voyageOnly().contentTypeModelTokens())
            .containsEntry("knowledge", "voyage-context-3").containsEntry("code", "voyage-code-3");
    }
}
