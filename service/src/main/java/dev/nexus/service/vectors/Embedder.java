// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * RDR-152 bead nexus-gmiaf.20 — common embedding interface for Seam B.
 *
 * <p>Implementations:
 * <ul>
 *   <li>{@link OnnxEmbedder} — LOCAL: onnxruntime-java + DJL HF tokenizer,
 *       exact S0.2 pipeline (cosine 1.0 vs Python chromadb ONNXMiniLM_L6_V2)</li>
 *   <li>{@link VoyageEmbedder} — CLOUD: Voyage AI REST with truncation=true envelope</li>
 * </ul>
 */
public interface Embedder extends AutoCloseable {

    /**
     * Embed a batch of texts.  Returns one float[] per input text (in order).
     *
     * @param texts list of text strings to embed
     * @return list of embedding vectors, aligned with input
     */
    List<float[]> embed(List<String> texts);

    /**
     * Embed a single text — convenience wrapper over {@link #embed(List)}.
     */
    default float[] embedOne(String text) {
        return embed(List.of(text)).get(0);
    }

    /**
     * Embed a batch of texts and return both the vectors and the token count
     * consumed by the embedding call (bead nexus-ehc4q).
     *
     * <p>Default implementation returns 0 for {@code tokens} so that
     * {@link FakeEmbedder} and any custom implementations compile without
     * change. Production embedders ({@link VoyageEmbedder}, {@link CceEmbedder},
     * {@link OnnxEmbedder}) override this to return the real count.
     *
     * @param texts list of text strings to embed
     * @return {@link EmbedResult} carrying vectors (aligned with input) and
     *         the total token count ({@code 0} when unknown)
     */
    default EmbedResult embedWithUsage(List<String> texts) {
        return new EmbedResult(embed(texts), 0L);
    }

    /**
     * RDR-103 embedding-model token this embedder produces (the collection-name
     * model segment, e.g. {@code "voyage-code-3"}, {@code "minilm-l6-v2-384"}).
     *
     * <p>Default {@code "unknown"} keeps test fakes compiling (the locked
     * {@code PgVectorRepositoryContractTest.FakeEmbedder} is additive-only);
     * every production embedder overrides (bead nexus-pebfx.2 model-identity
     * validation).
     */
    default String modelToken() {
        return "unknown";
    }

    /**
     * Bead nexus-s71lr, pass 3 — live embed-activity snapshot for {@code GET
     * /v1/status} ({@code dev.nexus.service.http.StatusHandler} via {@code
     * EmbedderRouter#embedActivitySnapshots()}). Default {@code null} — most
     * implementations (test fakes) do not track this; only the production
     * embedders that opted in ({@link Bge768Embedder},
     * {@link VoyageEmbedder}, {@link CceEmbedder}) override it. {@code null}
     * means "not tracked", never a fabricated zero-activity reading.
     */
    default EmbedActivitySnapshot activitySnapshot() {
        return null;
    }

    /**
     * Bead nexus-8hdg9 (post-{@code Phase 3/4} residual, critique T2
     * {@code critique-nexus-8hdg9-p3-p4-808582a09} [24692]) — record one
     * request aborted at a deadline check point OUTSIDE this embedder's own
     * code, so the SAME {@code GET /v1/status} {@code deadline_aborts_total}
     * counter this embedder already reports covers a check point a wrapper
     * performs on its behalf (see {@link AdmissionControlledEmbedder}, which
     * checks the deadline immediately after acquiring its admission permit
     * and before ever calling into the delegate — a request abandoned while
     * queued for admission never reaches this embedder's own sub-batch loop
     * at all, so that loop's check point can never see it).
     *
     * <p>Default no-op: most implementations (test fakes, {@link
     * VoyageEmbedder}, {@link CceEmbedder} — reached only through {@code
     * EmbedderRouter}, never through {@link AdmissionControlledEmbedder})
     * track nothing here. Only {@link Bge768Embedder} — the sole production
     * delegate {@link AdmissionControlledEmbedder} wraps — overrides it.
     */
    default void recordDeadlineAbort() {
    }

    @Override
    default void close() {}
}
