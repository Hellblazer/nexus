// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.concurrent.Semaphore;
import java.util.function.Function;

/**
 * nexus-00wsf residual (T2 {@code nexus/diagnosis-00wsf-engine-cpu-spin-2026-09-04},
 * [24231]) — bounds CONCURRENT calls into a wrapped local embedder (bge / ONNX)
 * with a fair {@link Semaphore}, mirroring {@link CceEmbedder}'s {@code inFlight}
 * idiom (per-instance concurrency governor over an external upstream) and
 * {@link dev.nexus.service.db.TenantScope}'s {@code admission} bound (systemic
 * backpressure ahead of a fixed-capacity resource).
 *
 * <p>{@link Bge768Embedder#embed} already bounds ONE {@code embed()} call's peak
 * memory via {@code embedSubBatched} + {@code MAX_PADDED_TOKEN_AREA}
 * (f1c669b47, engine-service-v0.1.81 — the fix for the incident's actual
 * symptom, a 77 GB attention tensor). Nothing bounds the SUM across
 * CONCURRENT calls: {@code NexusService} runs an unbounded virtual-thread-
 * per-request executor, so N callers can each be inside {@code embed()} at
 * once, each allocating up to the per-call ceiling — N stacked local-embed
 * requests still take roughly N times the memory and every core. This wraps
 * the local embedder so at most {@link #permits()} embeds run at once; the
 * (N+1)th caller queues on the semaphore instead of racing for pages with the
 * other N.
 *
 * <p>Delegates {@code modelToken()} and {@code close()} verbatim so
 * {@link EmbedderRouter} can wrap its local embedder unconditionally without
 * changing model-identity keying or shutdown behaviour.
 */
final class AdmissionControlledEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(AdmissionControlledEmbedder.class);

    /** Spawn-env override for the admission bound (permits, not a ratio). */
    static final String PERMITS_ENV = "NX_LOCAL_EMBED_ADMISSION_PERMITS";

    private final Embedder delegate;
    private final int      permits;
    private final Semaphore inFlight;

    /** Production constructor: permits resolved from {@link #permitsFromEnv()}. */
    AdmissionControlledEmbedder(Embedder delegate) {
        this(delegate, permitsFromEnv());
    }

    /** Explicit-permits constructor (tests, and this class's own env-resolving delegate). */
    AdmissionControlledEmbedder(Embedder delegate, int permits) {
        if (permits <= 0) {
            throw new IllegalArgumentException(
                    "admission permits must be positive, got " + permits);
        }
        this.delegate = delegate;
        this.permits  = permits;
        // Fair: a burst of concurrent local-embed requests cannot starve an
        // earlier arrival — same fairness choice as TenantScope.ADMISSION.
        this.inFlight = new Semaphore(permits, true);
        log.info("event=local_embed_admission_configured permits={}", permits);
    }

    /** The configured concurrent-embed ceiling (test/diagnostic visibility). */
    int permits() {
        return permits;
    }

    @Override
    public String modelToken() {
        return delegate.modelToken();
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        inFlight.acquireUninterruptibly();
        try {
            return delegate.embed(texts);
        } finally {
            inFlight.release();
        }
    }

    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        inFlight.acquireUninterruptibly();
        try {
            return delegate.embedWithUsage(texts);
        } finally {
            inFlight.release();
        }
    }

    @Override
    public void close() {
        delegate.close();
    }

    /** Production entry point: real env, real core count. */
    static int permitsFromEnv() {
        return permitsFromEnv(System::getenv, Runtime.getRuntime().availableProcessors());
    }

    /**
     * Env-injectable resolver (tests never mutate real process env — mirrors
     * {@code OnnxModelPaths}/{@code MintRateLimiter}'s injection pattern).
     *
     * <p>Default: half the available cores (minimum 1) — a single local embed
     * can itself use several cores once {@link OnnxThreadPolicy} bounds intra-op
     * parallelism, so admitting more than half the box at once would still let
     * concurrent requests fully oversubscribe every core. {@code
     * NX_LOCAL_EMBED_ADMISSION_PERMITS} overrides the default; a non-positive
     * or non-numeric override is REFUSED loudly rather than silently coerced
     * (no-silent-fallbacks-for-correctness) — an operator who fat-fingers "0"
     * must see why the service refused to start serving embeds unbounded,
     * not get an accidental unlimited admission gate.
     */
    static int permitsFromEnv(Function<String, String> env, int availableCores) {
        String raw = env.apply(PERMITS_ENV);
        if (raw == null || raw.isBlank()) {
            return Math.max(1, availableCores / 2);
        }
        int parsed;
        try {
            parsed = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    PERMITS_ENV + " must be an integer, got: " + raw, e);
        }
        if (parsed <= 0) {
            throw new IllegalArgumentException(
                    PERMITS_ENV + " must be positive, got: " + parsed);
        }
        return parsed;
    }
}
