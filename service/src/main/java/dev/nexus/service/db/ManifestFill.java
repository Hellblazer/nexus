// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.ManifestFillableCount.Kind;
import dev.nexus.service.jooq.nexus.tables.CatalogDocumentChunks;
import dev.nexus.service.jooq.nexus.tables.CatalogDocuments;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record1;
import org.jooq.Record2;
import org.jooq.Result;
import org.jooq.Select;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

/**
 * nexus-f3yqd, route R2 of T2 {@code nexus/rung-detect-route-spec-2026-11-10}: one BATCH of the engine-side fill of the
 * manifest section labels and the manifest spans, in place of each rung's client walk, its by-id chunk reads and its
 * {@code indexed_at} restore.
 *
 * <p><b>Which rows.</b> Exactly the rows {@link ManifestFillableCount} counts as {@code fillable}: the relaxed
 * candidates ({@link ManifestFillableCount#candidates}) that also pass {@link ManifestFillableCount#strict}. Both come
 * from there, so a completed fill leaves {@code fillable} 0 and the rows the count calls {@code edge} are never written
 * (the count reports them). Nothing here restates a guard; the one predicate the count reads is the one this reads.
 *
 * <p><b>One batch, one transaction</b> ({@link #batch}), under {@code tenantScope.withTenant}. A batch is a WINDOW of
 * the next {@code batchRows} manifest rows after the keyset cursor {@code (doc_id, position)} that the kind's
 * manifest-only prefilter admits ({@code labels_written = false}; a NULL span column), in key order: a bounded range
 * scan of the manifest alone. Everything else is evaluated over that key range and no wider, so a batch costs the
 * window, whatever the size of the tenant, and the whole fill is one pass: the rows of the window that are fillable
 * are counted, and (when not a dry run) written by ONE {@code UPDATE ... FROM (the strict predicate over the key range)}.
 * The UPDATE's own WHERE re-checks the TARGET row against the values the subquery read from the chunk row (labels:
 * {@code labels_written = false}; spans: {@link ManifestFillableCount#spansFillable} over the recorded values: a NULL
 * column the chunk records and no column it contradicts), so a row a concurrent writer changed between the read and the
 * write (a re-index, a label or span write that landed first) is skipped, never overwritten: PostgreSQL re-evaluates
 * that WHERE against the row's newest committed version when the UPDATE waited on its lock. The write is a COALESCE per
 * NULL column for spans, so a column a writer filled meanwhile keeps its value.
 *
 * <p><b>Values</b> (spec section 3). Labels: {@code section_title} and {@code section_type} are the chunk's string, or
 * the empty label for an absent key or a JSON null; {@code page_number} is the chunk's integer, NULL for absent, null or
 * 0; {@code labels_written} becomes true. Spans: each NULL column takes the value the chunk records for it
 * ({@link ManifestFillableCount#recordedLineStart} and the rest, the {@code manifest_span} rule by value).
 *
 * <p><b>What it does not touch.</b> {@code catalog_documents.indexed_at} and every chunk row. The only UPDATE trigger on
 * the manifest ({@code trg_catalog_chunks_stamp_on_update}) acts on the {@code (collection, chash)} keys a statement
 * drops, and this statement drops none, so the restamp the client's append path suffered (and its restore step) does
 * not exist here. {@code ManifestFillJobTest} proves both.
 *
 * <p><b>Resume.</b> Fill-only: a filled row leaves the predicate, so a restart reruns from the start and the rows
 * already filled drop out. The cursor is progress, never state a restart needs.
 */
public final class ManifestFill {

    /** The batch size a request without one uses. */
    public static final int DEFAULT_BATCH_ROWS = 1_000;

    /** The largest batch one request may ask for: one transaction holds this many row locks and trigger transition rows. */
    public static final int MAX_BATCH_ROWS = 5_000;

    /**
     * The statement timeout of one batch (the window, the count and the UPDATE share it). A batch that outlasts it is SQLSTATE 57014
     * and ends the job failed with what it had committed; a new job resumes by construction.
     */
    public static final long BATCH_TIMEOUT_MS = 120_000L;

    /** A keyset position in the manifest: the last key of the window a batch scanned. */
    public record Cursor(String docId, int position) { }

    /**
     * What one batch did: {@code scanned} manifest rows in its window (below the batch size means the window reached
     * the end of the manifest, so the fill is done), {@code picked} of them fillable (a dry run's whole answer),
     * {@code filled} rows the UPDATE wrote (0 on a dry run; below {@code picked} when a concurrent change took some),
     * and {@code next}, the last key of the window, the cursor to continue after (null for an empty window).
     */
    public record Batch(int scanned, int picked, int filled, Cursor next) { }

    private final CatalogRepository repo;

    public ManifestFill(CatalogRepository repo) {
        this.repo = repo;
    }

