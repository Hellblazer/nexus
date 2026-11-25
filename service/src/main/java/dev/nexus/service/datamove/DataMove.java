// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.time.OffsetDateTime;

/**
 * One row of {@code nexus.data_moves} (RDR-246, The registry), as the move worker reads it. The row is read on the admin
 * connection only. The retry and pause columns are carried so the failure-class and pause beads (nexus-f3yqd.2.15) read
 * them from the same value; this bead's passes read {@code pausedAt} and nothing else of them.
 */
public record DataMove(
        String label,
        String template,
        String drivingSchema,
        String drivingTable,
        int chunkSize,
        int sleepMs,
        int statementTimeoutMs,
        String verification,
        String onComplete,
        OffsetDateTime registeredAt,
        OffsetDateTime pausedAt,
        String pausedBy,
        int transientRetries,
        OffsetDateTime lastTransientAt,
        String lastTransient,
        int verifyStatementTimeoutMs) {

    /** The column's default: the bound on the finisher's first, READ ONLY verification pass (datamoves-002). */
    public static final int DEFAULT_VERIFY_STATEMENT_TIMEOUT_MS = 60_000;

    /**
     * {@code statement_timeout_ms} of a move whose completion runs with NO statement bound, only a lock wait (Sam, T2
     * {@code nexus/sam-ruling-validate-bounds-lock-wait-only-2026-11-23}): the largest value the column holds. The column's CHECK refuses 0, so
     * this stands for "no bound"; the finisher sets {@code statement_timeout} 0 (and so no network timeout) for it. Only a validation that
     * blocks no writer ({@code VALIDATE CONSTRAINT}: SHARE UPDATE EXCLUSIVE) may carry it; a step that blocks writers keeps its tight bound.
     */
    public static final int NO_STATEMENT_BOUND_MS = Integer.MAX_VALUE;

    /** The lock wait of a move with no statement bound (what the other moves get as the ceiling of {@code min(5 s, bound / 2)}). */
    public static final int UNBOUNDED_LOCK_TIMEOUT_MS = 5_000;

    /** True when the completion of this move has no statement bound, only {@link #UNBOUNDED_LOCK_TIMEOUT_MS}. */
    public boolean unbounded() {
        return statementTimeoutMs == NO_STATEMENT_BOUND_MS;
    }

    public boolean paused() {
        return pausedAt != null;
    }

    /** {@code schema.table}, for a message. */
    public String drivingTableLabel() {
        return drivingSchema + "." + drivingTable;
    }
}
