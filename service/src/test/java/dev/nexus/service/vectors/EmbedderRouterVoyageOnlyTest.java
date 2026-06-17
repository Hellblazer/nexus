// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * nexus-0n7uc — the production cloud router is VOYAGE-ONLY: it constructs no local
 * ONNX embedder, so the cloud engine never loads a MiniLM model (the missing-file
 * onnxruntime segfault that crashed v0.1.0 at boot — conexus STEP-5 / conexus-qcn).
 *
 * <p>Pure construction + routing assertions; no embed calls (so no Voyage network).
 * The Voyage/CCE embedders are HTTP clients that do nothing at construction.
 *
 * <p>Evidence scope (substantive-critic): these assertions prove no ONNX model
 * TOKEN is advertised and that minilm/non-conformant collections are refused — they
 * are behavioral proofs, not a construction proof that {@code new OnnxEmbedder()}
 * is never called. That "no ONNX constructed" guarantee is by inspection: the
 * voyage-only constructor body references no {@code OnnxEmbedder}, and
 * {@code OnnxEmbedder} has no static initializer that could load a model on
 * class-load. Main's voyage branch likewise constructs only this router.
 */
class EmbedderRouterVoyageOnlyTest {

    private EmbedderRouter router() {
        return new EmbedderRouter("dummy-key", "document");  // voyage-only cloud
    }

    @Test
    void voyageOnly_modeIsVoyage_andAdvertisesOnlyVoyageModels() {
        EmbedderRouter r = router();
        assertThat(r.modeName()).isEqualTo("voyage");
        assertThat(r.availableModels())
            .as("voyage-only cloud must NOT advertise any local ONNX model (no minilm)")
            .containsExactlyInAnyOrder("voyage-3", "voyage-code-3", "voyage-context-3")
            .doesNotContain("minilm-l6-v2-384");
    }

    @Test
    void conformantVoyageCollections_routeToTheirVoyageEmbedder() {
        EmbedderRouter r = router();
        assertThat(r.resolveEmbedderStrict("code__nexus__voyage-code-3__v1"))
            .isInstanceOf(VoyageEmbedder.class);
        assertThat(r.resolveEmbedderStrict("knowledge__nexus__voyage-context-3__v1"))
            .isInstanceOf(CceEmbedder.class);
    }

    @Test
    void minilmConformantCollection_isRefused_notLocallyEmbedded() {
        EmbedderRouter r = router();
        assertThatThrownBy(() ->
                r.resolveEmbedderStrict("code__nexus__minilm-l6-v2-384__v1"))
            .as("a minilm collection must be REFUSED in voyage-only cloud, never "
                + "embedded with a local 384-dim model")
            .isInstanceOf(EmbeddingModelUnavailableException.class)
            .hasMessageContaining("minilm-l6-v2-384");
    }

    @Test
    void nonConformantName_isRefused_noLocalFallback() {
        EmbedderRouter r = router();
        assertThatThrownBy(() -> r.resolveEmbedder("legacy_unprefixed_collection"))
            .as("non-conformant name has no local fallback in voyage-only cloud")
            .isInstanceOf(EmbeddingModelUnavailableException.class);
    }

    @Test
    void plainEmbed_refuses_thereIsNoLocalDefaultEmbedder() {
        EmbedderRouter r = router();
        assertThatThrownBy(() -> r.embed(List.of("x")))
            .isInstanceOf(EmbeddingModelUnavailableException.class);
    }

    @Test
    void close_doesNotThrow_withNoLocalEmbedder() {
        assertThatCode(() -> router().close()).doesNotThrowAnyException();
    }
}