    /**
     * Runs one batch for {@code tenant}.
     *
     * @param after     the keyset cursor to continue after, or null for the start
     * @param batchRows 1 to {@link #MAX_BATCH_ROWS}
     * @param dryRun    count the batch and write nothing
     * @throws IllegalArgumentException when {@code batchRows} is outside that range
     */
    public Batch batch(String tenant, Kind kind, Cursor after, int batchRows, boolean dryRun) {
        if (batchRows < 1 || batchRows > MAX_BATCH_ROWS) {
            throw new IllegalArgumentException("batch_rows must be 1.." + MAX_BATCH_ROWS);
        }
        // The one transaction of the batch, under the manifest writers' deadlock belt (ManifestWriteRetryGateTest): the
        // UPDATE locks manifest rows in its own order, and a concurrent manifest writer may lock some in another.
        return DeadlockRetry.run("manifest.fill " + kind.wire(), () -> repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Long.toString(BATCH_TIMEOUT_MS));
            Result<Record2<String, Integer>> window = windowQuery(ctx, tenant, kind, after, batchRows).fetch();
            if (window.isEmpty()) {
                return new Batch(0, 0, 0, null);
            }
            var end = window.get(window.size() - 1);
            Cursor last = new Cursor(end.value1(), end.value2());
            int picked = fillableInRange(ctx, tenant, kind, after, last).fetchOne().value1();
            int filled = dryRun || picked == 0 ? 0 : apply(ctx, tenant, kind, after, last);
            return new Batch(window.size(), picked, filled, last);
        }));
    }

    /**
     * The keys of the next {@code limit} manifest rows after {@code after} that the kind's manifest-only prefilter
     * admits, in key order: the window of a batch. A range scan of the manifest on its primary key; public so a
     * plan-shape test can explain the exact statement.
     */
    public static Select<Record2<String, Integer>> windowQuery(DSLContext ctx, String tenant, Kind kind, Cursor after,
                                                               int limit) {
        var row = CATALOG_DOCUMENT_CHUNKS;
        return ctx.select(row.DOC_ID, row.POSITION)
            .from(row)
            .where(row.TENANT_ID.eq(tenant).and(ManifestFillableCount.manifestPrefilter(row, kind))
                .and(after(row, after)))
            .orderBy(row.DOC_ID, row.POSITION)
            .limit(limit);
    }

    /**
     * How many rows of the key range {@code (after, last]} are fillable: the strict candidates of
     * {@link ManifestFillableCount}. Public so a plan-shape test can explain the exact statement.
     */
    public static Select<Record1<Integer>> fillableInRange(DSLContext ctx, String tenant, Kind kind, Cursor after,
                                                            Cursor last) {
        var row = CATALOG_DOCUMENT_CHUNKS;
        var doc = CATALOG_DOCUMENTS;
        return ctx.select(DSL.count())
            .from(ManifestFillableCount.joined(row, doc))
            .where(ManifestFillableCount.candidates(ctx, tenant, kind, row, doc)
                .and(ManifestFillableCount.strict(kind, doc))
                .and(after(row, after)).and(through(row, last)));
    }

    /** The rows strictly after {@code after} in key order; no condition for the start. */
    private static Condition after(CatalogDocumentChunks row, Cursor after) {
        return after == null ? DSL.trueCondition()
            : DSL.row(row.DOC_ID, row.POSITION).gt(DSL.row(after.docId(), after.position()));
    }

    /** The rows up to and including {@code last} in key order. */
    private static Condition through(CatalogDocumentChunks row, Cursor last) {
        return DSL.row(row.DOC_ID, row.POSITION).le(DSL.row(last.docId(), last.position()));
    }

    /**
     * The one UPDATE of a batch over the key range {@code (after, last]}: the fillable rows of that range, read once
     * from the chunk rows in the subquery {@code b}, written to the manifest rows they came from.
     */
    private static int apply(DSLContext ctx, String tenant, Kind kind, Cursor after, Cursor last) {
        var cand = CATALOG_DOCUMENT_CHUNKS.as("cand");
        CatalogDocuments candDoc = CATALOG_DOCUMENTS.as("cand_doc");
        var t = CATALOG_DOCUMENT_CHUNKS;
        Field<String> bDoc = cand.DOC_ID.as("b_doc");
        Field<Integer> bPos = cand.POSITION.as("b_pos");
        Field<byte[]> bChash = cand.CHASH.as("b_chash");
        Condition inRange = after(cand, after).and(through(cand, last));
        Condition fillable = ManifestFillableCount.candidates(ctx, tenant, kind, cand, candDoc)
            .and(ManifestFillableCount.strict(kind, candDoc)).and(inRange);
        Table<?> from = ManifestFillableCount.joined(cand, candDoc);
        // Every manifest writer holds the sweep gate SHARED for each collection it writes, before its row locks (the
        // retire-orphans sweep takes it EXCLUSIVE; ManifestInsertGateTest). This UPDATE re-points and re-homes nothing, but
        // it joins the rule instead of arguing an exemption. Collections in key order; shared holders never wait on each other.
        for (String collection : collectionsInRange(ctx, tenant, kind, after, last)) {
            CatalogRepository.acquireSweepGateShared(ctx, tenant, collection);
        }
        return kind == Kind.LABELS
            ? applyLabels(ctx, tenant, t, from, fillable, bDoc, bPos, bChash)
            : applySpans(ctx, tenant, t, from, fillable, bDoc, bPos, bChash);
    }

    /** The distinct collections of the key range {@code (after, last]} that the kind's prefilter admits, in name order. */
    private static List<String> collectionsInRange(DSLContext ctx, String tenant, Kind kind, Cursor after, Cursor last) {
        var row = CATALOG_DOCUMENT_CHUNKS;
        return ctx.selectDistinct(row.COLLECTION).from(row)
            .where(row.TENANT_ID.eq(tenant).and(ManifestFillableCount.manifestPrefilter(row, kind))
                .and(row.COLLECTION.isNotNull()).and(after(row, after)).and(through(row, last)))
            .orderBy(row.COLLECTION)
            .fetch(row.COLLECTION);
    }

    private static Condition sameRow(CatalogDocumentChunks t, String tenant, Table<?> b, Field<String> bDoc,
                                     Field<Integer> bPos, Field<byte[]> bChash) {
        return t.TENANT_ID.eq(tenant)
            .and(t.DOC_ID.eq(b.field(bDoc)))
            .and(t.POSITION.eq(b.field(bPos)))
            .and(t.CHASH.eq(b.field(bChash)));
    }

    private static int applyLabels(DSLContext ctx, String tenant, CatalogDocumentChunks t, Table<?> from,
                                   Condition fillable, Field<String> bDoc, Field<Integer> bPos, Field<byte[]> bChash) {
        Field<String> title = labelText("section_title").as("b_title");
        Field<String> type = labelText("section_type").as("b_type");
        Field<Integer> page = DSL.when(ManifestFillableCount.anyInt("page_number"),
            DSL.nullif(ManifestFillableCount.asInt("page_number"), 0)).as("b_page");
        Table<?> b = ctx.select(bDoc, bPos, bChash, title, type, page).from(from).where(fillable).asTable("b");
        return ctx.update(CATALOG_DOCUMENT_CHUNKS)
            .set(t.SECTION_TITLE, b.field(title))
            .set(t.SECTION_TYPE, b.field(type))
            .set(t.PAGE_NUMBER, b.field(page))
            .set(t.LABELS_WRITTEN, true)
            .from(b)
            // the target row's own guard: a row a concurrent writer already labelled is not written over
            .where(sameRow(t, tenant, b, bDoc, bPos, bChash).and(t.LABELS_WRITTEN.isFalse()))
            .execute();
    }

    /** A label key's value: the chunk's string, or the empty label for an absent key or a JSON null (the rung's {@code or ""}). */
    private static Field<String> labelText(String key) {
        return DSL.when(ManifestFillableCount.metaType(key).eq("string"), ManifestFillableCount.metaText(key))
            .otherwise(DSL.inline(""));
    }

    private static int applySpans(DSLContext ctx, String tenant, CatalogDocumentChunks t, Table<?> from,
                                  Condition fillable, Field<String> bDoc, Field<Integer> bPos, Field<byte[]> bChash) {
        Field<Integer> ls = ManifestFillableCount.recordedLineStart().as("b_ls");
        Field<Integer> le = ManifestFillableCount.recordedLineEnd().as("b_le");
        Field<Integer> cs = ManifestFillableCount.recordedCharStart().as("b_cs");
        Field<Integer> ce = ManifestFillableCount.recordedCharEnd().as("b_ce");
        Table<?> b = ctx.select(bDoc, bPos, bChash, ls, le, cs, ce).from(from).where(fillable).asTable("b");
        Field<Integer> rls = b.field(ls), rle = b.field(le), rcs = b.field(cs), rce = b.field(ce);
        return ctx.update(CATALOG_DOCUMENT_CHUNKS)
            .set(t.LINE_START, DSL.coalesce(t.LINE_START, rls))
            .set(t.LINE_END, DSL.coalesce(t.LINE_END, rle))
            .set(t.CHAR_START, DSL.coalesce(t.CHAR_START, rcs))
            .set(t.CHAR_END, DSL.coalesce(t.CHAR_END, rce))
            .from(b)
            // the target row's own guard: a NULL column the chunk records and no column it contradicts, now
            .where(sameRow(t, tenant, b, bDoc, bPos, bChash)
                .and(ManifestFillableCount.spansFillable(t, rls, rle, rcs, rce)))
            .execute();
    }
}
