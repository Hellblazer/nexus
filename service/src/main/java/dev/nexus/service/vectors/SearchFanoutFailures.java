// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * The classification of a failure that stopped a search fan-out, moved out of {@link PgVectorRepository} unchanged so that class stays
 * under its file-size ceiling (RDR-229 P3.5c assembly, nexus-p2m1x.25).
 */
final class SearchFanoutFailures {

    private SearchFanoutFailures() { /* static */ }

    /**
     * SQLSTATEs that mean "try again", for the failures that are NOT isolated per collection: a
     * statement timeout (57014) is settled per arm before it gets here. Class 08 connection
     * exceptions, 57P operator intervention (shutdown, cannot-connect-now), 40001 and 40P01
     * (serialization and deadlock, retryable by definition), class 53 insufficient resources
     * (53100 disk full, 53200 out of memory, 53300 too many connections) and 55P03 (lock not
     * available).
     */
    static boolean isTransientSqlState(String state) {
        return state != null && (state.startsWith("57P") || state.startsWith("08") || state.startsWith("53")
            || state.equals("40001") || state.equals("40P01") || state.equals("55P03"));
    }

    /**
     * Classify the failure that stopped a fan-out. Pool or admission exhaustion and an expired
     * request budget already carry the typed shapes the handler maps to 503, so they pass through
     * unchanged; any other transient SQL failure becomes a {@link SearchFanoutTransientException},
     * also 503. Anything else is a request-level failure and propagates as it is.
     */
    static RuntimeException of(Throwable failed) {
        if (failed instanceof Error err) {
            throw err;
        }
        RuntimeException re = failed instanceof RuntimeException r ? r : new RuntimeException(failed);
        if (re instanceof RequestDeadlineExceededException) {
            return re;
        }
        String sqlState = null;
        int depth = 0;
        for (Throwable c = re; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof java.sql.SQLTransientConnectionException) {
                return re;
            }
            if (sqlState == null && c instanceof java.sql.SQLException sql) {
                sqlState = sql.getSQLState();
            }
        }
        if (isTransientSqlState(sqlState)) {
            return new SearchFanoutTransientException(
                "a per-collection search statement failed transiently (SQLSTATE " + sqlState
                + "); no partial results are returned, retry", sqlState, re);
        }
        return re;
    }
}
