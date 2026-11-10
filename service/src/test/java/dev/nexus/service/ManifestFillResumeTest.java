// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.ManifestFillJobs;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2, fix round r1: the resume cursor ({@code after}), the progress fields ({@code scanned_rows},
 * {@code manifest_rows_estimate}) and the pause between batches ({@link ManifestFillJobs#PAUSE_PROPERTY}).
 *
 * <p>Guard-kill map (each mutation was applied by hand, and the named test went red):
 * <ul>
 *   <li>the job ignores {@code after} (walks from the start): {@code aDryRunAfterACursor_walksOnlyTheRowsPastIt} and
 *       {@code aStoppedJob_restartedWithItsCursor_...};</li>
 *   <li>the pause removed: {@code theJobPausesBetweenBatches};</li>
 *   <li>{@code scanned_rows} not summed, or the estimate counting rows the prefilter does not admit:
 *       {@code progressIsScannedOverTheRowsThePrefilterAdmits}.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillResumeTest extends ManifestFillTestBase {

    /** Waits (30 s at most) until the job has scanned at least {@code rows} manifest rows. */
    private Map<String, Object> awaitScanned(String job, int rows) throws Exception {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(30);
        while (true) {
            var status = poll(job);
            if (((Number) status.get("scanned_rows")).intValue() >= rows) return status;
            assertThat(System.nanoTime()).as("the job scans " + rows + " rows: " + status).isLessThan(deadline);
            Thread.sleep(10);
        }
    }

    /**
     * A job of {@code n} rows in batches of one that has committed its first batch and is pausing before the second:
     * stopped there (the engine's stop), and returned with the cursor its poll showed.
     */
    private Map<String, Object> stopAfterTheFirstBatch() throws Exception {
        var shown = new java.util.concurrent.atomic.AtomicReference<Map<String, Object>>();
        withProperty(ManifestFillJobs.PAUSE_PROPERTY, "2000", () -> {
            String job = (String) start("labels", false, 1).get("job_id");
            awaitScanned(job, 1);
            service.manifestFillJobsForTests().find(job, TENANT).stopFor("operator");
            var stopped = awaitEnd(job);
            assertThat(stopped).containsEntry("state", "failed").containsEntry("reason", "job_stopped")
                .containsEntry("filled", 1).containsEntry("safe_to_stop", true);
            assertThat((String) stopped.get("error")).contains("a new start re-reads from the beginning, or from the cursor passed");
            assertThat(stopped.get("on_stop")).isEqualTo(ManifestFillJobs.ON_STOP);
            assertThat((String) stopped.get("on_stop")).contains("committed batches stay written")
                .contains("a new start re-reads from the beginning, or from the cursor passed");
            shown.set(stopped);
        });
        return shown.get();
    }

    @Test
    void aStoppedJob_restartedWithItsCursor_fillsTheRest() throws Exception {
        String d = docOfRows(4);
        var stopped = stopAfterTheFirstBatch();
        Object cursor = stopped.get("cursor");
        assertThat(cursor).isEqualTo(Map.of("doc_id", d, "position", 0));

        var resumed = awaitEnd((String) startAfter("labels", false, 1, cursor).get("job_id"));

        assertThat(resumed).containsEntry("state", "done").containsEntry("filled", 3).containsEntry("done_rows", 3)
            .containsEntry("scanned_rows", 3);
        assertThat(resumed.get("cursor")).isEqualTo(Map.of("doc_id", d, "position", 3));
        for (int i = 0; i < 4; i++) assertThat(row(d, i).written()).as("position " + i).isTrue();
        assertThat(fillable("labels")).isZero();
    }

    @Test
    void aStoppedJob_restartedWithoutACursor_alsoCompletes() throws Exception {
        String d = docOfRows(4);
        stopAfterTheFirstBatch();

        var restarted = runFill("labels", false, 1);

        assertThat(restarted).containsEntry("state", "done").containsEntry("filled", 3).containsEntry("done_rows", 3);
        for (int i = 0; i < 4; i++) assertThat(row(d, i).written()).as("position " + i).isTrue();
        assertThat(fillable("labels")).isZero();
    }

    /**
     * A dry run writes nothing, so every row stays admitted: only {@code after} can keep the walk off the rows before the
     * cursor. The cursor is the job's start (its poll's cursor before the first batch), its progress counts only the
     * rows past it, and so does the estimate.
     */
    @Test
    void aDryRunAfterACursor_walksOnlyTheRowsPastIt() throws Exception {
        String d = docOfRows(4);

        var whole = runFill("labels", true, 1000);
        var resumed = awaitEnd((String) startAfter("labels", true, 1000, Map.of("doc_id", d, "position", 1)).get("job_id"));

        assertThat(whole).containsEntry("done_rows", 4).containsEntry("scanned_rows", 4)
            .containsEntry("manifest_rows_estimate", 4);
        assertThat(resumed).containsEntry("state", "done").containsEntry("done_rows", 2).containsEntry("scanned_rows", 2)
            .containsEntry("manifest_rows_estimate", 2).containsEntry("filled", 0);
        assertThat(resumed.get("cursor")).isEqualTo(Map.of("doc_id", d, "position", 3));
    }

    @Test
    void aCursorPastTheEnd_isAnEmptyJobThatKeepsTheCursor() throws Exception {
        String d = docOfRows(2);

        var status = awaitEnd((String) startAfter("labels", false, 1000, Map.of("doc_id", d, "position", 1)).get("job_id"));

        assertThat(status).containsEntry("state", "done").containsEntry("done_rows", 0).containsEntry("filled", 0)
            .containsEntry("scanned_rows", 0).containsEntry("manifest_rows_estimate", 0);
        assertThat(status.get("cursor")).as("nothing scanned, so the cursor is where it began")
            .isEqualTo(Map.of("doc_id", d, "position", 1));
        assertThat(row(d, 0)).isEqualTo(LEGACY);
    }

    @Test
    void aBadAfterIsA400() throws Exception {
        String[] bad = {
            "\"after\":\"1.2.3\"", "\"after\":[]", "\"after\":{}", "\"after\":{\"doc_id\":\"1.1.1\"}",
            "\"after\":{\"position\":0}", "\"after\":{\"doc_id\":\"\",\"position\":0}",
            "\"after\":{\"doc_id\":7,\"position\":0}", "\"after\":{\"doc_id\":\"1.1.1\",\"position\":-1}",
            "\"after\":{\"doc_id\":\"1.1.1\",\"position\":\"0\"}", "\"after\":{\"doc_id\":\"1.1.1\",\"position\":0.5}",
            "\"after\":{\"doc_id\":\"1.1.1\",\"position\":0,\"extra\":1}"};
        for (String after : bad) {
            var resp = post(TOKEN, TENANT, "/v1/catalog/manifest/fill", "{\"kind\":\"labels\"," + after + "}");
            assertThat(resp.statusCode()).as(after + ": " + resp.body()).isEqualTo(400);
            assertThat(resp.body()).contains("after must be");
        }
        var ok = post(TOKEN, TENANT, "/v1/catalog/manifest/fill", "{\"kind\":\"labels\",\"after\":null}");
        assertThat(ok.statusCode()).as("a null after is no after: " + ok.body()).isEqualTo(202);
    }

    // ── progress ────────────────────────────────────────────────────────────

    /**
     * {@code progress = scanned_rows / manifest_rows_estimate}: a window holds only the rows the kind's prefilter admits,
     * so the estimate is the count of exactly those rows. Here one row of the tenant is already labelled (not admitted by
     * the labels prefilter) and the rest are admitted, whether or not they are fillable: the fraction ends at 1.
     */
    @Test
    void progressIsScannedOverTheRowsThePrefilterAdmits() throws Exception {
        String d = docOfRows(5);
        // an edge row (admitted, never filled) and an already labelled row (not admitted)
        oneRow(0x61, "{\"section_title\":5,\"indexed_at\":\"" + OK_AT + "\"}");
        String written = oneRow(0x62, "{\"section_title\":\"x\",\"indexed_at\":\"" + OK_AT + "\"}");
        var writer = post(TOKEN, TENANT, "/v1/catalog/manifest/write", mapper.writeValueAsString(Map.of(
            "doc_id", written, "collection", COLL,
            "rows", java.util.List.of(Map.of("position", 0, "chash", h(0x62), "section_title", "Mine", "section_type", "")))));
        assertThat(writer.statusCode()).as(writer.body()).isEqualTo(200);
        restamp(TENANT, written);

        var status = runFill("labels", false, 2);

        assertThat(status).containsEntry("state", "done").containsEntry("filled", 5).containsEntry("done_rows", 5);
        assertThat(status).as("6 rows admitted (5 fillable, 1 edge), the labelled one is not")
            .containsEntry("manifest_rows_estimate", 6).containsEntry("scanned_rows", 6);
        assertThat(row(d, 0).written()).isTrue();
    }

    @Test
    void anEmptyManifest_scansNothing() throws Exception {
        var status = runFill("spans", false, 1000);

        assertThat(status).containsEntry("state", "done").containsEntry("scanned_rows", 0)
            .containsEntry("manifest_rows_estimate", 0).containsEntry("done_rows", 0);
    }

    // ── pacing ──────────────────────────────────────────────────────────────

    @Test
    void theJobPausesBetweenBatches() throws Exception {
        docOfRows(3);
        withProperty(ManifestFillJobs.PAUSE_PROPERTY, "400", () -> {
            var status = runFill("labels", false, 1);

            // batches of one over three rows: a pause after each of the first three, so at least two full pauses
            assertThat(status).containsEntry("state", "done").containsEntry("filled", 3);
            assertThat(((Number) status.get("elapsed_ms")).longValue()).as("paused between batches")
                .isGreaterThanOrEqualTo(800L);
        });
        assertThat(fillable("labels")).isZero();
    }
}
