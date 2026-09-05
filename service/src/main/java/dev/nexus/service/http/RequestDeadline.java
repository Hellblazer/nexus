// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.util.function.Function;

/**
 * Request-scoped write-path deadline resolution (nexus-8hdg9 phase 2, write-
 * path cancellation on client disconnect).
 *
 * <p>Generalizes {@code VoyageRetryLoop}'s existing request-scoped-deadline
 * idiom (nexus-99r7y): {@code newDeadlineNanos()} there mints one {@code
 * System.nanoTime()}-based deadline per logical request for a single
 * embedder's 429 budget; {@link #newDeadlineNanos(long)} here is the same
 * arithmetic, parameterized so {@link AuthFilter} can mint one deadline per
 * HTTP request covering the whole write path, not just one embedder's retry
 * loop.
 *
 * <p>{@link AuthFilter} resolves {@link #deadlineMsFromEnv()} ONCE at
 * construction (mirrors {@code LocalOnnxAdmission.fromEnv()}'s resolve-once-
 * at-boot shape) and mints a fresh {@link #newDeadlineNanos(long)} per
 * request, parked in {@link RequestContext} alongside the resolved {@code
 * Principal} and cleared with it.
 *
 * <p>This class is plumbing only (phase 2): nothing yet reads {@link
 * RequestContext#deadlineNanos()} inside an embed loop. The check points
 * that raise {@code RequestDeadlineExceededException} from an expired
 * deadline are phases 3 and 4 (nexus-8hdg9.3/.4), each gated on a timed
 * A/B per the design record's throughput-risk section before landing.
 */
final class RequestDeadline {

    /** Spawn-env override for the write-path request deadline, in milliseconds. */
    static final String DEADLINE_MS_ENV = "NX_UPSERT_EMBED_DEADLINE_MS";

    /**
     * Default budget when the env override is absent, in milliseconds.
     *
     * <p>Sized deliberately BELOW the Python client's upsert socket timeout
     * ({@code timeout=600} at {@code http_vector_client.py}'s {@code
     * /v1/vectors/upsert-chunks} call site) so a genuinely stuck request
     * gets an honest, actionable 503 from the server before the client's own
     * socket read simply times out with no detail. Not yet measured against
     * a real indexing run (that is phases 3/4's job, per the design
     * record's throughput-risk section) -- generous on purpose so a healthy
     * request never trips it before that measurement exists.
     */
    static final long DEFAULT_DEADLINE_MS = 300_000L;

    private RequestDeadline() {
    }

    /** Production entry point: real env. */
    static long deadlineMsFromEnv() {
        return deadlineMsFromEnv(System::getenv);
    }

    /**
     * Env-injectable resolver (tests never mutate real process env -- mirrors
     * {@code LocalOnnxAdmission.permitsFromEnv}/{@code queryTimeoutMsFromEnv}'s
     * injection pattern). A non-positive or non-numeric override is REFUSED
     * loudly rather than silently coerced (no-silent-fallbacks-for-correctness).
     */
    static long deadlineMsFromEnv(Function<String, String> env) {
        String raw = env.apply(DEADLINE_MS_ENV);
        if (raw == null || raw.isBlank()) {
            return DEFAULT_DEADLINE_MS;
        }
        long parsed;
        try {
            parsed = Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    DEADLINE_MS_ENV + " must be an integer, got: " + raw, e);
        }
        if (parsed <= 0) {
            throw new IllegalArgumentException(
                    DEADLINE_MS_ENV + " must be positive, got: " + parsed);
        }
        return parsed;
    }

    /**
     * Mint the write-path deadline for ONE request, generalizing {@code
     * VoyageRetryLoop.newDeadlineNanos()}'s idiom (nexus-99r7y) from a
     * single embedder's 429 budget to the whole write path.
     */
    static long newDeadlineNanos(long budgetMs) {
        return System.nanoTime() + budgetMs * 1_000_000L;
    }
}
