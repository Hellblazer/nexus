// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.OptionalInt;
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
 * <p><b>The session is SHARED, so the bound is per PROCESS, not per request
 * (2026-09-04, the v0.1.99 regression).</b> v0.1.99 derived the default as
 * {@code availableCores / admissionPermits} on the reasoning that each
 * admitted embed would run its own full-width session. It does not: every
 * request runs against ONE {@code OrtSession} per model, and ORT's intra-op
 * pool belongs to the session, so that arithmetic capped the WHOLE engine
 * at cores/permits threads (two on a 16-core box). The 7.29.0 release
 * shakedown measured it: {@code nx index rdr} sat 30+ minutes with the
 * engine pinned at ~190% CPU, where the previous engine finished the same
 * step in minutes. The engine therefore no longer sets the intra-op count
 * at all unless an operator asks: with no override the session keeps ORT's
 * own default (its physical-core heuristic, the exact pre-v0.1.99 width the
 * 7.8-of-16-cores measurement above was taken against), so this class never
 * has to guess at logical-versus-physical cores. {@link LocalOnnxAdmission}
 * alone bounds concurrency (admitted runs share this one pool, ORT schedules
 * them onto it). {@code NX_ONNX_INTRA_OP_THREADS} sets an explicit count,
 * refused at zero, negative, or non-numeric, same discipline as {@link
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
     * Production entry point: real env. Returns the operator's explicit
     * intra-op count, or empty when the session should keep ORT's default.
     * Logs the resolved choice at INFO either way, so an operator can see
     * which applies without reading the code.
     */
    static OptionalInt intraOpThreads() {
        OptionalInt threads = intraOpThreads(System::getenv);
        if (threads.isPresent()) {
            log.info("event=onnx_intra_op_threads_configured threads={} source=env shared_session=true",
                    threads.getAsInt());
        } else {
            log.info("event=onnx_intra_op_threads_configured threads=ort_default source=default shared_session=true");
        }
        return threads;
    }

    /**
     * Env-injectable resolver (tests never mutate real process env).
     *
     * <p>Empty when {@code NX_ONNX_INTRA_OP_THREADS} is unset or blank: the
     * session keeps ORT's default, see the class javadoc for why it is never
     * derived from cores or permits here. A set value is applied verbatim; a
     * non-positive or non-numeric value is REFUSED loudly
     * (no-silent-fallbacks-for-correctness).
     */
    static OptionalInt intraOpThreads(Function<String, String> env) {
        String raw = env.apply(INTRA_OP_THREADS_ENV);
        if (raw == null || raw.isBlank()) {
            return OptionalInt.empty();
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
        return OptionalInt.of(parsed);
    }
}
