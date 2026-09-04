// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * nexus-00wsf residual (T2 {@code nexus/diagnosis-00wsf-engine-cpu-spin-2026-09-04},
 * [24231]; reworked in review round 2, [24238]/[24239]) — gates a local (bge /
 * ONNX) {@link Embedder} through a SHARED {@link LocalOnnxAdmission}.
 *
 * <p><b>Process-wide, not per-instance (review finding 1).</b> This class
 * owns no {@link java.util.concurrent.Semaphore} of its own — every wrapper
 * in a process takes the SAME {@link LocalOnnxAdmission} (constructed once,
 * in {@code Main.java}'s local-mode branch, and shared with {@link
 * AdmissionControlledReranker} too — finding 4: reranks compete for the
 * SAME budget as embeds, not a separate one). {@code EmbedderRouter} no
 * longer wraps its local embedder internally; the caller wraps BEFORE
 * constructing the router(s), so wrapping the same delegate for both the
 * document and query routers shares one bound instead of doubling it.
 *
 * <p>See {@link LocalOnnxAdmission}'s javadoc for what this bounds (a CPU-
 * derived proxy — call COUNT, not the padded-token-area a call actually
 * costs) and what it does not (the query-path latency effect of this bound
 * combined with {@link OnnxThreadPolicy}'s intra-op cap is UNMEASURED).
 *
 * <p><b>Two acquisition policies (review finding 3).</b> {@code interactive}
 * chooses which: {@code false} (document/bulk-indexing callers) blocks via
 * {@link LocalOnnxAdmission#acquireInterruptible()} — interruptible, mirrors
 * {@link CceEmbedder}'s {@code inFlight.acquire()} idiom exactly (critic
 * finding 1 — the original version of this class claimed that mirror while
 * actually calling {@code acquireUninterruptibly}, which CceEmbedder does
 * not use). {@code true} (query callers) uses {@link
 * LocalOnnxAdmission#tryAcquire(long)} with a bounded timeout so a bulk-
 * indexing burst cannot stall interactive search unboundedly; on timeout
 * this fails loud via {@link LocalOnnxAdmission#admissionTimeoutException}
 * (a {@code SQLTransientConnectionException} in the cause chain — the same
 * typed-retryable signal {@code TenantScope}'s admission timeout produces,
 * so {@code HttpUtil.isPoolExhausted} maps it to 503 with no new mapping
 * surface).
 */
public final class AdmissionControlledEmbedder implements Embedder {

    private final Embedder          delegate;
    private final LocalOnnxAdmission admission;
    private final boolean           interactive;

    /**
     * @param delegate    the local embedder to gate (bge, or the ONNX fallback)
     * @param admission   the SHARED admission gate — pass the SAME instance to
     *                    every wrapper in the process so the bound is process-wide
     * @param interactive {@code true} for query-path callers (bounded-timeout
     *                    acquire, fails loud); {@code false} for document/bulk
     *                    callers (blocking, interruptible acquire)
     */
    public AdmissionControlledEmbedder(Embedder delegate, LocalOnnxAdmission admission, boolean interactive) {
        this.delegate    = delegate;
        this.admission   = admission;
        this.interactive = interactive;
    }

    @Override
    public String modelToken() {
        return delegate.modelToken();
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        acquire("embed");
        try {
            return delegate.embed(texts);
        } finally {
            admission.release();
        }
    }

    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        acquire("embedWithUsage");
        try {
            return delegate.embedWithUsage(texts);
        } finally {
            admission.release();
        }
    }

    @Override
    public void close() {
        delegate.close();
    }

    private void acquire(String what) {
        try {
            if (interactive) {
                if (!admission.tryAcquire(admission.queryTimeoutMs())) {
                    throw LocalOnnxAdmission.admissionTimeoutException(
                            "interactive " + what, admission.queryTimeoutMs());
                }
            } else {
                admission.acquireInterruptible();
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new RuntimeException("interrupted awaiting local ONNX admission for " + what, ie);
        }
    }
}
