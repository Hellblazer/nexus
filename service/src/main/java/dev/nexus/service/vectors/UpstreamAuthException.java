// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

/**
 * The upstream embedding provider (Voyage AI) rejected the service's
 * credentials (HTTP 401/403). This is an operator/config problem — a dead,
 * rotated, or wrong-scope API key — not an engine defect, and it must never
 * surface to clients as an opaque {@code 500 internal server error}
 * (nexus-pmhpc: a dead key turned store-put, search, query, and
 * hybrid-search into indistinguishable 500s).
 *
 * <p>{@code VectorHandler} maps this to {@code 502} with the actionable
 * detail, parallel to {@link EmbeddingModelUnavailableException}'s 422.
 */
public class UpstreamAuthException extends RuntimeException {
    public UpstreamAuthException(String message) {
        super(message);
    }
}
