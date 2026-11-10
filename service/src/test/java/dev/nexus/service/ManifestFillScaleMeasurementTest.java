// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ManifestFill;
import dev.nexus.service.db.ManifestFillableCount;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2, fix round r1: what the manifest fill job costs at the size of one big store, in the two shapes
 * that decide it. A hand-run measurement; no wall clock is asserted. It prints {@code F3YQD ...} lines (wall time of the
 * whole job, batches, rows scanned and filled, and {@code EXPLAIN (ANALYZE, BUFFERS)} of one window, one count and one
 * UPDATE) and the numbers go in the bead.
 *
 * <ul>
 *   <li><b>dense</b>: every manifest row is admitted by both prefilters and fillable (a legacy store nobody has
 *       filled): every window is full after {@code batch_rows} rows.</li>
 *   <li><b>sparse</b>: one row in a hundred is pending and fillable; of the rest, labels are written and spans full,
 *       except one row in eight whose chunk records no span (prose), which stays NULL for good. The labels window must
 *       scan about a hundred rows per admitted one, and the spans walk crosses every prose row on every start.</li>
 * </ul>
 *
 * <p>Opt-in: tag {@code measurement}, which the default run leaves out. Run:
 * {@code scripts/mvnw-leased.sh -B test -Dtest.excluded.groups= -Dgroups=measurement -Dtest=ManifestFillScaleMeasurementTest
 * -Dnexus.test.fork.timeout=0}; {@code -Dnexus.f3yqd.n=} sets the manifest rows (default 200,000). Non-vacuity: the
 * seeded counts and each job's {@code filled} are asserted.
 */
