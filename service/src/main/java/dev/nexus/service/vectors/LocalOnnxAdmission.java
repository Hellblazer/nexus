// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.SQLTransientConnectionException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

/**
 * nexus-00wsf residual, review round 2 (T2 {@code nexus/review-00wsf-admission-intraop-e35ef547a-2026-09-04},
 * [24238], and the paired critic pass [24239]) — the ONE process-wide admission
 * gate for local (bge/ONNX) CPU-bound work: embeds AND reranks.
 *
 * <p>Round 1's {@code AdmissionControlledEmbedder} owned its own {@link
 * Semaphore} and was constructed once per {@code EmbedderRouter}. Production
 * ({@code Main.java}) builds ONE local embedder but TWO routers (document,
 * query), so that shape admitted up to 2x the intended bound — the exact
 * "bound the SUM of concurrent local embeds" goal the diagnosis (T2 [24231])
 * set out to meet was not met. This class is the fix: constructed EXACTLY
 * ONCE per process ({@code Main.java}'s local-mode branch), its single
 * {@link Semaphore} is shared by every {@link AdmissionControlledEmbedder}
 * AND {@link AdmissionControlledReranker} wrapper in that process, however
 * many routers or callers hold a reference to one.
 *
 * <p><b>What this bounds, and what it does not (critic finding 2).</b> This
 * is a COUNT bound — at most {@link #permits()} local ONNX calls (embed or
 * rerank) run at once. It is a CPU-derived proxy for the resource that
 * actually blew up in the nexus-00wsf incident: MEMORY, specifically the
 * padded-token-area of an in-flight attention tensor. {@code
 * Bge768Embedder.MAX_PADDED_TOKEN_AREA} already bounds the area of ONE call;
 * this bounds how many calls run at once, not the SUM of their areas — two
 * calls each at the per-call area ceiling still admit roughly 2x that memory
 * concurrently. A token-area-WEIGHTED admission bound (acquire N "area
 * units" sized to the actual request before delegating, release them after)
 * would be the tighter fix and is a named follow-up, not implemented here:
 * {@code embedSubBatched} already computes padded-token-area per sub-batch,
 * so the input is cheap to obtain — the gap is threading that area out to
 * this layer BEFORE {@code delegate.embed()} runs, which this round's scope
 * (bound the call count, coordinate it with the intra-op thread count) does
 * not cover.
 *
 * <p>Two acquisition policies, chosen per caller (see {@link
 * AdmissionControlledEmbedder}'s {@code interactive} parameter):
 * <ul>
 *   <li><b>Non-interactive (bulk indexing, document embeds):</b> {@link
 *       #acquireInterruptible()} — blocks until a permit is free, same as
 *       {@link CceEmbedder}'s {@code inFlight.acquire()} (interruptible, NOT
 *       {@code acquireUninterruptibly} — critic finding 1: the original
 *       javadoc claimed to mirror CceEmbedder's idiom while actually using
 *       the uninterruptible variant CceEmbedder does not use). Bulk indexing
 *       is not latency-sensitive; queueing behind other local-embed work is
 *       the correct, and previously entirely absent, behaviour.</li>
 *   <li><b>Interactive (query embeds, reranks):</b> {@link
 *       #tryAcquire(long)} with a bounded timeout — a bulk-indexing burst
 *       must not stall interactive search unboundedly (review finding 3).
 *       On timeout the caller fails loud with a typed retryable signal (see
 *       {@link AdmissionControlledEmbedder}), never silently degrades or
 *       waits forever.</li>
 * </ul>
 *
 * <p><b>The admission wait and the PG statement timeout are ADDITIVE, not
 * shared (review round 3 finding).</b> {@link #tryAcquire(long)} runs
 * BEFORE the caller ever reaches {@code PgSession.setSearchStatementTimeout}
 * on the subsequent PG statement — the two budgets stack (admission wait +
 * embed time + PG statement time), they do not bound the same window. Round
 * 2 defaulted the query-admission timeout to the FULL search-statement-
 * timeout value, which meant a contended search could take up to roughly
 * 2x the intended SLA before the client's own retry logic even started.
 * {@link #queryTimeoutMsFromEnv()} now defaults to ONE THIRD of the search-
 * statement-timeout budget instead, so admission-wait + embed + PG-statement
 * stays within roughly the OLD single-budget envelope rather than doubling
 * it. {@code NX_QUERY_EMBED_ADMISSION_TIMEOUT_MS} still overrides this
 * directly when an operator wants a different split.
 *
 * <p><b>Unmeasured (critic finding 3).</b> Neither this class nor its
 * callers assert or claim a specific query-path latency number from the
 * admission bound or the paired {@link OnnxThreadPolicy} intra-op cap — the
 * effect is real by construction (bounded concurrency vs. none) but has not
 * been benchmarked end-to-end.
 *
 * <p><b>Follow-up, not implemented here (review round 3).</b> A tighter fix
 * for the additive-budget problem than a smaller shared timeout would be a
 * permits-conditional interactive reservation (dedicate one permit
 * exclusively to interactive callers whenever {@code permits > 1}, so a
 * query never queues behind bulk indexing at all) or priority preemption
 * (an interactive waiter jumps ahead of already-queued non-interactive
 * waiters); this round scopes to fixing the double-budget default, not
 * eliminating admission contention between the two caller classes.
 */
