// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.ManifestFillableCount.Kind;
import dev.nexus.service.jooq.nexus.tables.CatalogDocumentChunks;
import dev.nexus.service.jooq.nexus.tables.CatalogDocuments;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record2;
import org.jooq.Result;
import org.jooq.Select;
import org.jooq.Table;
import org.jooq.Update;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

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
 * manifest-only prefilter admits ({@code labels_written = false}; a NULL span column), in key order: a range scan of the
 * manifest on its primary key that stops when it has admitted {@code batchRows} rows. Everything else is evaluated over
 * that key range and no wider, and the whole fill is one pass: the fillable rows of the window are counted (grouped by
 * collection, so one read gives the count and the collections to gate) and, when not a dry run, written by ONE
 * {@code UPDATE ... FROM (the strict predicate over the key range)}.
 *
 * <p><b>Cost.</b> A batch costs the manifest rows its window SCANS to admit {@code batchRows}, which is the window
 * itself only while the admitted rows are dense. There is no partial index on the prefilter (an index build on an
 * existing store is boot-time heavy work nexus-f3yqd rules out), so a sparse admitted set (most rows already filled)
 * scans many rows per admitted one, inside the batch's one statement timeout; and for spans the converged case, where
 * every row whose chunk records no span stays NULL for good, walks every such row on every start. The cost of a whole
 * fill is therefore the manifest rows of the tenant past the cursor, not the fillable ones;
 * {@code ManifestFillScaleMeasurementTest} measures it.
 *
 * <p><b>Concurrent writers.</b> The UPDATE's own WHERE re-checks the TARGET row against the values the subquery read from
 * the chunk row (labels: {@code labels_written = false} and the same {@code (doc_id, position, chash)}; spans:
 * {@link ManifestFillableCount#spansFillable} over the recorded values, a NULL column the chunk records and no column it
 * contradicts), so a row a writer changed between the read and the write (a re-index, a label or span write that landed
 * first) is skipped, never overwritten: PostgreSQL re-evaluates that WHERE against the row's newest committed version
 * when the UPDATE waited on its lock. The write is a COALESCE per NULL column for spans, so a column a writer filled
 * meanwhile keeps its value.
 *
 * <p>The wait is bounded. The UPDATE runs under a {@code lock_timeout} ({@value #DEFAULT_LOCK_TIMEOUT_MS} ms, or the
 * system property {@link #LOCK_TIMEOUT_PROPERTY}) set after the sweep gates are taken, so a gate wait keeps the
 * batch's statement timeout. A writer that holds a row of the batch longer (a long reconcile commit) ends the attempt
 * with SQLSTATE 55P03, the transaction is rolled back, and the SAME window is run again in skip mode: the subquery picks
 * its target rows {@code FOR UPDATE OF cand SKIP LOCKED}, so a row somebody holds is not waited for and not written.
 * Such a row is in {@code picked} and not in {@code filled}, so the job reports it as {@code skipped_changed}, and the
 * next start fills it. The job therefore finishes whatever a writer holds.
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
 * already filled drop out; the walk still passes over every row the prefilter admits that is not fillable (see Cost).
 * A restart may instead pass the cursor a poll showed as {@code after}, and the walk begins there. The cursor is
 * progress a client may hand back, never state a restart needs.
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

    /** The longest the UPDATE of a batch waits for a row another transaction holds, before the batch runs again in skip mode. */
    public static final long DEFAULT_LOCK_TIMEOUT_MS = 5_000L;

    /** System property that replaces {@link #DEFAULT_LOCK_TIMEOUT_MS} (read on every batch); the test hook, and an operator's knob. */
    public static final String LOCK_TIMEOUT_PROPERTY = "nexus.manifest.fill.lock_timeout_ms";

    /** SQLSTATE of a lock wait that ran out ({@code lock_not_available}). */
    static final String LOCK_NOT_AVAILABLE = "55P03";

    private static final Logger log = LoggerFactory.getLogger(ManifestFill.class);

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
     * Runs one batch for {@code tenant}: normally, and in skip mode when a row of the batch stayed locked past the lock
     * timeout (see the class comment).
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
        try {
            return once(tenant, kind, after, batchRows, dryRun, false);
        } catch (RuntimeException e) {
            if (!hasSqlState(e, LOCK_NOT_AVAILABLE)) throw e;
            log.info("event=manifest_fill_lock_wait_exceeded tenant={} kind={} after={} action=rerun_skipping_locked_rows",
                tenant, kind.wire(), after);
            return once(tenant, kind, after, batchRows, dryRun, true);
        }
    }

    /**
     * One attempt, in its own transaction, under the manifest writers' deadlock belt (ManifestWriteRetryGateTest): the
     * UPDATE locks manifest rows in its own order, and a concurrent manifest writer may lock some in another.
     */
    private Batch once(String tenant, Kind kind, Cursor after, int batchRows, boolean dryRun, boolean skipLocked) {
        return DeadlockRetry.run("manifest.fill " + kind.wire(), () -> repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Long.toString(BATCH_TIMEOUT_MS));
            pinPlanner(ctx);
            pinWindow(ctx, true);
            Result<Record2<String, Integer>> window = windowQuery(ctx, tenant, kind, after, batchRows).fetch();
            pinWindow(ctx, false);
            if (window.isEmpty()) {
                return new Batch(0, 0, 0, null);
            }
            var end = window.get(window.size() - 1);
            Cursor last = new Cursor(end.value1(), end.value2());
            // one read: the fillable rows of the window per collection, whose sum is the pick and whose keys are the gates
            var perCollection = fillableByCollection(ctx, tenant, kind, after, last).fetch();
            int picked = 0;
            for (var r : perCollection) picked += r.value2();
            int filled = dryRun || picked == 0 ? 0
                : apply(ctx, tenant, kind, after, last, perCollection.getValues(0, String.class), skipLocked);
            return new Batch(window.size(), picked, filled, last);
        }));
    }

    /**
     * The planner settings of the batch transaction, and of nothing else ({@code SET LOCAL}, so they end with it): no
     * hash join, no merge join and no sequential scan, so every join of the count and the UPDATE is a nested loop of index
     * probes: the co-owner and tombstoned-holder {@code NOT EXISTS} tests of
     * {@link ManifestFillablePredicates#soleLiveOwner} probe {@code idx_catalog_chunks_chash} per window row, and the
     * document and chunk joins probe their primary keys. Left to itself the planner estimates the window at about two
     * thousand rows, finds hashing the tenant's whole manifest cheaper than that many probes, and every batch then reads
     * every manifest row of the tenant (measured at 200,000 rows: 18 to 20 ms of a 35 ms count and an 80 ms UPDATE; it
     * grows with the tenant, not the batch). Hash and merge joins off alone is not enough: the document join then falls
     * to a sequential scan of {@code catalog_documents} per window row (measured: UPDATE 176 ms), hence the third setting.
     * The whole-tenant count of {@link ManifestFillableCount} is not under this pin: there the hash anti-join is the right
     * plan. Public so a plan-shape test explains the statements under the same settings.
     *
     * <p>Sorts stay ON for the count and the UPDATE: the statement trigger of the manifest
     * ({@code trg_catalog_chunks_stamp_on_update}) plans its own queries under the transaction's settings, and with sorts
     * off it took 155 ms against 2 ms. They are off for the window alone, {@link #pinWindow}.
     */
    public static void pinPlanner(DSLContext ctx) {
        PgSession.setLocal(ctx, "enable_hashjoin", "off");
        PgSession.setLocal(ctx, "enable_mergejoin", "off");
        PgSession.setLocal(ctx, "enable_seqscan", "off");
    }

    /**
     * The window's own planner setting: no sort and no incremental sort ({@code off}), so the window stays an ordered scan
     * of the manifest's primary key that stops at its LIMIT, where a sparse admitted set would otherwise pick a parallel
     * sequential scan and a sort of every pending row; and back on ({@code !off}) before the count and the UPDATE.
     */
    public static void pinWindow(DSLContext ctx, boolean off) {
        PgSession.setLocal(ctx, "enable_sort", off ? "off" : "on");
        PgSession.setLocal(ctx, "enable_incremental_sort", off ? "off" : "on");
    }

    /**
     * How many manifest rows of {@code tenant} after {@code after} the kind's prefilter admits: the rows a walk from
     * there scans (every window row is one), so {@code scanned_rows / admittedRows} is the fraction of the walk done.
     * A tenant-scoped count on the manifest's primary key range, under the fillable count's statement timeout; null when
     * it ran past it (the answer then stays unknown; the fill does not depend on it).
     */
    public Long admittedRows(String tenant, Kind kind, Cursor after) {
        var row = CATALOG_DOCUMENT_CHUNKS;
        try {
            return repo.tenantScope().withTenant(tenant, ctx -> {
                PgSession.setLocal(ctx, "statement_timeout", Integer.toString(Integer.getInteger(
                    CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS)));
                return ctx.selectCount().from(row)
                    .where(row.TENANT_ID.eq(tenant).and(ManifestFillableCount.manifestPrefilter(row, kind))
                        .and(after(row, after)))
                    .fetchOne(0, Long.class);
            });
        } catch (RuntimeException e) {
            if (hasSqlState(e, CatalogRepository.QUERY_CANCELED)) return null;
            throw e;
        }
    }

    private static boolean hasSqlState(Throwable e, String state) {
        int depth = 0;
        for (Throwable t = e; t != null && depth < 32; t = t.getCause(), depth++) {
            if (t instanceof java.sql.SQLException se && state.equals(se.getSQLState())) return true;
        }
        return false;
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
     * The fillable rows of the key range {@code (after, last]}, counted per collection and in collection order: the
     * strict candidates of {@link ManifestFillableCount}. Their sum is the batch's pick and their collections are the
     * ones the batch gates (a collection with no fillable row in the window is never waited for). Public so a
     * plan-shape test can explain the exact statement.
     */
    public static Select<Record2<String, Integer>> fillableByCollection(DSLContext ctx, String tenant, Kind kind,
                                                                        Cursor after, Cursor last) {
        var row = CATALOG_DOCUMENT_CHUNKS;
        var doc = CATALOG_DOCUMENTS;
        return ctx.select(row.COLLECTION, DSL.count())
            .from(ManifestFillableCount.joined(row, doc))
            .where(ManifestFillableCount.candidates(ctx, tenant, kind, row, doc)
                .and(ManifestFillableCount.strict(kind, doc))
                .and(after(row, after)).and(through(row, last)))
            .groupBy(row.COLLECTION)
            .orderBy(row.COLLECTION);
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
     * The one UPDATE of a batch over the key range {@code (after, last]}: gates first, then the statement
     * ({@link #updateStatement}) under the lock timeout.
     */
    private static int apply(DSLContext ctx, String tenant, Kind kind, Cursor after, Cursor last,
                             List<String> collections, boolean skipLocked) {
        // Every manifest writer holds the sweep gate SHARED for each collection it writes, before its row locks (the
        // retire-orphans sweep takes it EXCLUSIVE; ManifestInsertGateTest). This UPDATE re-points and re-homes nothing, but
        // it joins the rule instead of arguing an exemption. The collections are those of the rows the pick found, in
        // name order; shared holders never wait on each other. A row re-homed after that read is written under the gate
        // of its old collection: the UPDATE adds no key and writes no collection, so it cannot interleave with the
        // sweep's retirement of either.
        for (String collection : collections) {
            CatalogRepository.acquireSweepGateShared(ctx, tenant, collection);
        }
        // After the gates, so a gate wait keeps the statement timeout; 0 turns the limit off in skip mode, which waits on no row.
        PgSession.setLocal(ctx, "lock_timeout", Long.toString(skipLocked ? 0L : lockTimeoutMs()));
        return updateStatement(ctx, tenant, kind, after, last, skipLocked).execute();
    }

    /**
     * The UPDATE of a batch, unexecuted: the fillable rows of the key range {@code (after, last]}, read once from the
     * chunk rows in the subquery {@code b}, written to the manifest rows they came from. With {@code skipLocked} the
     * subquery locks its target rows {@code FOR UPDATE OF cand SKIP LOCKED}, so a row another transaction holds is left
     * out of {@code b} and not written. Public so a measurement can explain the exact statement.
     */
    public static Update<?> updateStatement(DSLContext ctx, String tenant, Kind kind, Cursor after, Cursor last,
                                            boolean skipLocked) {
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
        return kind == Kind.LABELS
            ? applyLabels(ctx, tenant, t, from, fillable, cand, skipLocked, bDoc, bPos, bChash)
            : applySpans(ctx, tenant, t, from, fillable, cand, skipLocked, bDoc, bPos, bChash);
    }

    private static long lockTimeoutMs() {
        return Long.getLong(LOCK_TIMEOUT_PROPERTY, DEFAULT_LOCK_TIMEOUT_MS);
    }

    /** The subquery {@code b}: the picked rows with the values to write, locked {@code SKIP LOCKED} in skip mode. */
    private static Table<?> picked(DSLContext ctx, Table<?> from, Condition fillable, CatalogDocumentChunks cand,
                                   boolean skipLocked, Field<?>... columns) {
        var pick = ctx.select(columns).from(from).where(fillable);
        Select<?> chosen = skipLocked ? pick.forUpdate().of(cand).skipLocked() : pick;
        return chosen.asTable("b");
    }

    private static Condition sameRow(CatalogDocumentChunks t, String tenant, Table<?> b, Field<String> bDoc,
                                     Field<Integer> bPos, Field<byte[]> bChash) {
        return t.TENANT_ID.eq(tenant)
            .and(t.DOC_ID.eq(b.field(bDoc)))
            .and(t.POSITION.eq(b.field(bPos)))
            .and(t.CHASH.eq(b.field(bChash)));
    }

    private static Update<?> applyLabels(DSLContext ctx, String tenant, CatalogDocumentChunks t, Table<?> from,
                                   Condition fillable, CatalogDocumentChunks cand, boolean skipLocked,
                                   Field<String> bDoc, Field<Integer> bPos, Field<byte[]> bChash) {
        Field<String> title = labelText("section_title").as("b_title");
        Field<String> type = labelText("section_type").as("b_type");
        Field<Integer> page = DSL.when(ManifestFillableCount.anyInt("page_number"),
            DSL.nullif(ManifestFillableCount.asInt("page_number"), 0)).as("b_page");
        Table<?> b = picked(ctx, from, fillable, cand, skipLocked, bDoc, bPos, bChash, title, type, page);
        return ctx.update(CATALOG_DOCUMENT_CHUNKS)
            .set(t.SECTION_TITLE, b.field(title))
            .set(t.SECTION_TYPE, b.field(type))
            .set(t.PAGE_NUMBER, b.field(page))
            .set(t.LABELS_WRITTEN, true)
            .from(b)
            // the target row's own guard: a row a concurrent writer already labelled is not written over
            .where(sameRow(t, tenant, b, bDoc, bPos, bChash).and(t.LABELS_WRITTEN.isFalse()));
    }

    /** A label key's value: the chunk's string, or the empty label for an absent key or a JSON null (the rung's {@code or ""}). */
    private static Field<String> labelText(String key) {
        return DSL.when(ManifestFillableCount.metaType(key).eq("string"), ManifestFillableCount.metaText(key))
            .otherwise(DSL.inline(""));
    }

    private static Update<?> applySpans(DSLContext ctx, String tenant, CatalogDocumentChunks t, Table<?> from,
                                  Condition fillable, CatalogDocumentChunks cand, boolean skipLocked,
                                  Field<String> bDoc, Field<Integer> bPos, Field<byte[]> bChash) {
        Field<Integer> ls = ManifestFillableCount.recordedLineStart().as("b_ls");
        Field<Integer> le = ManifestFillableCount.recordedLineEnd().as("b_le");
        Field<Integer> cs = ManifestFillableCount.recordedCharStart().as("b_cs");
        Field<Integer> ce = ManifestFillableCount.recordedCharEnd().as("b_ce");
        Table<?> b = picked(ctx, from, fillable, cand, skipLocked, bDoc, bPos, bChash, ls, le, cs, ce);
        Field<Integer> rls = b.field(ls), rle = b.field(le), rcs = b.field(cs), rce = b.field(ce);
        return ctx.update(CATALOG_DOCUMENT_CHUNKS)
            .set(t.LINE_START, DSL.coalesce(t.LINE_START, rls))
            .set(t.LINE_END, DSL.coalesce(t.LINE_END, rle))
            .set(t.CHAR_START, DSL.coalesce(t.CHAR_START, rcs))
            .set(t.CHAR_END, DSL.coalesce(t.CHAR_END, rce))
            .from(b)
            // the target row's own guard: a NULL column the chunk records and no column it contradicts, now
            .where(sameRow(t, tenant, b, bDoc, bPos, bChash)
                .and(ManifestFillableCount.spansFillable(t, rls, rle, rcs, rce)));
    }
}
