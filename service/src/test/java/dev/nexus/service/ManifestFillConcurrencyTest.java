// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ManifestFill;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2: the fill job against concurrent writers and its own lifecycle. A second connection holds a
 * manifest row's lock in an open transaction (a writer that has not committed): the fill's pick, a plain read, goes
 * past it and picks the row, and the fill's UPDATE of that row WAITS. When the writer then commits its change, the
 * UPDATE re-evaluates its own WHERE against the row's newest version, so a row a concurrent writer changed after the
 * pick is skipped and never overwritten. That re-check is the target-row guard of the UPDATE (labels:
 * {@code labels_written = false}; spans: the NULL-column and no-disagree test over the recorded values), and these
 * tests are what kill the mutation that removes it.
 *
 * <p>Guard-kill map (each mutation was applied by hand, and the named test went red, before this was committed):
 * <ul>
 *   <li>the UPDATE's {@code labels_written = false} re-check removed: {@code aLabelWrittenAfterThePick_isNotOverwritten};</li>
 *   <li>the UPDATE's spans re-check removed: {@code aSpanWrittenAfterThePick_stopsTheWholeRow};</li>
 *   <li>the UPDATE's {@code chash} re-check removed ({@code sameRow}): {@code aRowRePointedAfterThePick_...} for each
 *       kind (the row would take the labels or spans of the chunk it no longer names);</li>
 *   <li>the 55P03 fallback removed (skip mode never runs): {@code aRowHeldPastTheLockTimeout_...} for each kind (the job
 *       fails {@code transient_db_error}); skip mode without {@code SKIP LOCKED}: the same tests (the rerun waits on the
 *       row, so the job outlasts the test's bound);</li>
 *   <li>the stop check before each batch removed: {@code aStoppedJob_keepsEveryCommittedBatch_andANewJobResumes};</li>
 *   <li>the shared sweep gate not taken before the UPDATE: {@code aBatchWaitsForTheSweepGateOfItsCollection};</li>
 *   <li>the fingerprint dropped from the start (a different {@code dry_run} joins): {@code aSecondStart_joinsTheRunningJob...};</li>
 *   <li>the kind put back in the scope (a spans start runs beside a labels job): the same test.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillConcurrencyTest extends ManifestFillTestBase {

    /** The race tests hold a row for as long as they need to; the lock-timeout tests set their own, short one. */
    @BeforeEach
    void aLongLockTimeout() {
        System.setProperty(ManifestFill.LOCK_TIMEOUT_PROPERTY, "60000");
    }

    @AfterEach
    void clearTheLockTimeout() {
        System.clearProperty(ManifestFill.LOCK_TIMEOUT_PROPERTY);
    }

    /** One document with {@code n} fillable label rows (and a recorded line start each), positions 0..n-1. */
    private String rows(int n) throws Exception {
        String d = doc("concurrent doc");
        String[] hashes = new String[n];
        for (int i = 0; i < n; i++) {
            hashes[i] = h(0x50 + i);
            chunk(TENANT, hashes[i], "{\"section_title\":\"S" + i + "\",\"line_start\":" + (i + 1) + ",\"chunk_start_char\":10,\"indexed_at\":\"" + OK_AT + "\"}");
        }
        manifest(d, hashes);
        return d;
    }

    @Test
    void aLabelWrittenAfterThePick_isNotOverwritten() throws Exception {
        String d = rows(3);
        try (var writer = new RowLock(d, 1)) {
            String job = (String) start("labels", false, 1000).get("job_id");
            awaitBlockedUpdate();
            writer.labelWritten("concurrent").commit();

            var status = awaitEnd(job);

            assertThat(status).containsEntry("state", "done").containsEntry("done_rows", 3).containsEntry("filled", 2);
            assertThat(counts(status)).containsEntry("fillable", 3).containsEntry("skipped_changed", 1);
        }
        assertThat(row(d, 0)).isEqualTo(new Row(true, "S0", "", null, null, null, null, null));
        assertThat(row(d, 1)).as("the concurrent writer's row, as it left it")
            .isEqualTo(new Row(true, "concurrent", null, null, null, null, null, null));
        assertThat(row(d, 2)).isEqualTo(new Row(true, "S2", "", null, null, null, null, null));
    }

    @Test
    void aSpanWrittenAfterThePick_stopsTheWholeRow() throws Exception {
        String d = rows(2);
        try (var writer = new RowLock(d, 1)) {
            String job = (String) start("spans", false, 1000).get("job_id");
            awaitBlockedUpdate();
            // 99 disagrees with the chunk's recorded line start (2): the chunk row no longer describes this position
            writer.lineStart(99).commit();

            var status = awaitEnd(job);

            assertThat(status).containsEntry("state", "done").containsEntry("filled", 1);
            assertThat(counts(status)).containsEntry("fillable", 2).containsEntry("skipped_changed", 1);
        }
        assertThat(row(d, 0)).isEqualTo(new Row(false, null, null, null, 1, null, 10, null));
        assertThat(row(d, 1)).as("not even the column the chunk records and the row lacks is written")
            .isEqualTo(new Row(false, null, null, null, 99, null, null, null));
    }

    /**
     * A re-index re-points the row's position at another chunk between the UPDATE's read and its lock. The row's labels
     * and spans must not be taken from the chunk it no longer names: the UPDATE re-checks {@code chash} on the row it
     * locks. (The re-point is the only change; {@code labels_written} and every span column are as the pick saw them.)
     */
    private void assertARePointedRowIsNotFilledFromItsOldChunk(String kind) throws Exception {
        String d = rows(3);
        chunk(TENANT, h(0x70), "{\"section_title\":\"Other\",\"line_start\":77,\"indexed_at\":\"" + OK_AT + "\"}");
        try (var writer = new RowLock(d, 1)) {
            String job = (String) start(kind, false, 1000).get("job_id");
            awaitBlockedUpdate();
            writer.chash(h(0x70)).commit();

            var status = awaitEnd(job);

            assertThat(status).containsEntry("state", "done").containsEntry("filled", 2);
            assertThat(counts(status)).containsEntry("fillable", 3).containsEntry("skipped_changed", 1);
        }
        assertThat(row(d, 1)).as("the re-pointed row takes nothing from the chunk it named at the pick").isEqualTo(LEGACY);
        assertThat(row(d, 0).written() || row(d, 0).lineStart() != null).as("its neighbours were filled").isTrue();
        assertThat(row(d, 2).written() || row(d, 2).lineStart() != null).as("its neighbours were filled").isTrue();
    }

    @Test
    void aRowRePointedAfterThePick_isNotFilledFromItsOldChunk_labels() throws Exception {
        assertARePointedRowIsNotFilledFromItsOldChunk("labels");
    }

    @Test
    void aRowRePointedAfterThePick_isNotFilledFromItsOldChunk_spans() throws Exception {
        assertARePointedRowIsNotFilledFromItsOldChunk("spans");
    }

    /**
     * A writer holds a row of the batch and never lets go within the lock timeout (a long reconcile commit). The job
     * does not wait the batch's statement timeout and does not fail: the batch runs again skipping the held row, the
     * job finishes, and the held row is counted as skipped. A later start, with the lock gone, fills it.
     */
    private void assertAHeldRowIsSkippedAndTheJobFinishes(String kind) throws Exception {
        String d = rows(3);
        withProperty(ManifestFill.LOCK_TIMEOUT_PROPERTY, "300", () -> {
            try (var writer = new RowLock(d, 1)) {
                long t0 = System.nanoTime();

                var status = runFill(kind, false, 1000);

                assertThat(java.util.concurrent.TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - t0))
                    .as("nowhere near the 120 s batch timeout").isLessThan(30);
                assertThat(status).containsEntry("state", "done").containsEntry("done_rows", 3).containsEntry("filled", 2);
                assertThat(counts(status)).containsEntry("fillable", 3).containsEntry("skipped_changed", 1);
                assertThat(row(d, 1)).as("the held row was not written, and not waited for").isEqualTo(LEGACY);
            }
        });
        var again = runFill(kind, false, 1000);
        assertThat(again).as("the next start takes the row the writer held").containsEntry("state", "done")
            .containsEntry("done_rows", 1).containsEntry("filled", 1);
        assertThat(fillable(kind)).isZero();
    }

    @Test
    void aRowHeldPastTheLockTimeout_isSkippedAndTheJobFinishes_labels() throws Exception {
        assertAHeldRowIsSkippedAndTheJobFinishes("labels");
    }

    @Test
    void aRowHeldPastTheLockTimeout_isSkippedAndTheJobFinishes_spans() throws Exception {
        assertAHeldRowIsSkippedAndTheJobFinishes("spans");
    }

    @Test
    void aBatchWaitsForTheSweepGateOfItsCollection() throws Exception {
        String d = rows(2);
        try (var sweep = new SweepGateHold(TENANT, COLL)) {
            String job = (String) start("labels", false, 1000).get("job_id");
            awaitAdvisoryLockWait();
            assertThat(poll(job)).as("the batch is waiting on the retire-orphans sweep, as every manifest writer does")
                .containsEntry("state", "running").containsEntry("filled", 0);
            assertThat(row(d, 0)).isEqualTo(LEGACY);
            sweep.release();

            assertThat(awaitEnd(job)).containsEntry("state", "done").containsEntry("filled", 2);
        }
    }

    @Test
    void aSecondStart_joinsTheRunningJob_andAnotherKindOrDryRunOrTenantDoesNot() throws Exception {
        String d = rows(2);
        try (var writer = new RowLock(d, 1)) {
            var first = start("labels", false, 1000);
            String id = (String) first.get("job_id");
            assertThat(first).containsEntry("created", true);
            awaitBlockedUpdate();

            // the same request, as a later box would send it: handed the running job (its batch size and cursor are ignored)
            var again = start("labels", false, 500);
            assertThat(again).containsEntry("job_id", id).containsEntry("created", false);
            var withCursor = startAfter("labels", false, 500, Map.of("doc_id", d, "position", 1));
            assertThat(withCursor).containsEntry("job_id", id).containsEntry("created", false);
            assertThat(poll(id)).containsEntry("state", "running").containsEntry("stage", "filling");

            // the other fingerprint: a dry run is not the running real run
            var conflict = post(TOKEN, TENANT, "/v1/catalog/manifest/fill", fillBody("labels", true, 1000));
            assertThat(conflict.statusCode()).as(conflict.body()).isEqualTo(409);
            assertThat(mapper.readValue(conflict.body(), MAP_T)).containsEntry("job_id", id);

            // one job per tenant: the other kind is a 409 naming the running job and what it is
            var spans = post(TOKEN, TENANT, "/v1/catalog/manifest/fill", fillBody("spans", false, 1000));
            assertThat(spans.statusCode()).as(spans.body()).isEqualTo(409);
            assertThat(mapper.readValue(spans.body(), MAP_T)).containsEntry("job_id", id)
                .containsEntry("kind", "labels").containsEntry("dry_run", false);

            // another tenant has its own scope: a job of its own, and it cannot see this one
            var other = post(TOKEN_B, TENANT_B, "/v1/catalog/manifest/fill", fillBody("labels", false, 1000));
            assertThat(other.statusCode()).as(other.body()).isEqualTo(202);
            Map<String, Object> otherBody = mapper.readValue(other.body(), MAP_T);
            assertThat(otherBody).containsEntry("created", true);
            assertThat(otherBody.get("job_id")).isNotEqualTo(id);
            assertThat(get(TOKEN_B, TENANT_B, "/v1/catalog/manifest/fill/" + id).statusCode()).isEqualTo(404);

            writer.rollback();
            assertThat(awaitEnd(id)).containsEntry("state", "done").containsEntry("filled", 2);
        }
        // the scope is free once the job has ended: the other kind starts
        assertThat(start("spans", false, 1000)).containsEntry("created", true);
    }

    @Test
    void aStoppedJob_keepsEveryCommittedBatch_andANewJobResumes() throws Exception {
        String d = rows(3);
        try (var writer = new RowLock(d, 1)) {
            String job = (String) start("labels", false, 1).get("job_id");
            // batch 1 (position 0) commits; batch 2 (position 1) waits for the writer
            awaitBlockedUpdate();
            // a batch's progress is published when it commits: batch 1 only
            assertThat(poll(job)).containsEntry("done_rows", 1).containsEntry("filled", 1);
            // the engine is told to stop (the tenant reset, the wall-clock cap, or a restart that ends the job)
            service.manifestFillJobsForTests().find(job, TENANT).stopFor("restart-test");
            writer.rollback();

            var stopped = awaitEnd(job);

            // batch 2 was in flight and commits; the job stops at the boundary after it, before batch 3
            assertThat(stopped).containsEntry("state", "failed").containsEntry("reason", "job_stopped")
                .containsEntry("http_status", 500).containsEntry("filled", 2).containsEntry("safe_to_stop", true);
            assertThat((String) stopped.get("error")).contains("restart-test").contains("a new start re-reads from the beginning, or from the cursor passed");
        }
        assertThat(row(d, 0).written()).isTrue();
        assertThat(row(d, 1).written()).isTrue();
        assertThat(row(d, 2)).as("the batch it never ran").isEqualTo(LEGACY);
        assertThat(fillable("labels")).isEqualTo(1);

        // a restart loses the job, not the fill: starting again takes what is left
        var resumed = runFill("labels", false, 1);

        assertThat(resumed).containsEntry("state", "done").containsEntry("done_rows", 1).containsEntry("filled", 1);
        assertThat(row(d, 2)).isEqualTo(new Row(true, "S2", "", null, null, null, null, null));
        assertThat(fillable("labels")).isZero();
    }
}
