// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.util.function.Function;

/**
 * Request-scoped embed-deadline resolution (nexus-8hdg9 phase 2, write-path
 * cancellation on client disconnect).
 *
 * <p>Generalizes {@code VoyageRetryLoop}'s existing request-scoped-deadline
 * idiom (nexus-99r7y): {@code newDeadlineNanos()} there mints one {@code
 * System.nanoTime()}-based deadline per logical request for a single
 * embedder's 429 budget; {@link #newDeadlineNanos(long)} here is the same
 * arithmetic, parameterized so {@link AuthFilter} can mint one deadline per
 * HTTP request covering every route's embed call, not just one embedder's
 * retry loop.
 *
 * <p>{@link AuthFilter} resolves {@link #deadlineMsFromEnv()} ONCE at
 * construction (mirrors {@code LocalOnnxAdmission.fromEnv()}'s resolve-once-
 * at-boot shape) and mints a fresh {@link #newDeadlineNanos(long)} per
 * request, parked in {@link RequestContext} alongside the resolved {@code
 * Principal} and cleared with it.
 *
 * <p><b>Not upsert-specific</b> (critique remediation, T2 {@code
 * critique-nexus-8hdg9-p2-5ce59b36d} [24651] finding 2): {@link
 * dev.nexus.service.vectors.RequestDeadlineExceededException}'s javadoc
 * carries the full rationale -- in short, {@code AuthFilter} mints this
 * deadline for EVERY request regardless of route, and the same synchronous
 * embed call serves upsert-chunks AND search/query, so {@link
 * #DEADLINE_MS_ENV}'s name is a general "how long may this request's embed
 * work run" knob, not an upsert-only one. Scoping the mint to write-shaped
 * routes only was rejected: it would make {@code AuthFilter} parse route
 * semantics for no real benefit, since the search path's own client-side
 * timeout (120s) is already tighter than this deadline's default.
 *
 * <p>This class is plumbing only (phase 2): nothing yet reads {@link
 * RequestContext#deadlineNanos()} inside an embed loop. The check points
 * that raise {@code RequestDeadlineExceededException} from an expired
 * deadline are phases 3 and 4 (nexus-8hdg9.3/.4), each gated on a timed
 * A/B per the design record's throughput-risk section before landing.
 */
final class RequestDeadline {

    /** Spawn-env override for the request embed deadline, in milliseconds. */
    static final String DEADLINE_MS_ENV = "NX_EMBED_DEADLINE_MS";

    /**
     * Default budget when the env override is absent, in milliseconds.
     *
     * <p>Sized deliberately BELOW the Python client's upsert socket timeout
     * ({@code _UPSERT_CHUNKS_TIMEOUT_S = 600} at {@code http_vector_client.py}'s
     * two {@code /v1/vectors/upsert-chunks} call sites) so a genuinely stuck
     * request gets an honest, actionable 503 from the server before the
     * client's own socket read simply times out with no detail. That
     * ordering is a cross-language invariant, not just a comment: {@code
     * tests/test_embed_deadline_default_ordering.py} reads this constant's
     * declaration out of THIS source file (no shared Java/Python constant
     * file exists) and fails if a future edit here loses the margin. Not yet
     * measured against a real indexing run (that is phases 3/4's job, per
     * the design record's throughput-risk section) -- generous on purpose so
     * a healthy request never trips it before that measurement exists.
     */
    static final long DEFAULT_DEADLINE_MS = 300_000L;

    /**
     * Advisory request header carrying the CLIENT's own embed budget in
     * milliseconds (nexus-8hdg9 phase 5). The Python client stamps it on
     * {@code /v1/vectors/upsert-chunks} from its socket timeout minus a
     * margin ({@code _UPSERT_CHUNKS_DEADLINE_MS} in {@code
     * http_vector_client.py}), so the server's deadline is sourced from the
     * budget the caller will actually wait rather than a server guess.
     * Wire-additive both directions: absent on an old client, the env
     * default applies unchanged; an old engine ignores an unknown header.
     */
    public static final String REQUEST_DEADLINE_HEADER = "X-Nexus-Request-Deadline-Ms";

    private RequestDeadline() {
    }

    /**
     * Resolve ONE request's budget from the advisory header against the
     * env-resolved default (nexus-8hdg9 phase 5).
     *
     * <ul>
     *   <li>Absent or blank header: {@code envDefaultMs}, unchanged.</li>
     *   <li>Malformed (non-numeric) or non-positive header: {@code
     *       envDefaultMs}. IGNORED, not a 400 -- the header is advisory and a
     *       bad value must never turn a valid write into a client error;
     *       contrast {@link #deadlineMsFromEnv(Function)}, which refuses a bad
     *       OPERATOR setting loudly because that is a boot-time
     *       misconfiguration, not a per-request hint.</li>
     *   <li>Present, positive, numeric header: WINS OUTRIGHT, larger than the
     *       env default included. The design's reason for the header (T2
     *       design-nexus-8hdg9-write-cancellation §2 Option C) is that a
     *       server-guessed deadline "too tight kills healthy slow requests",
     *       mitigated by sourcing the deadline from the client's own budget --
     *       a client that will wait 540s must not be aborted at a 300s server
     *       guess. The env default is the fallback for the absent/malformed
     *       cases only, not a ceiling; the header value is itself bounded by
     *       the client's own socket timeout (it is derived from it minus a
     *       margin), so a cooperating client cannot declare an unbounded
     *       budget.</li>
     * </ul>
     */
    static long resolveBudgetMs(String headerValue, long envDefaultMs) {
        if (headerValue == null || headerValue.isBlank()) {
            return envDefaultMs;
        }
        long requested;
        try {
            requested = Long.parseLong(headerValue.trim());
        } catch (NumberFormatException e) {
            return envDefaultMs;
        }
        if (requested <= 0) {
            return envDefaultMs;
        }
        return requested;
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
     * Mint the embed deadline for ONE request, generalizing {@code
     * VoyageRetryLoop.newDeadlineNanos()}'s idiom (nexus-99r7y) from a
     * single embedder's 429 budget to every route's embed call.
     */
    static long newDeadlineNanos(long budgetMs) {
        return System.nanoTime() + budgetMs * 1_000_000L;
    }
}
