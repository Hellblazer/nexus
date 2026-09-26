// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * Thrown when a request's embed deadline has already elapsed at a check
 * point (nexus-8hdg9, write-path cancellation on client disconnect).
 *
 * <p>The deadline is request-scoped, minted once by {@code AuthFilter} from
 * {@code RequestDeadline.deadlineMsFromEnv()} and parked in {@code
 * RequestContext} alongside the resolved {@code Principal} (phase 2,
 * nexus-8hdg9.2) -- generalizing {@code VoyageRetryLoop}'s existing
 * request-scoped-deadline idiom (nexus-99r7y, {@code newDeadlineNanos()}
 * plus remaining-budget arithmetic) from a single embedder's 429 budget to
 * the whole request. This phase adds only the exception and its HTTP
 * mapping; no embed-loop check point raises it yet -- those are phases
 * 3 and 4 ({@code Bge768Embedder.embedSubBatched}, {@code
 * CceEmbedder.embedParallel}), each requiring a timed A/B before landing.
 *
 * <p><b>Not upsert-specific</b> (critique remediation, T2 {@code
 * critique-nexus-8hdg9-p2-5ce59b36d} [24651]): the SAME synchronous embed
 * call ({@code EmbedderRouter.embedForCollectionWithUsage}) serves both
 * {@code /v1/vectors/upsert-chunks} and the search/query routes ({@code
 * PgVectorRepository.searchWithTokens} embeds the query text on the same
 * request thread), so once phases 3/4 wire a check point this exception can
 * surface from either. {@code AuthFilter} mints the deadline unconditionally
 * for every request regardless of route, so the env var driving the budget
 * (see {@code RequestDeadline.DEADLINE_MS_ENV}, {@code NX_EMBED_DEADLINE_MS})
 * is named generically rather than as an upsert-only knob -- scoping the mint
 * to write-shaped routes only was considered and rejected: it would require
 * {@code AuthFilter} to parse route semantics it otherwise has no reason to
 * know, for a search path whose OWN client-side timeout (120s, well under
 * this deadline's 300s default) already fails it faster than this deadline
 * ever could.
 *
 * <p>Mapped to HTTP 503 by {@code VectorHandler} -- deliberately INSIDE the
 * client's {@code _GATEWAY_RETRY_CODES} ladder ({@code 502,503,504},
 * {@code http_vector_client.py}). A request that ran out of its own deadline
 * is an honest slow-server signal: the client's gateway retry + backoff is
 * the correct response, not a silent hang or an opaque 500. Contrast {@link
 * VoyageTooManyTokensException}, which is deliberately mapped OUTSIDE that
 * ladder because resending an oversize body is guaranteed to fail again --
 * this exception has no such guarantee, so retrying is the right default.
 *
 * <p><b>Retry-After (critique remediation):</b> carries an explicit {@code
 * retryAfterSeconds}, mapped by {@code VectorHandler} into the same {@code
 * Retry-After} header + {@code retry_after_seconds} body field shape {@link
 * UpstreamRateLimitedException}'s 429 arm already uses -- without it, the
 * client's gateway retry (fixed {@code 2.0, 5.0, 10.0}s schedule) would fire
 * three more full-cost synchronous embeds against a server that just told it
 * the current one ran out of budget, once phases 3/4 make this reachable in
 * production. {@link #DEFAULT_RETRY_AFTER_SECONDS} is the value a future
 * check point should pass absent a more specific signal (there is no
 * upstream {@code Retry-After} header to honor here, unlike the 429 case).
 */
public final class RequestDeadlineExceededException extends RuntimeException {

    /**
     * Suggested client wait, in seconds, for a caller that has no more
     * specific signal to derive one from -- short enough that a genuinely
     * transient overload paces down quickly, long enough that the gateway
     * retry's own {@code 2.0, 5.0, 10.0}s schedule cannot immediately
     * re-fire three more full-cost embeds against the same loaded server.
     */
    public static final long DEFAULT_RETRY_AFTER_SECONDS = 5L;

    /**
     * What the caller loses by retrying (nexus-qajw7), surfaced by {@code VectorHandler}
     * as the {@code X-Nexus-Deadline-Outcome} header and the {@code deadline_outcome} body
     * field so the client can budget its retries by it.
     */
    public enum Outcome {
        /** Refused before any embedding started: a retry costs one cheap request, and a
         *  queue that drains in the meantime can admit it. */
        REFUSED("refused"),
        /** Aborted after work was submitted, even if none of it had finished: batches
         *  may have returned or still be in flight, and all of it is discarded. A retry
         *  redoes that work, which Voyage bills again or, for a local embedder such as
         *  bge768, spends as CPU again. The two refusal sites are the only places where
         *  nothing was submitted; anything else is ABORTED. */
        ABORTED("aborted");

        private final String wire;

        Outcome(String wire) {
            this.wire = wire;
        }

        /** The lowercase value sent on the wire. */
        public String wire() {
            return wire;
        }
    }

    private final long retryAfterSeconds;
    private final Outcome outcome;

    /** An {@link Outcome#ABORTED} deadline; the conservative default, since it is the
     *  outcome the client does not widen its retries for. */
    public RequestDeadlineExceededException(String message, long retryAfterSeconds) {
        this(message, retryAfterSeconds, Outcome.ABORTED);
    }

    public RequestDeadlineExceededException(String message, long retryAfterSeconds, Outcome outcome) {
        super(message);
        this.retryAfterSeconds = retryAfterSeconds;
        this.outcome = outcome;
    }

    /** Suggested client wait, surfaced as the response's {@code Retry-After}. */
    public long retryAfterSeconds() {
        return retryAfterSeconds;
    }

    /** Whether work was discarded; see {@link Outcome}. */
    public Outcome outcome() {
        return outcome;
    }
}