public final class LocalOnnxAdmission {

    private static final Logger log = LoggerFactory.getLogger(LocalOnnxAdmission.class);

    /** Spawn-env override for the admission bound (permits, not a ratio). */
    static final String PERMITS_ENV = "NX_LOCAL_EMBED_ADMISSION_PERMITS";

    /** Spawn-env override for the interactive (query/rerank) admission timeout. */
    static final String QUERY_TIMEOUT_ENV = "NX_QUERY_EMBED_ADMISSION_TIMEOUT_MS";

    private final int      permits;
    private final long     queryTimeoutMs;
    private final Semaphore inFlight;

    /**
     * Production factory: permits and query timeout resolved from real
     * env/cores. Logs ONE combined boot line naming both the derived
     * timeout AND the search-statement-timeout it was derived from, so the
     * additive-not-shared relationship documented in the class javadoc is
     * inspectable at boot, not just in a source comment.
     */
    public static LocalOnnxAdmission fromEnv() {
        long searchStatementTimeoutMs =
                dev.nexus.service.db.PgSession.startupSearchStatementTimeoutMs();
        int  permits        = permitsFromEnv();
        long queryTimeoutMs = queryTimeoutMsFromEnv(System::getenv, searchStatementTimeoutMs);
        log.info("event=local_onnx_admission_configured permits={} query_timeout_ms={} "
                + "search_statement_timeout_ms={} "
                + "note=admission_wait_precedes_pg_statement_timeout_budgets_are_additive",
                permits, queryTimeoutMs, searchStatementTimeoutMs);
        return new LocalOnnxAdmission(permits, queryTimeoutMs);
    }

    /**
     * @param permits        concurrent-local-ONNX-call ceiling (positive)
     * @param queryTimeoutMs interactive (query/rerank) admission timeout in ms (positive)
     */
    public LocalOnnxAdmission(int permits, long queryTimeoutMs) {
        if (permits <= 0) {
            throw new IllegalArgumentException(
                    "admission permits must be positive, got " + permits);
        }
        if (queryTimeoutMs <= 0) {
            throw new IllegalArgumentException(
                    "query embed admission timeout must be positive, got " + queryTimeoutMs);
        }
        this.permits        = permits;
        this.queryTimeoutMs = queryTimeoutMs;
        // Fair: a burst of concurrent local-embed requests cannot starve an
        // earlier arrival — same fairness choice as TenantScope.ADMISSION.
        this.inFlight = new Semaphore(permits, true);
        // Boot-time visibility lives in fromEnv() (the one production call
        // site), which also logs the search-statement-timeout this
        // constructor's queryTimeoutMs was derived from — logging again
        // here would duplicate that line for every fromEnv() call and add
        // noise to the many direct-construction tests in this package.
    }

    /** The configured concurrent-local-ONNX-call ceiling. */
    int permits() {
        return permits;
    }

    /** The configured interactive (query/rerank) admission timeout, in ms. */
    long queryTimeoutMs() {
        return queryTimeoutMs;
    }

