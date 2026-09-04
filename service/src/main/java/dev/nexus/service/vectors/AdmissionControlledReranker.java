// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * nexus-00wsf residual, review round 2 (finding 4: {@code CrossEncoderReranker}
 * was outside admission control entirely — thread-capped via {@link
 * OnnxThreadPolicy} but free to stack an uncapped extra local-ONNX workload
 * on top of the embed admission budget).
 *
 * <p>Gates a local {@link Reranker} through the SAME shared {@link
 * LocalOnnxAdmission} instance the embed-path wrappers use — reranks and
 * embeds compete for one process-wide budget, not two independent ones, since
 * both are local-ONNX CPU-bound work on the same box. Always constructed
 * {@code interactive=true} in production: the local cross-encoder reranker is
 * only ever invoked from the search/query response path (never bulk
 * indexing), so it uses the bounded-timeout acquire, same rationale as query
 * embeds — see {@link AdmissionControlledEmbedder}'s javadoc for the
 * acquisition-policy split and the unmeasured-latency caveat, which applies
 * here identically.
 */
public final class AdmissionControlledReranker implements Reranker {

    private final Reranker          delegate;
    private final LocalOnnxAdmission admission;
    private final boolean           interactive;

    public AdmissionControlledReranker(Reranker delegate, LocalOnnxAdmission admission, boolean interactive) {
        this.delegate    = delegate;
        this.admission   = admission;
        this.interactive = interactive;
    }

    @Override
    public String modelToken() {
        return delegate.modelToken();
    }

    @Override
    public List<Scored> rerank(String query, List<String> documents, Integer topK) {
        try {
            if (interactive) {
                if (!admission.tryAcquire(admission.queryTimeoutMs())) {
                    throw LocalOnnxAdmission.admissionTimeoutException(
                            "interactive rerank", admission.queryTimeoutMs());
                }
            } else {
                admission.acquireInterruptible();
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new RuntimeException("interrupted awaiting local ONNX admission for rerank", ie);
        }
        try {
            return delegate.rerank(query, documents, topK);
        } finally {
            admission.release();
        }
    }
}
