// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
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
 *
 * <p>RDR-204 Phase 2 (bead nexus-ft04v.16): {@code resolveEmbedderStrict} now
 * dispatches by the collection's REGISTERED ROW, read through {@link
 * CollectionRegistry}, never a token parsed from the name. {@link #mark} seeds
 * the (tenant, collection) → row mapping directly (pure in-process, no I/O) —
 * every lookup below is a cache HIT, so the {@code TenantScope} argument
 * (used only on a cache MISS) is never dereferenced; {@code null} is passed
 * deliberately to prove that.
 */
class EmbedderRouterVoyageOnlyTest {

    private static final String TENANT = "embedder-router-voyage-only-tenant";

    private EmbedderRouter router() {
        return new EmbedderRouter("dummy-key", "document");  // voyage-only cloud
    }

    /** Seed {@code collection}'s {@link CollectionRow} directly in the in-process
     *  cache — see the class javadoc. */
    private static void mark(String collection, String embeddingModel, int dimension) {
        CollectionRegistry.markKnown(TENANT, collection,
            new CollectionRow("unknown", TENANT, embeddingModel, dimension, "live"));
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
    void voyageModelRows_routeToTheirVoyageEmbedder() {
        String codeCollection = "code__nexus__voyage-code-3__v1";
        String cceCollection  = "knowledge__nexus__voyage-context-3__v1";
        mark(codeCollection, "voyage-code-3", 1024);
        mark(cceCollection, "voyage-context-3", 1024);
        EmbedderRouter r = router();
        assertThat(r.resolveEmbedderStrict(null, TENANT, codeCollection))
            .isInstanceOf(VoyageEmbedder.class);
        assertThat(r.resolveEmbedderStrict(null, TENANT, cceCollection))
            .isInstanceOf(CceEmbedder.class);
    }

    @Test
    void minilmModelRow_isRefused_notLocallyEmbedded() {
        String collection = "code__nexus__minilm-l6-v2-384__v1";
        mark(collection, "minilm-l6-v2-384", 384);
        EmbedderRouter r = router();
        assertThatThrownBy(() ->
                r.resolveEmbedderStrict(null, TENANT, collection))
            .as("a minilm-model row must be REFUSED in voyage-only cloud, never "
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