    /**
     * Non-interactive acquire: blocks until a permit is free. Interruptible
     * — mirrors {@link CceEmbedder}'s {@code inFlight.acquire()} idiom
     * exactly (critic finding 1), not {@code acquireUninterruptibly}.
     */
    void acquireInterruptible() throws InterruptedException {
        inFlight.acquire();
    }

    /**
     * Interactive acquire: waits at most {@code timeoutMs} for a permit.
     *
     * @return true if admitted, false if the timeout elapsed first
     */
    boolean tryAcquire(long timeoutMs) throws InterruptedException {
        return inFlight.tryAcquire(timeoutMs, TimeUnit.MILLISECONDS);
    }

    void release() {
        inFlight.release();
    }

    /**
     * The typed-retryable failure for an interactive caller that could not
     * get a permit within its timeout — mirrors {@code TenantScope}'s
     * admission-timeout idiom exactly: a {@link SQLTransientConnectionException}
     * in the cause chain, so {@code HttpUtil.isPoolExhausted} maps it to a
     * 503 with zero new error-mapping surface at every handler that already
     * runs requests through {@code HttpUtil.sendTypedDbError}.
     */
    static RuntimeException admissionTimeoutException(String what, long timeoutMs) {
        return new RuntimeException(
                "local ONNX admission queue full for " + what + " after " + timeoutMs + "ms",
                new SQLTransientConnectionException(
                        "admission queue full after " + timeoutMs + "ms (retryable)"));
    }

    // ── permits resolver ─────────────────────────────────────────────────

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
     * parallelism (which now DERIVES its own default from this permits value —
     * see that class), so admitting more than half the box at once would
     * still let concurrent requests fully oversubscribe every core. {@code
     * NX_LOCAL_EMBED_ADMISSION_PERMITS} overrides the default; a non-positive
     * or non-numeric override is REFUSED loudly rather than silently coerced
     * (no-silent-fallbacks-for-correctness).
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

    // ── query-embed admission timeout resolver ──────────────────────────

    /** Production entry point: real env, default derived from the search statement timeout. */
    static long queryTimeoutMsFromEnv() {
        return queryTimeoutMsFromEnv(
                System::getenv, dev.nexus.service.db.PgSession.startupSearchStatementTimeoutMs());
    }

    /**
     * Env-injectable resolver.
     *
     * <p>Default = ONE THIRD of {@code searchStatementTimeoutMs} (review
     * round 3 — see the class javadoc's "additive, not shared" section).
     * Round 2 defaulted this to the FULL search-statement-timeout value,
     * reasoning it should be "sized to the search statement-timeout
     * budget" — true in spirit, wrong in arithmetic: the admission wait
     * happens BEFORE {@code PgSession.setSearchStatementTimeout} is ever
     * applied to the later PG statement, so the two windows stack instead
     * of sharing a budget, and a full-budget default let a contended
     * search take up to ~2x the intended SLA before the client's retry
     * even started. Dividing by three keeps
     * {@code admission-wait + embed + PG-statement} within roughly the OLD
     * single-budget envelope. Still an UPPER-BOUND proxy either way, not a
     * guarantee of remaining request budget: neither this default nor an
     * override accounts for time already spent elsewhere in the request
     * before the embed call runs. {@code
     * NX_QUERY_EMBED_ADMISSION_TIMEOUT_MS} overrides the derived default
     * outright; a non-positive or non-numeric override is REFUSED loudly,
     * same discipline as {@link #permitsFromEnv}.
     */
    static long queryTimeoutMsFromEnv(Function<String, String> env, long searchStatementTimeoutMs) {
        String raw = env.apply(QUERY_TIMEOUT_ENV);
        if (raw == null || raw.isBlank()) {
            return Math.max(1, searchStatementTimeoutMs / 3);
        }
        long parsed;
        try {
            parsed = Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    QUERY_TIMEOUT_ENV + " must be an integer, got: " + raw, e);
        }
        if (parsed <= 0) {
            throw new IllegalArgumentException(
                    QUERY_TIMEOUT_ENV + " must be positive, got: " + parsed);
        }
        return parsed;
    }
}
