// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.CceEmbedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.EmbeddingModelUnavailableException;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.VoyageEmbedder;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-pebfx.2 — embedding-mode fail-loud + model-identity dispatch.
 *
 * <p>Background (2026-06-10 production migration): the service silently fell
 * back to ONNX-384 without {@code NX_VOYAGE_API_KEY}, surfacing only as
 * per-collection dim-mismatch 400s — and ONLY because voyage models happen to
 * be 1024-dim. A same-dimension wrong-model would have contaminated silently.
 *
 * <p>Contract pinned here: for four-segment conformant names the RDR-103
 * model segment is the routing AUTHORITY. A conformant collection whose model
 * the current mode cannot embed is refused with
 * {@link EmbeddingModelUnavailableException} (→ HTTP 422), never silently
 * embedded with a different model. Legacy prefix routing
 * ({@link EmbedderRouter#resolveEmbedder}) survives only as the fallback for
 * non-conformant names; its behaviour stays pinned by {@code EmbedParityTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EmbeddingModeFailLoudTest {

    private static OnnxEmbedder onnx;

    @BeforeAll
    static void setUp() {
        onnx = new OnnxEmbedder();
    }

    @AfterAll
    static void tearDown() {
        onnx.close();
    }

    // ── Model-token identity (item 4 precondition) ───────────────────────────

    @Test
    void modelTokens_matchRdr103Segments() {
        assertThat(onnx.modelToken()).isEqualTo("minilm-l6-v2-384");
        assertThat(new VoyageEmbedder("k", "voyage-code-3", "document").modelToken())
                .isEqualTo("voyage-code-3");
        assertThat(new VoyageEmbedder("k", "voyage-3", "document").modelToken())
                .isEqualTo("voyage-3");
        assertThat(new CceEmbedder("k", "document").modelToken())
                .isEqualTo("voyage-context-3");
    }

    // ── ONNX-local mode refuses voyage-token collections (item 3) ────────────

    @Test
    void localMode_refusesVoyageSegmentCollection_withExplicitMessage() {
        EmbedderRouter router = new EmbedderRouter(onnx, "document");
        assertThatThrownBy(() -> router.embedForCollection(
                "knowledge__nexus__voyage-context-3__v1", List.of("text")))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("onnx-local")
                .hasMessageContaining("voyage-context-3")
                .hasMessageContaining("knowledge__nexus__voyage-context-3__v1")
                .hasMessageContaining("NX_VOYAGE_API_KEY");
    }

    @Test
    void localMode_refusesVoyageCodeSegment() {
        EmbedderRouter router = new EmbedderRouter(onnx, "query");
        assertThatThrownBy(() -> router.resolveEmbedderStrict(
                "code__nexus__voyage-code-3__v1"))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("voyage-code-3");
    }

    @Test
    void localMode_stillServesMinilmSegment() {
        EmbedderRouter router = new EmbedderRouter(onnx, "document");
        assertThat(router.resolveEmbedderStrict(
                "knowledge__dualrun__minilm-l6-v2-384__v1"))
                .isSameAs(onnx);
    }

    // ── Model segment is the authority, not the prefix (item 4) ──────────────

    @Test
    void cloudMode_minilmSegment_routesToOnnx_notByPrefix() {
        // The live nexus-pebfx.8 failure class: knowledge__seam-b-test__minilm…
        // was prefix-routed to CCE (1024) and 400'd on the chunks_384 table.
        // Segment-authoritative dispatch makes it servable.
        EmbedderRouter router = new EmbedderRouter(onnx, "dummy-key", "query");
        assertThat(router.resolveEmbedderStrict(
                "knowledge__seam-b-test__minilm-l6-v2-384__v1"))
                .isSameAs(onnx);
    }

    @Test
    void cloudMode_voyage3Segment_routesToPlainVoyage_notCce() {
        // Same-dim wrong-model hole: prefix routing sent knowledge__*__voyage-3
        // to CCE (voyage-context-3) — both 1024-dim, silent contamination.
        EmbedderRouter router = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(router.resolveEmbedderStrict("knowledge__x__voyage-3__v1")
                .modelToken()).isEqualTo("voyage-3");
    }

    @Test
    void cloudMode_cceAndCodeSegments_routeByModelToken() {
        EmbedderRouter router = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(router.resolveEmbedderStrict("docs__nexus__voyage-context-3__v1")
                .modelToken()).isEqualTo("voyage-context-3");
        assertThat(router.resolveEmbedderStrict("code__nexus__voyage-code-3__v1")
                .modelToken()).isEqualTo("voyage-code-3");
    }

    @Test
    void unknownModelSegment_refusedInBothModes() {
        // bge-base-en-v15-768 is a known RDR-103 token (chunks_768 exists) but
        // no embedder is wired for it — must refuse, not ONNX-embed into a
        // 768-dim table.
        EmbedderRouter local = new EmbedderRouter(onnx, "document");
        EmbedderRouter cloud = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThatThrownBy(() -> local.resolveEmbedderStrict(
                "knowledge__x__bge-base-en-v15-768__v1"))
                .isInstanceOf(EmbeddingModelUnavailableException.class);
        assertThatThrownBy(() -> cloud.resolveEmbedderStrict(
                "knowledge__x__bge-base-en-v15-768__v1"))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("bge-base-en-v15-768");
    }

    // ── Non-conformant names keep legacy prefix routing ──────────────────────

    @Test
    void nonConformantName_fallsBackToPrefixRouting() {
        EmbedderRouter local = new EmbedderRouter(onnx, "document");
        assertThat(local.resolveEmbedderStrict("knowledge__test")).isSameAs(onnx);

        EmbedderRouter cloud = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(cloud.resolveEmbedderStrict("knowledge__test").modelToken())
                .isEqualTo("voyage-context-3");
        assertThat(cloud.resolveEmbedderStrict(null)).isSameAs(onnx);
    }

    // ── Banner surface ────────────────────────────────────────────────────────

    @Test
    void modeNameAndAvailableModels_reflectConstruction() {
        EmbedderRouter local = new EmbedderRouter(onnx, "document");
        assertThat(local.modeName()).isEqualTo("onnx-local");
        assertThat(local.availableModels()).containsExactly("minilm-l6-v2-384");

        EmbedderRouter cloud = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(cloud.modeName()).isEqualTo("voyage");
        assertThat(cloud.availableModels()).containsExactly(
                "minilm-l6-v2-384", "voyage-3", "voyage-code-3", "voyage-context-3");
    }
}
