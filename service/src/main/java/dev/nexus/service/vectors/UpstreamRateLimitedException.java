/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.vectors;

/**
 * The upstream embedding provider (Voyage) is rate-limiting and the caller's
 * bounded retry budget could not absorb it (nexus-99r7y).
 *
 * <p>Typed, following {@link UpstreamAuthException}'s convention, so handlers
 * can map it to an HONEST {@code 429} with a {@code Retry-After} header of our
 * own — well before the public edge's 30s upstream bound turns the wait into
 * an opaque 5xx (measured 2026-08-15 on engine-service-v0.1.76: sustained
 * Voyage 429s at the project-wide 4,000 RPM ceiling burned the old fixed
 * retry attempts inside the edge timeout, and bulk
 * {@code /v1/catalog/manifest/write_many} surfaced 5xx to the client while
 * the engine itself was idle). A client seeing 429+Retry-After can pace
 * itself — the conexus-cy9u7 client-side rate brake keys on exactly that
 * header shape.
 */
public class UpstreamRateLimitedException extends RuntimeException {

    private final long retryAfterSeconds;

    public UpstreamRateLimitedException(String message, long retryAfterSeconds) {
        super(message);
        this.retryAfterSeconds = retryAfterSeconds;
    }

    /** Suggested client wait, surfaced as the response's {@code Retry-After}. */
    public long retryAfterSeconds() {
        return retryAfterSeconds;
    }
}
