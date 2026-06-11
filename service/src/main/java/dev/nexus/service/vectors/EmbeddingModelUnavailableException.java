// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * Thrown when a collection's embedding-model segment (RDR-103 name authority)
 * names a model the service cannot embed for in its current mode — e.g. a
 * {@code voyage-*} collection while the service has no Voyage credentials
 * (bead nexus-pebfx.2: no-silent-fallbacks-for-correctness).
 *
 * <p>Mapped to HTTP 422 by {@code VectorHandler}: the request is well-formed
 * (so not 400) but unprocessable in this service configuration. Distinct from
 * {@link IllegalArgumentException} (→ 400) so a misconfigured service is
 * distinguishable from a malformed request at the client.
 */
public final class EmbeddingModelUnavailableException extends RuntimeException {

    public EmbeddingModelUnavailableException(String message) {
        super(message);
    }
}
