// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.function.Function;

/**
 * nexus-00wsf residual (T2 {@code nexus/diagnosis-00wsf-engine-cpu-spin-2026-09-04},
 * [24231]; coordinated with admission in review round 2, [24238]) — the
 * shared intra-op thread bound for every {@code OrtSession} this service
 * creates.
 *
 * <p>{@link Bge768Embedder}, {@link OnnxEmbedder}, and {@link
 * CrossEncoderReranker} each constructed a bare {@code new
 * OrtSession.SessionOptions()} with no {@code setIntraOpNumThreads} call and
 * no env var anywhere in the repo to bound it. Measured: a single ONNX
 * request took 7.8 of 16 cores with no admission control at all — a
 * plausible mechanical link to nexus-7f7gb (the storage-service watchdog
 * cycling the engine under legitimate load, since one request could starve
 * the health-probe thread of CPU). This class is the ONE resolver all three
 * sites read, so the bound is consistent.
 *
 * <p><b>Coordinated with {@link LocalOnnxAdmission} (review finding 2).</b>
 * The default is no longer independent of the admission-permits default:
 * {@code intraOpThreads = max(1, availableCores / admissionPermits)}, so
 * {@code permits * intraOpThreads <= availableCores} by construction (floor
 * division) in the unconfigured case — round 1 defaulted both to {@code
 * cores/2} independently, which on a 16-core box could reach 8x8=64 threads
 * if every admitted embed ran full-width, before even counting round 1's
 * separate double-admission defect. An explicit {@code
 * NX_ONNX_INTRA_OP_THREADS} override still wins outright (an operator who
 * sets it is accepting responsibility for the product), refused at zero,
 * negative, or non-numeric, same discipline as {@link
 * LocalOnnxAdmission#permitsFromEnv}.
 *
 * <p><b>Unmeasured (critic finding 3).</b> This class does not claim a
 * specific query-path latency effect from the intra-op cap — see {@link
 * LocalOnnxAdmission}'s javadoc.
 *
 * <p>Mirrors {@link OnnxModelPaths}'s pure-resolver-plus-real-env-wrapper
 * shape so tests never mutate real process env.
 */
final class OnnxThreadPolicy {

    private static final Logger log = LoggerFactory.getLogger(OnnxThreadPolicy.class);

    /** Spawn-env override for the intra-op thread bound (an absolute count, not a ratio). */
    static final String INTRA_OP_THREADS_ENV = "NX_ONNX_INTRA_OP_THREADS";

    private OnnxThreadPolicy() {}

    /**
     * Production entry point: real env, real core count, admission permits
     * resolved from {@link LocalOnnxAdmission#permitsFromEnv()} so the
     * default coordinates with the admission bound. Logs the resolved value
     * at INFO (previously this class logged nothing — review finding 2/5;
     * pairs with {@link LocalOnnxAdmission}'s own boot log line for full
     * "both resolved values" visibility).
     */
    static int intraOpThreads() {
        int cores = Runtime.getRuntime().availableProcessors();
        int permits = LocalOnnxAdmission.permitsFromEnv();
        int threads = intraOpThreads(System::getenv, cores, permits);
        log.info("event=onnx_intra_op_threads_configured threads={} admission_permits={} cores={}",
                threads, permits, cores);
        return threads;
    }

    /**
     * Env-injectable resolver (tests never mutate real process env).
     *
     * <p>Default: {@code max(1, availableCores / admissionPermits)} — see
     * class javadoc for the coordination rationale. {@code
     * NX_ONNX_INTRA_OP_THREADS} overrides the default; a non-positive or
     * non-numeric override is REFUSED loudly (no-silent-fallbacks-for-
     * correctness).
     */
    static int intraOpThreads(Function<String, String> env, int availableCores, int admissionPermits) {
        String raw = env.apply(INTRA_OP_THREADS_ENV);
        if (raw == null || raw.isBlank()) {
            return Math.max(1, availableCores / Math.max(1, admissionPermits));
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
