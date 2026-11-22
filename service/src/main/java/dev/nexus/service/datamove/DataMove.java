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
        String lastTransient) {

    public boolean paused() {
        return pausedAt != null;
    }

    /** {@code schema.table}, for a message. */
    public String drivingTableLabel() {
        return drivingSchema + "." + drivingTable;
    }
}
