// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2, fix round r1: the engine's shutdown ({@code EngineJobs.shutdown}, a {@code shutdownNow}) interrupts
 * a running fill, and the job stops at its next batch boundary instead of running on. Its own class because it shuts the
 * fill registry down, which no other test of the service's fill jobs can run after.
 *
 * <p>Guard-kill: the interrupt check removed from the job loop makes {@code anInterruptedJob_stopsAtTheNextBatchBoundary}
 * run to the end ({@code done}, three rows filled).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillShutdownTest extends ManifestFillTestBase {

    @Test
    void anInterruptedJob_stopsAtTheNextBatchBoundary() throws Exception {
        String d = docOfRows(3);
        try (var writer = new RowLock(d, 1)) {
            String job = (String) start("labels", false, 1).get("job_id");
            // batch 1 (position 0) commits; batch 2 (position 1) waits for the writer
            awaitBlockedUpdate();
            // the engine shuts down: the worker is interrupted while its batch is in flight
            service.manifestFillJobsForTests().shutdown();
            writer.rollback();

            var status = awaitEnd(job);

            // batch 2 was in flight and commits (the driver does not abandon a statement for an interrupt); the loop then
            // sees the flag and stops before batch 3
            assertThat(status).containsEntry("state", "failed").containsEntry("reason", "job_stopped")
                .containsEntry("filled", 2);
            assertThat((String) status.get("error")).contains("interrupted");
        }
        assertThat(row(d, 1).written()).isTrue();
        assertThat(row(d, 2)).as("the batch it never ran").isEqualTo(LEGACY);
    }
}
