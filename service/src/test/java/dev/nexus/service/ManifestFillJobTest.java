// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2: {@code POST /v1/catalog/manifest/fill} fills, as a background job, exactly the manifest rows
 * {@code GET /v1/catalog/manifest/fillable} counts as {@code fillable}, with the values of T2
 * {@code nexus/rung-detect-route-spec-2026-11-10} section 3, and nothing else. Each test builds the rows of one
 * situation beside rows the job must NOT write, reads the manifest back from PostgreSQL, and then asks the count: a
 * completed job leaves it at {@code fillable} 0 with {@code edge} unchanged.
 *
 * <p>Guard-kill map (each mutation was applied by hand, and the named test went red, before this was committed):
 * <ul>
 *   <li>the pick or the UPDATE uses the RELAXED candidates instead of the strict form: the edge rows are written, so
 *       {@code labelsFillExactlyTheStrictRows_withTheSpecValues} and {@code spansFill...} fail;</li>
 *   <li>labels: {@code page_number} 0 is not NULLIF'd, or a JSON-null title is not the empty label: the value asserts of
 *       the labels test fail;</li>
 *   <li>spans: a column is set to the recorded value instead of COALESCE'd with the row's own: the partially filled row
 *       of the spans test loses its {@code line_end};</li>
 *   <li>a dry run applies the UPDATE: {@code aDryRunWritesNothing_andItsCountsAreTheRealRunsCounts} fails;</li>
 *   <li>the keyset cursor is dropped or the range end is wrong: the batch tests of
 *       {@code ManifestFillConcurrencyTest} and {@code fillsAcrossBatches} fail.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillJobTest extends ManifestFillTestBase {

    // ── labels ──────────────────────────────────────────────────────────────

    /** The labels situation: three fillable rows, two edge rows, a row already written, a row with no label key. */
    private record LabelsFixture(String a, String b, String c, String edgeValue, String edgeStamp, String written,
                                 String noKeys) { }

    private LabelsFixture labelsFixture() throws Exception {
        String a = oneRow(0x01, "{\"section_title\":\"Intro\",\"section_type\":\"heading\",\"page_number\":3,\"indexed_at\":\"" + OK_AT + "\"}");
        // a JSON null title and page 0 are the empty label and no page
        String b = oneRow(0x02, "{\"section_title\":null,\"page_number\":0,\"indexed_at\":\"" + OK_AT + "\"}");
        // an absent title is the empty label; a type and a page
        String c = oneRow(0x03, "{\"section_type\":\"body\",\"page_number\":7,\"indexed_at\":\"" + OK_AT + "\"}");
        // edge: a title that is a JSON number, which SQL does not guess at
        String edgeValue = oneRow(0x04, "{\"section_title\":5,\"indexed_at\":\"" + OK_AT + "\"}");
        // edge: a date-only chunk stamp, which the strict form cannot read
        String edgeStamp = oneRow(0x05, "{\"section_title\":\"X\",\"indexed_at\":\"2026-10-01\"}");
        // already written: not a candidate
        String written = oneRow(0x06, "{\"section_title\":\"Fresh\",\"indexed_at\":\"" + OK_AT + "\"}");
        try (Connection su = pg.createConnection("")) {
            int n = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES).update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true).set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "Pre")
                .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(written)).execute();
            assertThat(n).isEqualTo(1);
        }
        // no label key: the labels rung has nothing to take from this chunk
        String noKeys = oneRow(0x07, "{\"indexed_at\":\"" + OK_AT + "\"}");
        return new LabelsFixture(a, b, c, edgeValue, edgeStamp, written, noKeys);
    }

    @Test
    void labelsFillExactlyTheStrictRows_withTheSpecValues() throws Exception {
        var f = labelsFixture();
        assertThat(fillable("labels", 1000)).containsEntry("fillable", 3).containsEntry("edge", 2).containsEntry("capped", false);

        var status = runFill("labels");

        assertThat(status).containsEntry("state", "done").containsEntry("stage", "done").containsEntry("kind", "labels")
            .containsEntry("dry_run", false).containsEntry("done_rows", 3).containsEntry("total_rows", 3)
            .containsEntry("filled", 3).containsEntry("safe_to_stop", true)
            .containsEntry("on_stop", "resumes from the last committed batch");
        assertThat(counts(status)).containsEntry("fillable", 3).containsEntry("edge", 2).containsEntry("skipped_changed", 0);
        assertThat(status.get("cursor")).isNotNull();
        // spec section 3: title/type are the chunk's string, or the empty label; page is the integer, NULL for 0
        assertThat(row(f.a(), 0)).isEqualTo(new Row(true, "Intro", "heading", 3, null, null, null, null));
        assertThat(row(f.b(), 0)).as("a JSON-null title and page 0").isEqualTo(new Row(true, "", "", null, null, null, null, null));
        assertThat(row(f.c(), 0)).as("an absent title").isEqualTo(new Row(true, "", "body", 7, null, null, null, null));
        // the rows it must not write
        assertThat(row(f.edgeValue(), 0)).as("a non-scalar label value is an edge row").isEqualTo(LEGACY);
        assertThat(row(f.edgeStamp(), 0)).as("an unreadable stamp is an edge row").isEqualTo(LEGACY);
        assertThat(row(f.written(), 0)).as("an already written row").isEqualTo(new Row(true, "Pre", null, null, null, null, null, null));
        assertThat(row(f.noKeys(), 0)).as("a chunk with no label key").isEqualTo(LEGACY);
        // R1 now answers pending 0 and the edge rows are still reported
        assertThat(fillable("labels", 1000)).containsEntry("fillable", 0).containsEntry("edge", 2).containsEntry("capped", false);
    }

    @Test
    void aSecondRunFillsNothing() throws Exception {
        labelsFixture();
        assertThat(runFill("labels")).containsEntry("filled", 3);

        var again = runFill("labels");

        assertThat(again).containsEntry("state", "done").containsEntry("done_rows", 0).containsEntry("filled", 0)
            .containsEntry("total_rows", 0);
        assertThat(counts(again)).containsEntry("fillable", 0).containsEntry("edge", 2);
    }

    // ── spans ───────────────────────────────────────────────────────────────

    @Test
    void spansFillExactlyTheStrictRows_withTheRecordedValues() throws Exception {
        // fresh row: every column NULL, the chunk records three of the four
        String fresh = oneRow(0x11, "{\"line_start\":5,\"chunk_start_char\":10,\"chunk_end_char\":20,\"indexed_at\":\"" + OK_AT + "\"}");
        // partly filled row: line_start agrees, line_end is the row's own and the chunk records none, char_end is NULL
        String partial = oneRow(0x12, "{\"line_start\":5,\"chunk_end_char\":30,\"indexed_at\":\"" + OK_AT + "\"}");
        spans(partial, 0, 5, 9, null, null);
        // a start of 0 beside a recorded end is recorded
        String zeroStart = oneRow(0x13, "{\"chunk_start_char\":0,\"chunk_end_char\":40,\"indexed_at\":\"" + OK_AT + "\"}");
        // disagrees with the row's own line_start: a chunk row that describes another position is not trusted for any column
        String disagree = oneRow(0x14, "{\"line_start\":5,\"chunk_end_char\":40,\"indexed_at\":\"" + OK_AT + "\"}");
        spans(disagree, 0, 9, null, null, null);
        // edge: a span the chunk records, a date-only stamp
        String edgeStamp = oneRow(0x15, "{\"line_start\":5,\"indexed_at\":\"2026-10-01\"}");
        // no span key at all
        String noKeys = oneRow(0x16, "{\"indexed_at\":\"" + OK_AT + "\"}");
        assertThat(fillable("spans", 1000)).containsEntry("fillable", 3).containsEntry("edge", 1);

        var status = runFill("spans");

        assertThat(status).containsEntry("state", "done").containsEntry("kind", "spans").containsEntry("done_rows", 3)
            .containsEntry("total_rows", 3).containsEntry("filled", 3);
        assertThat(counts(status)).containsEntry("fillable", 3).containsEntry("edge", 1).containsEntry("skipped_changed", 0);
        assertThat(row(fresh, 0)).isEqualTo(new Row(false, null, null, null, 5, null, 10, 20));
        assertThat(row(partial, 0)).as("COALESCE: the row's own line_end stays, the NULL char_end is filled")
            .isEqualTo(new Row(false, null, null, null, 5, 9, null, 30));
        assertThat(row(zeroStart, 0)).isEqualTo(new Row(false, null, null, null, null, null, 0, 40));
        assertThat(row(disagree, 0)).as("a disagreeing row is not written in any column")
            .isEqualTo(new Row(false, null, null, null, 9, null, null, null));
        assertThat(row(edgeStamp, 0)).isEqualTo(LEGACY);
        assertThat(row(noKeys, 0)).isEqualTo(LEGACY);
        assertThat(fillable("spans", 1000)).containsEntry("fillable", 0).containsEntry("edge", 1);
    }

    // ── dry run, isolation, and what a fill does not touch ─────────────────

    @Test
    void aDryRunWritesNothing_andItsCountsAreTheRealRunsCounts() throws Exception {
        var f = labelsFixture();
        List<Row> before = List.of(row(f.a(), 0), row(f.b(), 0), row(f.c(), 0), row(f.edgeValue(), 0), row(f.written(), 0));

        var dry = runFill("labels", true, 2);

        assertThat(dry).containsEntry("state", "done").containsEntry("dry_run", true).containsEntry("filled", 0)
            .containsEntry("done_rows", 3).containsEntry("total_rows", 3);
        assertThat(List.of(row(f.a(), 0), row(f.b(), 0), row(f.c(), 0), row(f.edgeValue(), 0), row(f.written(), 0)))
            .as("a dry run writes nothing").isEqualTo(before);
        assertThat(fillable("labels", 1000)).as("and the count still says pending").containsEntry("fillable", 3);

        var real = runFill("labels", false, 2);

        assertThat(real).containsEntry("filled", 3);
        assertThat(counts(real)).as("the dry run's counts are the real run's").isEqualTo(counts(dry));
        assertThat(real.get("done_rows")).isEqualTo(dry.get("done_rows"));
        assertThat(real.get("total_rows")).isEqualTo(dry.get("total_rows"));
        assertThat(real.get("cursor")).isEqualTo(dry.get("cursor"));
    }

    @Test
    void aFillWritesOnlyTheManifest_theDocumentsStampAndTheChunkRowsAreUnchanged() throws Exception {
        String d = doc("stamped doc");
        chunk(TENANT, h(0x21), both(OK_AT));
        chunk(TENANT, h(0x22), both(OK_AT));
        manifest(d, h(0x21), h(0x22));
        var before = untouched();
        var stampBefore = indexedAt(d);
        assertThat(stampBefore).isEqualTo(java.time.OffsetDateTime.parse(DOC_AT));

        assertThat(runFill("labels")).containsEntry("filled", 2);
        assertThat(runFill("spans")).containsEntry("filled", 2);

        assertThat(row(d, 0)).isEqualTo(new Row(true, "Intro", "", null, 5, null, null, null));
        assertThat(indexedAt(d)).as("no trigger restamps catalog_documents.indexed_at").isEqualTo(stampBefore);
        var after = untouched();
        assertThat(after.documents()).as("documents").isEqualTo(before.documents());
        assertThat(after.chunks()).as("chunk rows, their metadata and last_written_at").isEqualTo(before.chunks());
        assertThat(after.orphanRows()).as("the manifest UPDATE trigger stamps no chunk as orphaned").isEqualTo(before.orphanRows());
    }

    @Test
    void anotherTenantsRowsAreUntouched_andItsJobsAreNotVisible() throws Exception {
        var f = labelsFixture();
        String b = doc(TOKEN_B, TENANT_B, "tenant b doc", "", DOC_AT, "{}");
        chunk(TENANT_B, h(0x31), "{\"section_title\":\"B\",\"indexed_at\":\"" + OK_AT + "\"}");
        manifestAs(TOKEN_B, TENANT_B, b, h(0x31));
        var aJob = start("labels", false, 1000);

        var aStatus = awaitEnd((String) aJob.get("job_id"));

        assertThat(aStatus).containsEntry("filled", 3);
        assertThat(row(f.a(), 0).written()).isTrue();
        assertThat(row(b, 0)).as("tenant B's row is not tenant A's to fill").isEqualTo(LEGACY);
        assertThat(get(TOKEN_B, TENANT_B, "/v1/catalog/manifest/fill/" + aJob.get("job_id")).statusCode())
            .as("tenant A's job is a 404 to tenant B").isEqualTo(404);
        // tenant B's own job fills its own row and finds only it
        var bResp = post(TOKEN_B, TENANT_B, "/v1/catalog/manifest/fill", fillBody("labels", false, 1000));
        assertThat(bResp.statusCode()).as(bResp.body()).isEqualTo(202);
        var bStatus = awaitEnd(TOKEN_B, TENANT_B, (String) mapper.readValue(bResp.body(), MAP_T).get("job_id"));
        assertThat(bStatus).containsEntry("filled", 1).containsEntry("done_rows", 1);
        assertThat(row(b, 0)).isEqualTo(new Row(true, "B", "", null, null, null, null, null));
    }

    // ── batches and the cursor ──────────────────────────────────────────────

    /** One document of {@code n} fillable label rows, positions 0..n-1; returns its tumbler. */
    private String rowsOfOneDocument(int n) throws Exception {
        String d = doc("many rows");
        String[] hashes = new String[n];
        for (int i = 0; i < n; i++) {
            hashes[i] = h(0x40 + i);
            chunk(TENANT, hashes[i], "{\"section_title\":\"S" + i + "\",\"indexed_at\":\"" + OK_AT + "\"}");
        }
        manifest(d, hashes);
        return d;
    }

    @Test
    void fillsAcrossBatches_andTheCursorIsTheLastRowPicked() throws Exception {
        String d = rowsOfOneDocument(5);

        var status = runFill("labels", false, 2);

        assertThat(status).containsEntry("state", "done").containsEntry("done_rows", 5).containsEntry("filled", 5);
        assertThat(status.get("cursor")).isEqualTo(Map.of("doc_id", d, "position", 4));
        for (int i = 0; i < 5; i++) {
            assertThat(row(d, i)).as("position " + i).isEqualTo(new Row(true, "S" + i, "", null, null, null, null, null));
        }
        assertThat(fillable("labels")).isZero();
    }

    @Test
    void aTotalThatIsAnExactMultipleOfTheBatchSizeEndsOnAnEmptyBatch() throws Exception {
        String d = rowsOfOneDocument(4);

        var status = runFill("labels", false, 2);

        assertThat(status).containsEntry("state", "done").containsEntry("done_rows", 4).containsEntry("filled", 4);
        assertThat(status.get("cursor")).isEqualTo(Map.of("doc_id", d, "position", 3));
    }
}
