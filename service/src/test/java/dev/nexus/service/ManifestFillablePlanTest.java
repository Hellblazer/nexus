// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ManifestFillableCount;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd round r1: plan evidence for {@code GET /v1/catalog/manifest/fillable}. A tenant that has converged (the
 * steady state: every row the rungs could fill is filled, the rest never will be) makes the count scan every
 * candidate, so what matters is how cheaply the scan rejects them. The manifest-only prefilter (labels_written = false;
 * a NULL span column) is a top-level conjunct on the manifest row alone, so the planner can apply it at the manifest
 * scan before any join to chunks. This seeds a few thousand manifest rows, nearly all converged, and shows that.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillablePlanTest extends ManifestFillableTestBase {

    private static final int DOCS = 400;
    private static final int ROWS_PER_DOC = 8;
    /** Rows 0..fullRows-1 of a document: written labels, full spans. Row fullRows: prose (no keys; spans NULL forever). The rest: labels written, spans NULL, the chunk records none. */
    private static final int MIXED_FULL_ROWS = 6;
    /** A prose-heavy tenant: one row in eight has spans, so the NULL-span test passes nearly every row. */
    private static final int PROSE_FULL_ROWS = 1;

    private static byte[] chash(int doc, int row) {
        byte[] b = new byte[32];
        b[0] = (byte) (doc >> 8);
        b[1] = (byte) doc;
        b[2] = (byte) row;
        b[31] = 1;
        return b;
    }

    /** DOCS documents of ROWS_PER_DOC manifest rows each, all converged: nothing for either rung to fill. */
    private void seedConverged(int fullRows) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, COLL);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, COLL);
            var docs = new ArrayList<org.jooq.Query>();
            var chunks = new ArrayList<org.jooq.Query>();
            var manifest = new ArrayList<org.jooq.Query>();
            for (int d = 0; d < DOCS; d++) {
                String tumbler = "9." + d + ".1";
                String path = "docs/doc-" + d + ".md";
                docs.add(dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                    .set(CATALOG_DOCUMENTS.TITLE, "doc " + d).set(CATALOG_DOCUMENTS.FILE_PATH, path)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLL).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "prose")
                    .set(CATALOG_DOCUMENTS.INDEXED_AT, OffsetDateTime.parse(DOC_AT))
                    .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{}")));
                for (int r = 0; r < ROWS_PER_DOC; r++) {
                    String title = path + ":chunk-" + r;
                    String meta = r < fullRows
                        ? "{\"title\":\"" + title + "\",\"indexed_at\":\"" + OK_AT + "\",\"section_title\":\"S\","
                            + "\"line_start\":" + (r * 10 + 1) + ",\"line_end\":" + (r * 10 + 9) + "}"
                        : r == fullRows
                            ? "{\"title\":\"" + title + "\",\"indexed_at\":\"" + OK_AT + "\"}"
                            : "{\"title\":\"" + title + "\",\"indexed_at\":\"" + OK_AT + "\",\"section_title\":\"S\"}";
                    chunks.add(dsl.insertInto(CHUNKS)
                        .set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, COLL).set(CHUNKS.CHASH, chash(d, r))
                        .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, "stub")
                        .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f))
                        .set(CHUNKS.METADATA, JSONB.valueOf(meta)));
                    var m = dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                        .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
                        .set(CATALOG_DOCUMENT_CHUNKS.POSITION, r).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, r)
                        .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash(d, r))
                        .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLL).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                        .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, r != fullRows);
                    manifest.add(r < fullRows
                        ? m.set(CATALOG_DOCUMENT_CHUNKS.LINE_START, r * 10 + 1).set(CATALOG_DOCUMENT_CHUNKS.LINE_END, r * 10 + 9)
                            .set(CATALOG_DOCUMENT_CHUNKS.CHAR_START, 0).set(CATALOG_DOCUMENT_CHUNKS.CHAR_END, 80)
                        : m);
                }
            }
            dsl.batch(docs).execute();
            for (int i = 0; i < chunks.size(); i += 400) {
                dsl.batch(chunks.subList(i, Math.min(i + 400, chunks.size()))).execute();
            }
            dsl.batch(manifest).execute();
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    /** The lines of the plan node that scans the manifest table itself (not one of the aliased holder subqueries). */
    private static String manifestScanNode(String plan) {
        List<String> lines = plan.lines().toList();
        Pattern scan = Pattern.compile("on catalog_document_chunks\\s+\\(");
        for (int i = 0; i < lines.size(); i++) {
            if (scan.matcher(lines.get(i)).find()) {
                var node = new StringBuilder(lines.get(i));
                for (int j = i + 1; j < lines.size() && !lines.get(j).trim().startsWith("->")
                        && !lines.get(j).contains("Planning") && !lines.get(j).contains("Execution"); j++) {
                    node.append('\n').append(lines.get(j));
                }
                return node.toString();
            }
        }
        return "";
    }

    /** The plan of {@code kind} over the seeded tenant, with the converged counts asserted first, and the manifest scan's node. */
    private String planAndScanNode(ManifestFillableCount.Kind kind, String label) throws Exception {
        var body = fillable(kind.wire(), 1000);
        assertThat(body).as(kind.wire() + ": a converged tenant").containsEntry("fillable", 0).containsEntry("edge", 0);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS)).as("the volume the plan is read at")
                .isEqualTo(DOCS * ROWS_PER_DOC);
            var statement = ManifestFillableCount.query(ctx, TENANT, kind, 1000);
            String plan = PgContainerHelper.explainPlanInlined(ctx, statement);
            String analysed = PgContainerHelper.explainPlanWith(ctx, statement,
                PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS);
            System.out.println("plan of " + kind.wire() + " (" + label + "):\n" + plan);
            System.out.println("plan with ANALYZE and BUFFERS of " + kind.wire() + " (" + label + "):\n" + analysed);
            String node = manifestScanNode(plan);
            assertThat(node).as(kind.wire() + ": the plan has a scan of the manifest table\n" + plan).isNotEmpty();
            return node;
        }
    }

    private static void assertSpanTestAtTheManifestScan(String node) {
        assertThat(node).as("the NULL-span-column test is applied at the manifest scan\n" + node)
            .contains("line_start IS NULL").contains("line_end IS NULL")
            .contains("char_start IS NULL").contains("char_end IS NULL");
    }

    @Test
    void theManifestOnlyPrefilterReachesTheManifestScan_mixedTenant() throws Exception {
        seedConverged(MIXED_FULL_ROWS);
        assertThat(planAndScanNode(ManifestFillableCount.Kind.LABELS, "3200 manifest rows, converged"))
            .as("labels_written = false is applied at the manifest scan").containsIgnoringCase("labels_written");
        assertSpanTestAtTheManifestScan(planAndScanNode(ManifestFillableCount.Kind.SPANS, "3200 manifest rows, converged"));
    }

    /**
     * Where the manifest-only test is NOT selective (here 7 rows in 8 hold a NULL span column) PostgreSQL's own
     * extraction of a restriction from the OR of arms ({@code fills}) drops it as not selective enough, so the plan
     * carries the test only because it is an explicit top-level conjunct. This is the case that fails without the
     * prefilter: the test then rides on the planner's heuristic and not on the statement.
     */
    @Test
    void theManifestOnlyPrefilterReachesTheManifestScan_proseHeavyTenant() throws Exception {
        seedConverged(PROSE_FULL_ROWS);
        assertSpanTestAtTheManifestScan(planAndScanNode(ManifestFillableCount.Kind.SPANS, "3200 manifest rows, prose-heavy, converged"));
    }
}
