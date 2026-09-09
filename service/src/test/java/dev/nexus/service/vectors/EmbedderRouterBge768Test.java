// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-160 P2 (bead nexus-jl9z5) — production-shaped local-mode routing with the
 * bge-768 embedder wired (the model Main wires in local mode).
 *
 * <p>RDR-204 Phase 2 (bead nexus-ft04v.16) rewrite: {@code resolveEmbedderStrict}
 * now dispatches by the collection's REGISTERED ROW (read through {@link
 * CollectionRegistry}), never a token parsed from the collection's own name.
 * {@link #mark} seeds the (tenant, collection) → row mapping directly (a pure
 * in-process {@code ConcurrentHashMap} write, no I/O) — a cache HIT for every
 * lookup below, so {@code resolveEmbedderStrict}'s {@code TenantScope} argument
 * (used only on a cache MISS) is never dereferenced; {@code null} is passed
 * deliberately to prove that.
 *
 * <p>Model-free: uses a {@link FakeBge} whose {@code modelToken()} is
 * {@code "bge-base-en-v15-768"}, so this verifies the ROUTING contract without
 * the 416MB ONNX (the cosine parity itself is {@code Bge768ParityTest}).
 *
 * <p>The load-bearing P2 assertion is {@link #minilmRow_refused_noSilentFallback}:
 * on a bge-only local service a {@code minilm-l6-v2-384} row must be
 * REFUSED, never silently embedded at 384-dim into a 768 table.
 */
class EmbedderRouterBge768Test {

    private static final String TENANT = "embedder-router-bge768-tenant";

    /** A stand-in for {@link Bge768Embedder} that needs no model file. */
    private static final class FakeBge implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() {
            return "bge-base-en-v15-768";
        }
    }

    private final EmbedderRouter router = new EmbedderRouter(new FakeBge(), "document");

    /** Seed {@code collection}'s {@link CollectionRow} directly in the in-process
     *  cache — see the class javadoc. */
    private static void mark(String collection, String embeddingModel, int dimension) {
        CollectionRegistry.markKnown(TENANT, collection,
            new CollectionRow("unknown", TENANT, embeddingModel, dimension, "live"));
    }

    @Test
    void localMode_routesBgeRow_toBgeEmbedder() {
        String primary = "knowledge__nexus__bge-base-en-v15-768__v1";
        mark(primary, "bge-base-en-v15-768", 768);
        Embedder e = router.resolveEmbedderStrict(null, TENANT, primary);
        assertThat(e.modelToken()).isEqualTo("bge-base-en-v15-768");
        // every bge-model row routes to bge in local mode, regardless of the
        // name's own content-type prefix.
        for (String col : List.of(
                "docs__nexus__bge-base-en-v15-768__v1",
                "rdr__nexus__bge-base-en-v15-768__v1",
                "code__nexus__bge-base-en-v15-768__v1")) {
            mark(col, "bge-base-en-v15-768", 768);
            assertThat(router.resolveEmbedderStrict(null, TENANT, col).modelToken())
                    .as("local-mode bge routing for %s", col)
                    .isEqualTo("bge-base-en-v15-768");
        }
    }

    @Test
    void minilmRow_refused_noSilentFallback() {
        // The whole point of RDR-160 P2: a MiniLM-model row on the bge-only
        // local service must REFUSE, not degrade to a 384-dim embed.
        String collection = "knowledge__nexus__minilm-l6-v2-384__v1";
        mark(collection, "minilm-l6-v2-384", 384);
        assertThatThrownBy(() -> router.resolveEmbedderStrict(null, TENANT, collection))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("minilm-l6-v2-384");
    }

    @Test
    void voyageRow_refused_inLocalMode() {
        String collection = "knowledge__nexus__voyage-context-3__v1";
        mark(collection, "voyage-context-3", 1024);
        assertThatThrownBy(() -> router.resolveEmbedderStrict(null, TENANT, collection))
                .isInstanceOf(EmbeddingModelUnavailableException.class);
    }

    @Test
    void availableModels_and_modeName_reflectBge() {
        // modeName stays "onnx-local" — the RUNTIME is local ONNX; only the MODEL
        // changed (RDR-160). The model identity is surfaced via availableModels.
        assertThat(router.modeName()).isEqualTo("onnx-local");
        assertThat(router.availableModels()).containsExactly("bge-base-en-v15-768");
    }

    @Test
    void nonConformantName_stillRoutesByRegistryRow_toLocalBge() {
        // RDR-204 Phase 1's universal-registration requirement: an unparseable,
        // non-conformant name goes through the registry now — there is no more
        // name-shape escape hatch into prefix routing for a NON-NULL collection.
        String collection = "knowledge__test";
        mark(collection, "bge-base-en-v15-768", 768);
        Embedder e = router.resolveEmbedderStrict(null, TENANT, collection);
        assertThat(e.modelToken()).isEqualTo("bge-base-en-v15-768");
    }

    // NOTE: a genuinely UNREGISTERED (tenant, collection) pair cannot be exercised in
    // THIS class — CollectionRegistry#lookup's cache-miss branch needs a real
    // TenantScope backed by a real DataSource to reach the SELECT that proves absence,
    // and this class is deliberately DB-less (see the class javadoc). That contract is
    // already pinned against a real Testcontainers substrate by
    // CollectionRegistryTest#require_throwsWithNoRowCached_whenCollectionNeverRegistered.

    /**
     * RDR-160 P4.3 (bead nexus-x9cjh) — the production embed-dispatch composition
     * with the REAL Bge768Embedder (not FakeBge): provisioned model → router
     * resolves a bge collection to it → embedDoubleForCollection yields a 768-dim
     * vector. Closes the fetch→load→embed→768 chain that the fake cannot.
     * Skipped (loud) when the 416MB model is absent; the live HTTP layer over this
     * is model-agnostic and covered by VectorHandlerEmbeddingModeTest.
     */
    @Test
    void realBge_embedForCollection_yields768Dim() {
        String modelPath = System.getProperty("nexus.bge.modelPath", Bge768Embedder.DEFAULT_MODEL_PATH);
        String tokPath = System.getProperty("nexus.bge.tokenizerPath", Bge768Embedder.DEFAULT_TOKENIZER_PATH);
        Assumptions.assumeTrue(
                Files.isRegularFile(Path.of(modelPath)) && Files.isRegularFile(Path.of(tokPath)),
                "bge-768 model absent — provision via `nx init --service` (RDR-160 P3)");

        String collection = "knowledge__nexus__bge-base-en-v15-768__v1";
        mark(collection, "bge-base-en-v15-768", 768);
        try (Bge768Embedder bge = new Bge768Embedder(modelPath, tokPath)) {
            EmbedderRouter real = new EmbedderRouter(bge, "document");
            // a bge-model row (→ embedding_768) routes to the real embedder
            List<double[]> vecs = real.embedDoubleForCollection(
                    null, TENANT, collection, List.of("fresh --service boot smoke"));
            assertThat(vecs).hasSize(1);
            assertThat(vecs.get(0)).hasSize(768);
        }
    }
}