@Tag("measurement")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillScaleMeasurementTest extends ManifestFillTestBase {

    private static final int N = Integer.getInteger("nexus.f3yqd.n", 200_000);
    private static final int BATCH_ROWS = 1_000;
    private static final int POSITIONS_PER_DOC = 100;
    private static final int SEED_BATCH = 20_000;

    private enum Shape { DENSE, SPARSE }

    /** Pending (fillable for both kinds) rows of a shape: all of them, or one in a hundred. */
    private static int pending(Shape shape) {
        return shape == Shape.DENSE ? N : (N + 99) / 100;
    }

    private void seed(Shape shape) throws Exception {
        long t0 = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLL);
            String model = PgContainerHelper.collectionModel(seed, TENANT, COLL);

            int docs = (N - 1) / POSITIONS_PER_DOC + 1;
            var docSeries = DSL.generateSeries(0, docs - 1).as("d", "n");
            Field<Integer> n = docSeries.field("n", Integer.class);
            seed.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CONTENT_TYPE,
                    CATALOG_DOCUMENTS.INDEXED_AT, CATALOG_DOCUMENTS.METADATA)
                .select(seed.select(DSL.inline(TENANT), DSL.concat(DSL.inline("9."), DSL.cast(n, SQLDataType.VARCHAR), DSL.inline(".1")),
                        DSL.concat(DSL.inline("doc "), DSL.cast(n, SQLDataType.VARCHAR)), DSL.inline(COLL),
                        DSL.inline("prose"), DSL.val(OffsetDateTime.parse(DOC_AT), CATALOG_DOCUMENTS.INDEXED_AT),
                        DSL.val(JSONB.valueOf("{}"), CATALOG_DOCUMENTS.METADATA))
                    .from(docSeries))
                .execute();

            for (int lo = 1; lo <= N; lo += SEED_BATCH) {
                int hi = Math.min(N, lo + SEED_BATCH - 1);
                var series = DSL.generateSeries(lo, hi).as("s", "i");
                Field<Integer> i = series.field("i", Integer.class);
                Field<byte[]> chash = DSL.function("decode", SQLDataType.BLOB,
                    DSL.concat(DSL.md5(DSL.concat(DSL.inline("f3-a-"), DSL.cast(i, SQLDataType.VARCHAR))),
                        DSL.md5(DSL.concat(DSL.inline("f3-b-"), DSL.cast(i, SQLDataType.VARCHAR)))),
                    DSL.inline("hex"));
                // pending: every row (dense), or one in a hundred (sparse). prose: sparse rows that record no span.
                var pend = shape == Shape.DENSE ? DSL.trueCondition() : i.mod(100).eq(0);
                var prose = shape == Shape.DENSE ? DSL.falseCondition() : i.mod(8).eq(1);
                Field<Integer> ls = i.mod(1000).plus(1);
                Field<String> spanKeys = DSL.concat(DSL.inline(",\"line_start\":"), DSL.cast(ls, SQLDataType.VARCHAR),
                    DSL.inline(",\"line_end\":"), DSL.cast(ls.plus(4), SQLDataType.VARCHAR),
                    DSL.inline(",\"chunk_start_char\":0,\"chunk_end_char\":100"));
                Field<String> meta = DSL.concat(DSL.inline("{\"indexed_at\":\"" + OK_AT + "\",\"section_title\":\"S\""),
                    DSL.when(prose, DSL.inline("")).otherwise(spanKeys), DSL.inline("}"));
                seed.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHASH,
                        CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_768, CHUNKS.METADATA)
                    .select(seed.select(DSL.val(TENANT, CHUNKS.TENANT_ID), DSL.val(COLL, CHUNKS.COLLECTION),
                            DSL.val(model, CHUNKS.EMBEDDING_MODEL), chash, DSL.inline("stub"),
                            DSL.val(VectorSeeds.vec(768, 0.1f), CHUNKS.EMBEDDING_768),
                            DSL.cast(meta, SQLDataType.JSONB))
                        .from(series))
                    .execute();
                // manifest: pending rows are legacy (labels unwritten, spans NULL); the others are filled, but prose rows' spans
                Field<Integer> nullSpan = DSL.inline((Integer) null);
                var legacy = pend;
                seed.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                        CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, CATALOG_DOCUMENT_CHUNKS.LINE_START,
                        CATALOG_DOCUMENT_CHUNKS.LINE_END, CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END)
                    .select(seed.select(DSL.val(TENANT, CATALOG_DOCUMENT_CHUNKS.TENANT_ID),
                            DSL.concat(DSL.inline("9."), DSL.cast(i.minus(1).div(POSITIONS_PER_DOC), SQLDataType.VARCHAR),
                                DSL.inline(".1")),
                            i.minus(1).mod(POSITIONS_PER_DOC), i.minus(1).mod(POSITIONS_PER_DOC), chash,
                            DSL.val(COLL, CATALOG_DOCUMENT_CHUNKS.COLLECTION),
                            DSL.val(model, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL),
                            DSL.when(legacy, DSL.inline(false)).otherwise(DSL.inline(true)),
                            DSL.when(legacy.or(prose), nullSpan).otherwise(ls),
                            DSL.when(legacy.or(prose), nullSpan).otherwise(ls.plus(4)),
                            DSL.when(legacy.or(prose), nullSpan).otherwise(DSL.inline(0)),
                            DSL.when(legacy.or(prose), nullSpan).otherwise(DSL.inline(100)))
                        .from(series))
                    .execute();
                System.out.println("F3YQD seeded " + shape + " through row " + hi + " of " + N);
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CHUNKS);
            assertThat(seed.fetchCount(CATALOG_DOCUMENT_CHUNKS)).as("manifest rows").isEqualTo(N);
            assertThat(seed.fetchCount(CHUNKS)).as("chunk rows").isEqualTo(N);
        }
        System.out.println("F3YQD seeded " + shape + " " + N + " rows in " + (System.nanoTime() - t0) / 1_000_000_000L + " s");
    }

    /** The plans of one window, its count and its UPDATE, over the untouched fixture (the UPDATE is rolled back). */
    private void explainOneWindow(Shape shape, ManifestFillableCount.Kind kind) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ManifestFill.pinPlanner(ctx);   // the batch's own planner settings; the job below runs under them too
            ManifestFill.pinWindow(ctx, true);
            var window = ManifestFill.windowQuery(ctx, TENANT, kind, null, BATCH_ROWS);
            System.out.println("F3YQD " + shape + " " + kind.wire() + " WINDOW plan (analyze, buffers):\n"
                + PgContainerHelper.explainPlanWith(ctx, window, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS));
            var rows = window.fetch();
            ManifestFill.pinWindow(ctx, false);
            var end = rows.get(rows.size() - 1);
            var last = new ManifestFill.Cursor(end.value1(), end.value2());
            var count = ManifestFill.fillableByCollection(ctx, TENANT, kind, null, last);
            System.out.println("F3YQD " + shape + " " + kind.wire() + " COUNT plan (analyze, buffers):\n"
                + PgContainerHelper.explainPlanWith(ctx, count, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS));
            var update = ManifestFill.updateStatement(ctx, TENANT, kind, null, last, false);
            System.out.println("F3YQD " + shape + " " + kind.wire() + " UPDATE plan (analyze, buffers, rolled back):\n"
                + PgContainerHelper.explainPlanWith(ctx, update, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS));
            su.rollback();
        }
    }

    private Map<String, Object> runToEnd(String kind) throws Exception {
        String job = (String) start(kind, false, BATCH_ROWS).get("job_id");
        long deadline = System.nanoTime() + TimeUnit.MINUTES.toNanos(40);
        while (true) {
            var status = poll(job);
            if (!"running".equals(status.get("state"))) return status;
            assertThat(System.nanoTime()).as("the fill ends: " + status).isLessThan(deadline);
            Thread.sleep(500);
        }
    }

    private void measure(Shape shape) throws Exception {
        seed(shape);
        for (var kind : ManifestFillableCount.Kind.values()) explainOneWindow(shape, kind);
        for (var kind : ManifestFillableCount.Kind.values()) {
            long t0 = System.nanoTime();
            var status = runToEnd(kind.wire());
            long wallMs = (System.nanoTime() - t0) / 1_000_000L;
            long scanned = ((Number) status.get("scanned_rows")).longValue();
            long batches = (scanned + BATCH_ROWS - 1) / BATCH_ROWS + (scanned % BATCH_ROWS == 0 ? 1 : 0);
            System.out.println("F3YQD RESULT shape=" + shape + " kind=" + kind.wire() + " rows=" + N
                + " batch_rows=" + BATCH_ROWS + " wall_ms=" + wallMs + " job_elapsed_ms=" + status.get("elapsed_ms")
                + " batches=" + batches + " scanned=" + scanned + " estimate=" + status.get("manifest_rows_estimate")
                + " picked=" + status.get("done_rows") + " filled=" + status.get("filled") + " state=" + status.get("state"));
            assertThat(status).containsEntry("state", "done");
            long expectFilled = kind == ManifestFillableCount.Kind.LABELS ? pending(shape) : pending(shape);
            assertThat(((Number) status.get("filled")).longValue()).as("every pending row is filled").isEqualTo(expectFilled);
        }
        // converged: a second pass over the same store (the case every later start walks)
        for (var kind : ManifestFillableCount.Kind.values()) {
            long t0 = System.nanoTime();
            var status = runToEnd(kind.wire());
            System.out.println("F3YQD RESULT converged shape=" + shape + " kind=" + kind.wire() + " wall_ms="
                + (System.nanoTime() - t0) / 1_000_000L + " scanned=" + status.get("scanned_rows")
                + " filled=" + status.get("filled") + " state=" + status.get("state"));
            assertThat(status).containsEntry("state", "done").containsEntry("filled", 0);
        }
    }

    @Test
    void denseAdmittedSet() throws Exception {
        measure(Shape.DENSE);
    }

    @Test
    void sparseAndProseHeavySet() throws Exception {
        measure(Shape.SPARSE);
    }
}
