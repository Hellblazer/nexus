// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * RDR-188: the upstream rerank provider (Voyage AI) failed in a non-auth way —
 * retries exhausted on 429/5xx, a non-retryable 4xx, a network failure, or an
 * unparseable/invalid response body.
 *
 * <p>Typed so the {@code /v1/vectors/*} handlers can convert the failure into a
 * LOUD structured degraded-rerank field on the search response (results still
 * served in distance order, degradation visible to the caller) — never a silent
 * fallback to input order, which is exactly the client-side anti-pattern
 * ({@code scoring._rerank_cloud}'s broad WARN-only except) this RDR retires.
 * Auth failures (401/403) raise {@link UpstreamAuthException} instead, matching
 * the embed path's 502 mapping.
 */
public class RerankUpstreamException extends RuntimeException {
    public RerankUpstreamException(String message) {
        super(message);
    }

    public RerankUpstreamException(String message, Throwable cause) {
        super(message, cause);
    }
}
