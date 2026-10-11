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
 * <p><b>One case where the default is not ORT's: a CPU limit (nexus-qe5y2).</b>
 * ORT sizes that default from the HOST's cores and pins its threads to them; it
 * does not read a cpuset or a CPU quota. In a 6-CPU cgroup on a 64-thread host
 * each engine started about 32 intra-op threads on 6 CPUs and logged
 * {@code pthread_setaffinity_np failed ... error code: 22} for masks naming CPUs
 * outside the set; model load under that oversubscription outlasted the engine's
 * 3 s shutdown wait (nexus-y6aj0). So when the JVM can use fewer processors than
 * the host has online, the count is the JVM's number. This is not the v0.1.99
 * arithmetic: nothing is divided by permits, and on a host with no limit, where
 * the two numbers are equal, ORT's default stays. The host count is read on Linux
 * only ({@code /sys/devices/system/cpu/online}); elsewhere, or when the file
 * cannot be read, nothing is derived.
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
        OptionalInt explicit = intraOpThreads(System::getenv);
        if (explicit.isPresent()) {
            log.info("event=onnx_intra_op_threads_configured threads={} source=env shared_session=true",
                    explicit.getAsInt());
            return explicit;
        }
        int available = Runtime.getRuntime().availableProcessors();
        OptionalInt host = hostProcessors(HOST_CPUS_ONLINE);
        OptionalInt limited = underCpuLimit(available, host);
        if (limited.isPresent()) {
            log.info("event=onnx_intra_op_threads_configured threads={} source=cpu_limit host_processors={} "
                    + "shared_session=true", limited.getAsInt(), host.getAsInt());
        } else {
            log.info("event=onnx_intra_op_threads_configured threads=ort_default source=default shared_session=true");
        }
        return limited;
    }

    /** Linux: the CPUs online on the host, whatever cpuset or quota this process runs under. */
    static final java.nio.file.Path HOST_CPUS_ONLINE = java.nio.file.Path.of("/sys/devices/system/cpu/online");

    /**
     * The intra-op count for a process that may use fewer processors than its host has
     * ({@code available < host}): {@code available}. Empty when there is no limit or the host
     * count is unknown, so the session keeps ORT's default.
     */
    static OptionalInt underCpuLimit(int available, OptionalInt host) {
        if (host.isPresent() && available > 0 && available < host.getAsInt()) {
            return OptionalInt.of(available);
        }
        return OptionalInt.empty();
    }

    /** The number of CPUs listed in a kernel CPU-list file such as {@code online}; empty if unreadable. */
    static OptionalInt hostProcessors(java.nio.file.Path cpuListFile) {
        try {
            return cpuListCount(java.nio.file.Files.readString(cpuListFile));
        } catch (java.io.IOException | RuntimeException e) {
            return OptionalInt.empty();
        }
    }

    /** Counts a kernel CPU list ({@code "0-63"}, {@code "0-3,8-11"}, {@code "5"}); empty when it is not one. */
    static OptionalInt cpuListCount(String list) {
        if (list == null || list.isBlank()) return OptionalInt.empty();
        int count = 0;
        for (String part : list.trim().split(",")) {
            String[] ends = part.trim().split("-", -1);
            try {
                int lo = Integer.parseInt(ends[0].trim());
                int hi = ends.length == 2 ? Integer.parseInt(ends[1].trim()) : lo;
                if (ends.length > 2 || lo < 0 || hi < lo) return OptionalInt.empty();
                count += hi - lo + 1;
            } catch (NumberFormatException e) {
                return OptionalInt.empty();
            }
        }
        return count > 0 ? OptionalInt.of(count) : OptionalInt.empty();
    }

    /**
     * Env-injectable resolver (tests never mutate real process env).
     *
     * <p>Empty when {@code NX_ONNX_INTRA_OP_THREADS} is unset or blank: no
     * operator override. The no-arg entry point then applies the CPU-limit
     * rule or keeps ORT's default (class javadoc). A set value is applied verbatim; a
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
