// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.SQLException;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Supplier;

/**
 * Shared deadlock-retry belt for the service's multi-row
 * {@code INSERT ... ON CONFLICT (...) DO UPDATE} write paths (bead nexus-ps9wb).
 *
 * <p><strong>Background.</strong> Two concurrent multi-row upsert batches into the
 * same table that touch an overlapping set of conflict keys in DIFFERENT arrival
 * orders lock the shared rows in opposite orders within their single statement →
 * lock cycle → Postgres kills a victim with {@code deadlock detected} (SQLSTATE
 * 40P01) → the caller sees HTTP 500. The PRIMARY fix at each call site is to sort a
 * batch's rows by the ON CONFLICT key before binding, so every concurrent batch
 * acquires row locks in one global order and no cycle can form. This helper is the
 * BELT for residual deadlocks the per-site sort cannot rule out — a concurrent
 * delete, or a different repository writing an overlapping keyspace in its own order.
 *
 * <p><strong>Safety.</strong> The deadlock victim's transaction is ALREADY rolled
 * back by Postgres before the exception surfaces, so re-running an idempotent
 * {@code ON CONFLICT DO UPDATE} batch is safe (no partial state, no double effect).
 * Attempts are bounded ({@link #MAX_ATTEMPTS}) with jittered backoff; the original
 * exception propagates unchanged on exhaustion or for any non-deadlock error. The
 * wrapped unit MUST be only the DB transaction — never anything with external side
 * effects (e.g. embedding calls), or a retry would repeat them.
 */
public final class DeadlockRetry {

    private static final Logger log = LoggerFactory.getLogger(DeadlockRetry.class);

    /** Total attempts (initial try + retries) before the deadlock propagates. */
    static final int MAX_ATTEMPTS = 4;

    /** PostgreSQL SQLSTATE for {@code deadlock_detected}. */
    static final String SQLSTATE_DEADLOCK = "40P01";

    private DeadlockRetry() {
    }

    /**
     * Run {@code writeTxn} (a single idempotent DB transaction), retrying on a
     * deadlock (SQLSTATE 40P01) up to {@link #MAX_ATTEMPTS} total attempts.
     *
     * @param context short label for the retry log line (e.g. the collection name)
     * @param writeTxn the transaction to run; MUST be free of external side effects
     */
    public static void run(String context, Runnable writeTxn) {
        run(context, () -> {
            writeTxn.run();
            return null;
        });
    }

    /**
     * {@link #run(String, Runnable)} variant returning the transaction's value.
     */
    public static <T> T run(String context, Supplier<T> writeTxn) {
        int attempt = 0;
        while (true) {
            try {
                return writeTxn.get();
            } catch (RuntimeException ex) {
                if (isDeadlock(ex) && ++attempt < MAX_ATTEMPTS) {
                    log.warn("event=deadlock_retry context={} attempt={} maxAttempts={}",
                            context, attempt, MAX_ATTEMPTS);
                    backoff(attempt);
                    continue;
                }
                throw ex;
            }
        }
    }

    /**
     * True if a {@code deadlock_detected} (SQLSTATE 40P01) SQLException appears
     * anywhere in {@code t}'s cause chain. jOOQ wraps the driver SQLException in a
     * {@code DataAccessException} (and TenantScope may further wrap that in a
     * RuntimeException), so the SQLException is a cause, not the top-level throwable —
     * hence the depth-bounded chain walk, which handles either wrapper.
     */
    static boolean isDeadlock(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se && SQLSTATE_DEADLOCK.equals(se.getSQLState())) {
                return true;
            }
        }
        return false;
    }

    private static void backoff(int attempt) {
        long ms = Math.min(attempt, MAX_ATTEMPTS) * (10L + ThreadLocalRandom.current().nextLong(20));
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new RuntimeException("interrupted during deadlock-retry backoff", ie);
        }
    }
}
