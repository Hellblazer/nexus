// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * A per-collection search statement failed transiently (a statement timeout above all) while
 * serving {@code POST /v1/vectors/search-per-collection} (nexus-tu8wp.1).
 *
 * <p>The whole request fails with this, never a partial result: a collection that errored must
 * not be indistinguishable from one that is empty. {@code VectorHandler} maps it to 503 with a
 * {@code Retry-After}, inside the client's gateway retry codes. Pool or admission exhaustion
 * does not use this type: it already carries the typed
 * {@link java.sql.SQLTransientConnectionException} the shared 503 ladder reads.
 */
public final class SearchFanoutTransientException extends RuntimeException {

    private final String sqlState;

    SearchFanoutTransientException(String message, String sqlState, Throwable cause) {
        super(message, cause);
        this.sqlState = sqlState;
    }

    /** The SQLSTATE that classified the failure as transient (for example {@code 57014}). */
    public String sqlState() {
        return sqlState;
    }
}
