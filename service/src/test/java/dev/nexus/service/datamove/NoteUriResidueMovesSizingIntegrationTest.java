// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * RDR-229 P2.10 (nexus-p2m1x.15), the sizing step on a LOCAL store. Off unless {@code -Dnexus.p210.sizing=<rows>}; it prints
 * one line per move and chunk size ({@code p210-sizing}), which the bead notes and the changeset comment quote. It times
 * the three shipped moves over a seeded store of {@code <rows>} catalog documents (one tenth carrying residue on a
 * prefix-free docs label, an aspect row for every second document and a highlight row for every fourth), with
 * {@code sleep_ms} 0 so the figure is the chunk's own cost: dml_utils walks every row of the driving table whatever the
 * template matches, so the cost follows the table's size and not the residue count (T2 229-research-66). The shipped sizing
 * was measured on a production PITR fork (scripts/spikes/rdr229-p210-fork-timing/run.sh, 2026-11-20); this is the
 * local-store companion, which also times the rewrites.
 */
@EnabledIfSystemProperty(named = "nexus.p210.sizing", matches = "\\d+")
class NoteUriResidueMovesSizingIntegrationTest extends DataMoveIntegrationBase {

    private static final String TENANT = "sizing";
    private static final OffsetDateTime NOW = OffsetDateTime.parse("2026-11-20T00:00:00Z");
    private static final org.jooq.Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL_F = DSL.field(DSL.name("label"), String.class);
    private static final List<String> LABELS = List.of(NoteUriResidueRepairMovesIntegrationTest.DOCS_MOVE,
        NoteUriResidueRepairMovesIntegrationTest.ASPECTS_MOVE, NoteUriResidueRepairMovesIntegrationTest.HIGHLIGHTS_MOVE);

    /** The documents move as the migration registered it, read before the base's reset empties the registry. */
    private static Record shippedDocsMove;

    @org.junit.jupiter.api.BeforeAll
    void readTheShippedDocsMove() {
        shippedDocsMove = ctx().select().from(MOVES).where(LABEL_F.eq(LABELS.get(0))).fetchOne();
    }

    private void seed(int rows) {
        ctx().deleteFrom(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT)).execute();
        ctx().deleteFrom(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)).execute();
        ctx().deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).execute();
        ctx().deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).execute();
        PgContainerHelper.insertCollection(ctx(), TENANT, "plain-docs");
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, "docs")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq("plain-docs")).execute();
        var g = DSL.generateSeries(1, rows).as("g", "i");
        Field<Integer> i = DSL.field(DSL.name("g", "i"), Integer.class);
        Field<String> uri = DSL.when(i.mod(10).eq(0), DSL.val("chroma://plain-docs/f").concat(DSL.cast(i, String.class)))
            .otherwise(DSL.val("file:///r/f").concat(DSL.cast(i, String.class)));
        Field<String> tumbler = DSL.val("m").concat(DSL.cast(i, String.class));
        ctx().insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI)
            .select(DSL.select(DSL.val(TENANT), tumbler, tumbler, DSL.val("plain-docs"), uri).from(g)).execute();
        ctx().insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
            .select(DSL.select(DSL.val(TENANT), DSL.val("plain-docs"), DSL.val("p/").concat(DSL.cast(i, String.class)),
                    DSL.val(NOW), DSL.val("t"), DSL.val("t"), tumbler, uri).from(g).where(i.mod(2).eq(0))).execute();
        ctx().insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                DOCUMENT_HIGHLIGHTS.INGESTED_AT)
            .select(DSL.select(DSL.val(TENANT), tumbler, DSL.val("plain-docs"), uri, DSL.val("- h"), DSL.val(NOW))
                .from(g).where(i.mod(4).eq(0))).execute();
    }

    private void clearRegistry() {
        ctx().deleteFrom(MOVES).execute();
        for (String label : LABELS) {
            for (String run : DataMoveRuns.activeRunLabels(ctx(), label)) {
                DataMoveRuns.archive(ctx(), run);
            }
        }
    }

    /** A shipped row (as the changeset or its on_complete wrote it) registered with {@code chunk}, no pause, no on_complete. */
    private void register(Record shipped, int chunk) {
        register(shipped, chunk, false);
    }

    private void register(Record shipped, int chunk, boolean withOnComplete) {
        ctx().insertInto(MOVES)
            .columns(LABEL_F, DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                DSL.field(DSL.name("chunk_size"), Integer.class), DSL.field(DSL.name("sleep_ms"), Integer.class),
                DSL.field(DSL.name("statement_timeout_ms"), Integer.class),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("changeset_id"), String.class),
                DSL.field(DSL.name("on_complete"), String.class))
            .values(shipped.get("label", String.class), shipped.get("template", String.class),
                shipped.get("driving_schema", String.class), shipped.get("driving_table", String.class), chunk, 0,
                shipped.get("statement_timeout_ms", Integer.class), shipped.get("verification", String.class),
                shipped.get("changeset_id", String.class),
                withOnComplete ? shipped.get("on_complete", String.class) : null)
            .execute();
    }

    @Test
    void timeTheThreeShippedMoves() {
        int rows = Integer.getInteger("nexus.p210.sizing");
        // the satellites' rows are the ones the documents move's on_complete registers: run it once on a small store to read them
        seed(1_000);
        clearRegistry();
        register(shippedDocsMove, shippedDocsMove.get("chunk_size", Integer.class), true);
        var once = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        for (int i = 0; i < 6 && once.runCycle() > 0; i++) { /* until idle */ }
        List<Record> shipped = new java.util.ArrayList<>();
        for (String label : LABELS) {
            Record r = ctx().select().from(MOVES).where(LABEL_F.eq(label)).fetchOne();
            assertThat(r).as(label + " registered").isNotNull();
            shipped.add(r);
        }
        for (int chunk : new int[] {500, 1_000, 5_000, 20_000}) {
            seed(rows);
            StringBuilder line = new StringBuilder(String.format("p210-sizing rows=%d chunk_size=%d", rows, chunk));
            for (Record move : shipped) {
                clearRegistry();
                register(move, chunk);
                String label = move.get("label", String.class);
                var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
                long t0 = System.nanoTime();
                assertThat(w.runCycle()).isEqualTo(1);
                double secs = (System.nanoTime() - t0) / 1e9;
                assertThat(completedAt(label)).as(label).isNotNull();
                line.append(String.format(" %s_s=%.2f", move.get("driving_table", String.class), secs));
            }
            System.out.println(line);
        }
    }
}
