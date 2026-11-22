// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), item (B): where {@code DataMoveWorker} records a move complete, it tells the completion check
 * ({@link DataMoveWorker#onMoveCompleted}), which reads again at once ({@link CompletionBootWork#onMoveCompleted}). A real move
 * through the production finisher; no sleeps and no clock in the assertions.
 */
class CompletionRefreshOnMoveCompletionIntegrationTest extends DataMoveIntegrationBase {

    /** A move over tenant_plain that the production finisher can verify and complete. */
    private void registerMove(String label) {
        register(label, "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 60_000, null);
    }

    private DataMoveWorker productionWorker() {
        return worker(installed(), 0, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()), new VerifyingFinisher());
    }

    @Test
    void theWorkerTellsTheListenerOnceWhenAMoveCompletes_andNotWhenNothingRuns() throws Exception {
        registerMove("m-refresh");
        var completions = new AtomicInteger();
        var worker = productionWorker();
        worker.onMoveCompleted(completions::incrementAndGet);

        assertThat(worker.runCycle()).as("the one registered move is worked").isEqualTo(1);

        assertThat(completedAt("m-refresh")).as("non-vacuity: the move completed").isNotNull();
        assertThat(completions.get()).as("the listener ran once, for the completion").isEqualTo(1);
        assertThat(worker.runCycle()).as("nothing is open now").isZero();
        assertThat(completions.get()).as("and no completion, no call").isEqualTo(1);
    }

    @Test
    void aListenerThatThrowsDoesNotFailTheMove() throws Exception {
        registerMove("m-refresh-throws");
        var worker = productionWorker();
        worker.onMoveCompleted(() -> {
            throw new IllegalStateException("the listener is broken");
        });

        assertThat(worker.runCycle()).isEqualTo(1);

        assertThat(completedAt("m-refresh-throws")).as("the completion stands").isNotNull();
    }

    @Test
    void aMoveLeftOpenIsNotACompletion() throws Exception {
        registerMove("m-refresh-open");
        var completions = new AtomicInteger();
        var worker = worker(installed(), 0);   // RUNS_ONLY: the runs finish, no completed_at is recorded
        worker.onMoveCompleted(completions::incrementAndGet);

        assertThat(worker.runCycle()).isEqualTo(1);

        assertThat(completedAt("m-refresh-open")).as("non-vacuity: still open").isNull();
        assertThat(completions.get()).isZero();
    }
}
