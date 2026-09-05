// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * Thrown when a request's write-path deadline has already elapsed at a check
 * point (nexus-8hdg9, write-path cancellation on client disconnect).
 *
 * <p>The deadline is request-scoped, minted once by {@code AuthFilter} from
 * {@code RequestDeadline.deadlineMsFromEnv()} and parked in {@code
 * RequestContext} alongside the resolved {@code Principal} (phase 2,
 * nexus-8hdg9.2) -- generalizing {@code VoyageRetryLoop}'s existing
 * request-scoped-deadline idiom (nexus-99r7y, {@code newDeadlineNanos()}
 * plus remaining-budget arithmetic) from a single embedder's 429 budget to
 * the whole write path. This phase adds only the exception and its HTTP
 * mapping; no embed-loop check point raises it yet -- those are phases
 * 3 and 4 ({@code Bge768Embedder.embedSubBatched}, {@code
 * CceEmbedder.embedParallel}), each requiring a timed A/B before landing.
 *
 * <p>Mapped to HTTP 503 by {@code VectorHandler} -- deliberately INSIDE the
 * client's {@code _GATEWAY_RETRY_CODES} ladder ({@code 502,503,504},
 * {@code http_vector_client.py}). A request that ran out of its own deadline
 * is an honest slow-server signal: the client's gateway retry + backoff is
 * the correct response, not a silent hang or an opaque 500. Contrast {@link
 * VoyageTooManyTokensException}, which is deliberately mapped OUTSIDE that
 * ladder because resending an oversize body is guaranteed to fail again --
 * this exception has no such guarantee, so retrying is the right default.
 */
public final class RequestDeadlineExceededException extends RuntimeException {

    public RequestDeadlineExceededException(String message) {
        super(message);
    }
}
