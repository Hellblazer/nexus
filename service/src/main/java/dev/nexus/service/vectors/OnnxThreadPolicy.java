// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.function.Function;

/**
 * nexus-00wsf residual (T2 {@code nexus/diagnosis-00wsf-engine-cpu-spin-2026-09-04},
 * [24231]) — the shared intra-op thread bound for every {@code OrtSession}
 * this service creates.
 *
 * <p>{@link Bge768Embedder}, {@link OnnxEmbedder}, and {@link
 * CrossEncoderReranker} each constructed a bare {@code new
 * OrtSession.SessionOptions()} with no {@code setIntraOpNumThreads} call and
 * no env var anywhere in the repo to bound it. Measured: a single ONNX
 * request took 7.8 of 16 cores with no admission control at all — a
 * plausible mechanical link to nexus-7f7gb (the storage-service watchdog
 * cycling the engine under legitimate load, since one request could starve
 * the health-probe thread of CPU). This class is the ONE resolver all three
 * sites read, so the bound is consistent and independently tunable from the
 * {@link AdmissionControlledEmbedder} concurrency bound (a different
 * mechanism: this limits threads WITHIN one ONNX call; that limits how many
 * ONNX calls run AT ONCE).
 *
 * <p>Mirrors {@link OnnxModelPaths}'s pure-resolver-plus-real-env-wrapper
 * shape so tests never mutate real process env.
 */
final class OnnxThreadPolicy {

    /** Spawn-env override for the intra-op thread bound (an absolute count, not a ratio). */
    static final String INTRA_OP_THREADS_ENV = "NX_ONNX_INTRA_OP_THREADS";

    private OnnxThreadPolicy() {}

    /** Production entry point: real env, real core count. */
    static int intraOpThreads() {
        return intraOpThreads(System::getenv, Runtime.getRuntime().availableProcessors());
    }

    /**
     * Env-injectable resolver (tests never mutate real process env).
     *
     * <p>Default: half the available cores (minimum 1) — bounded well below
     * "one request takes every core", while still letting a single embed use
     * real parallelism on typical hardware. {@code NX_ONNX_INTRA_OP_THREADS}
     * overrides the default; a non-positive or non-numeric override is
     * REFUSED loudly (no-silent-fallbacks-for-correctness), same discipline
     * as {@link AdmissionControlledEmbedder#permitsFromEnv}.
     */
    static int intraOpThreads(Function<String, String> env, int availableCores) {
        String raw = env.apply(INTRA_OP_THREADS_ENV);
        if (raw == null || raw.isBlank()) {
            return Math.max(1, availableCores / 2);
        }
        int parsed;
        try {
            parsed = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    INTRA_OP_THREADS_ENV + " must be an integer, got: " + raw, e);
        }
        if (parsed <= 0) {
            throw new IllegalArgumentException(
                    INTRA_OP_THREADS_ENV + " must be positive, got: " + parsed);
        }
        return parsed;
    }
}
