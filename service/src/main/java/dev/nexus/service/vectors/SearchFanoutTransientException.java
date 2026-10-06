// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * A per-collection search statement failed transiently while serving
 * {@code POST /v1/vectors/search-per-collection} (nexus-tu8wp.1): a lost or refused connection
 * (class 08), an operator shutdown (57P), a serialization failure or deadlock (40001, 40P01),
 * insufficient resources (class 53: disk full, out of memory, too many connections) or a lock
 * timeout (55P03).
 *
 * <p>The whole request fails with this, never a partial result: a collection that errored this
 * way says nothing about the collection itself, so it must not be reported as one that is empty.
 * {@code VectorHandler} maps it to 503 with a {@code Retry-After}, inside the client's gateway
 * retry codes. A STATEMENT TIMEOUT (57014) is deliberately not this type: it is isolated to its
 * collection (Sam, 2026-10-05), because a deterministic slow collection would otherwise fail every
 * search of its model group. Pool or admission exhaustion does not use this type either: it
 * already carries the typed {@link java.sql.SQLTransientConnectionException} the shared 503
 * ladder reads.
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
