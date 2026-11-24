/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.Record2;
import org.jooq.Result;
import org.jooq.Row6;
import org.jooq.SelectField;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.Savepoint;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Callable;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * nexus-pvkbv, write side: a chunk row's span ({@code line_start}, {@code line_end}, {@code chunk_start_char},
 * {@code chunk_end_char}) belongs to whichever document wrote the chunk last, and the row records no writer.
 * The search stage ({@code ManifestLabelStage}) drops a NULL-span owner's field while ANOTHER document still
 * holds the chunk in the manifest, live or tombstoned. A document that stops holding the chunk leaves no
 * manifest trace, so once it is gone the remaining holder looks like the chunk's only one and its search hit
 * would show the departed writer's span. This class closes that: in the transaction that removes the manifest
 * rows, for each {@code (collection, chash)} the document stopped holding, when EXACTLY ONE document still
 * holds the chunk and that holder's manifest rows lack a span field, the field is removed from the chunk
 * row's metadata. Per field: a holder with lines and no characters loses only the characters.
 *
 * <p>Left alone: a chunk the remaining holder holds with the field (the stage reads the manifest's value), a
 * chunk two or more documents hold (the stage already drops the field), a chunk nobody else holds (the
 * reaper's, or the sweep's). Clearing can erase a span that really was the remaining holder's own, when the
 * row carries a span the manifest never had (a row written before the manifest had spans, which is the one
 * case the chunk row is the only place a span lives). The writer test narrows that: a field is cleared only
 * if some departing document could have written it, meaning its own manifest value for the chunk is NULL or
 * equals the chunk row's. A departing document whose own value is present and different did not write the
 * row's, so the field stays. A departing document with no value at all cannot be told from the writer, and
 * the field is cleared (the safe direction: the read side drops the same field once a second document holds
 * the chunk).
 *
 * <p>Lock order. The statement-level AFTER DELETE trigger of {@code catalog_document_chunks} locks the chunk
 * rows of the dropped keys in key order (FOR NO KEY UPDATE; vectors-021-3), but only the rows whose
 * {@code last_written_at} is older than one hour. So a caller running AFTER its delete updates rows its own
 * transaction already locked, in key order, only for chunks older than an hour. A chunk written within the
 * hour (the common case right after an index run) is locked by nothing before this UPDATE, and the UPDATE ...
 * FROM join has no ORDER BY, so its row-lock order is the plan's. A deadlock against a key-ordered writer is
 * therefore possible; every caller's transaction is under {@code DeadlockRetry}, and {@link #guarded} lets a
 * deadlock through to it. The trash purge ({@link #clearBeforeTrashPurge}) runs before the purge's cascade
 * delete and meets the same exposure.
 *
 * <p>Residual, left on purpose: the same trigger serialized two concurrent departures of old chunks, and for
 * young chunks nothing does. Two documents that are the last two other holders of a young chunk, leaving in
 * concurrent transactions, each still see the other's manifest rows (READ COMMITTED does not show an
 * uncommitted delete), count two holders, and neither clears. The survivor then shows the departed span
 * until it is re-indexed with force. Closing it needs a lock both take (an advisory lock per chunk, or
 * locking the chunk rows in key order first), and the cost is paid by every manifest write, not only the rare
 * race.
 *
 * <p>Not covered, by design, and this is not a closed list of what the clear leaves behind: (a) a sequential
 * departure. P wrote the row's span; A (no span of its own) and B (a span that differs from the row's) also hold
 * the chunk. P leaves: two holders remain, nothing is cleared. B leaves: A is the sole holder and lacks the
 * field, but B's own span differs from the row's, so B is not the writer and the field stays; A's hit then shows
 * P's departed span. The test cannot tell P's departed span from a legacy holder's row-only span, so it errs
 * toward keeping. (b) A zero start written before the client fix (nexus-qe78r): the Python client used to
 * send a {@code chunk_start_char} (and any other span field) of 0 as NULL, so a manifest row written then still
 * carries NULL for a true 0, reads as "never had the field", and can have a shared first chunk's 0 start
 * cleared from the chunk row. Such rows are repaired only by re-indexing the document with force; rows written
 * after the client fix carry the 0.
 *
 * <p>Maintenance, not a client write: only {@code metadata} is set, never {@code last_written_at}, so the
 * reaper's grace window does not restart (nexus-wbfpw.43). Each cleared row is still a chunk-row UPDATE, and
 * a chunk-row UPDATE re-inserts the row into the vector index (vectors-021), so a purge that clears many
 * chunks pays that per row, in the purging transaction.
 *
 * <p>Fail-open: the clear is best-effort maintenance. {@link #guarded} runs it under a savepoint; a SQL error
 * that is not a deadlock is logged, undone to the savepoint (which is released), and the manifest write goes
 * on, as the sweep's sub-steps do. Called on an autocommit connection, where no savepoint can be taken, it
 * throws instead: failing open there would skip every clear with only a warning.
 *
 * <p>This class is not on {@code TombstoneFilterGateTest}'s corpus, and it reads tombstoned holders on purpose:
 * a tombstoned document may be the one that wrote the row, exactly as in {@code ManifestLabelStage}.
 */
final class DepartedWriterSpans {

    private static final Logger log = LoggerFactory.getLogger(DepartedWriterSpans.class);

    /** Spans from gate 3 whose collection id could not be resolved (RDR-229 P3.5c): logged at WARN and counted, never kept silently. */
    private static final java.util.concurrent.atomic.AtomicLong UNRESOLVED = new java.util.concurrent.atomic.AtomicLong();

    /**
     * The one reading of the gate a pass uses: the rows a pass reads (their label and id columns) and the clear that follows them must see the
     * SAME answer, or at the one-time latch flip a row read by name is cleared by id, found to carry no id, counted unresolved and skipped
     * (folio's review F1). A caller takes it once per pass and hands it to {@code rowsAt}, {@code Gone.columns} and {@code clear*}.
     */
    static Gate3 pinned(Gate3 gate3) {
        final boolean latched = gate3.holds();
        return () -> latched;
    }

    /** The count of unresolved spans so far. Read by the tests only: no production code reads it, the WARN line at the two resolving sites is the production signal. */
    static long unresolvedSpans() {
        return UNRESOLVED.get();
    }

    /** Chashes per statement, the bind budget {@code PgVectorRepository.SOURCE_URI_JOIN_BATCH} also uses. */
    static final int BATCH = 300;

    /** Departing-row tuples per statement: six binds each, in one derived table the statement reads once (nexus-5jvny; it was a CTE read eight times). */
    private static final int MAX_TUPLES = 1000;

    private DepartedWriterSpans() {
    }

    /**
     * One manifest row a document stopped holding: where the chunk lives and the departing document's OWN
     * span for it ({@code null} for a field the manifest row never had).
     */
    record Gone(String collection, byte[] chash, Integer lineStart, Integer lineEnd, Integer charStart,
                Integer charEnd, Long collectionId) {

        /**
         * A row with no id: the name path's shape. From gate 3 {@link #clear} does not resolve a missing id from the label (RDR-229
         * P3.5c, linda 2026-11-23): such a span is counted unresolved and left alone; a producer that has switched passes {@code collectionId}.
         */
        Gone(String collection, byte[] chash, Integer lineStart, Integer lineEnd, Integer charStart, Integer charEnd) {
            this(collection, chash, lineStart, lineEnd, charStart, charEnd, null);
        }

        /** The columns a caller selects (or RETURNs) to build {@link Gone}s with {@link #of}, on the name path. */
        static final List<SelectField<?>> COLUMNS = List.of(CATALOG_DOCUMENT_CHUNKS.COLLECTION,
            CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END,
            CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END);

        /** The alias of the label column {@link #columns} selects from gate 3. */
        private static final String LABEL = "gone_collection";

        /**
         * {@link #COLUMNS} before gate 3. From gate 3 the collection is the registry's current label of the row's id (a rename
         * leaves the row's own name column stale), and the id comes along as the seventh column (RDR-229 P3.5c).
         */
        static List<SelectField<?>> columns(Gate3 gate3) {
            if (!gate3.holds()) {
                return COLUMNS;
            }
            var rows = CATALOG_DOCUMENT_CHUNKS;
            return List.of(CollectionIdReads.labelOf(gate3, rows.TENANT_ID, rows.COLLECTION, rows.COLLECTION_ID).as(LABEL),
                rows.CHASH, rows.LINE_START, rows.LINE_END, rows.CHAR_START, rows.CHAR_END, rows.COLLECTION_ID);
        }

        static Gone of(Record r) {
            boolean byId = r.field(LABEL) != null;
            return new Gone(byId ? r.get(LABEL, String.class) : r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION),
                r.get(CATALOG_DOCUMENT_CHUNKS.CHASH),
                r.get(CATALOG_DOCUMENT_CHUNKS.LINE_START), r.get(CATALOG_DOCUMENT_CHUNKS.LINE_END),
                r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_START), r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_END),
                byId ? r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID) : null);
        }

        static List<Gone> allOf(Collection<? extends Record> rows) {
            List<Gone> out = new ArrayList<>(rows.size());
            for (Record r : rows) out.add(of(r));
            return out;
        }

        /** Lower-case hex of the chash, the key the departure comparisons use. */
        String hex() {
            return HexFormat.of().formatHex(chash);
        }

        String pairKey() {
            return collection + "\u0000" + hex();
        }

        /** The pair by the collection's id (from gate 3): a label can name two registry rows, an id names one. */
        String idPairKey() {
            return collectionId + "\u0000" + hex();
        }

        private String tupleKey() {
            return pairKey() + "\u0000" + lineStart + "," + lineEnd + "," + charStart + "," + charEnd;
        }
    }

    /**
     * The rows a manifest rewrite stopped holding: those in {@code removed} (the rows the delete returned or the
     * before-read listed) that the rewrite did not write back. A rewrite writes every row under
     * {@code keptCollection}, so a row under another collection (a move) is always departed.
     *
     * @param removed        the deleted rows
     * @param keptCollection the collection the replacement rows name, or {@code null} when it writes none
     * @param keptChashes    the replacement rows' chashes, lower-case hex
     * @return the departed rows, each distinct tuple once; empty when nothing departed
     */
    static List<Gone> departed(Collection<Gone> removed, String keptCollection, Set<String> keptChashes) {
        List<Gone> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (Gone g : removed) {
            if (g.collection() == null || g.chash() == null) {
                continue;
            }
            if (g.collection().equals(keptCollection) && keptChashes.contains(g.hex())) {
                continue;
            }
            if (seen.add(g.tupleKey())) out.add(g);
        }
        return out;
    }

    /**
     * Runs {@code body} under a savepoint on the transaction's own connection. A deadlock propagates, so the
     * surrounding {@code DeadlockRetry} re-runs the whole transaction. Any other failure is logged, undone back
     * to the savepoint (which is then released, so the transaction does not accumulate one per failure), and
     * answered with {@code fallback}: the clear and its reads are maintenance, and an error in them must not
     * abort the manifest write that triggered them.
     *
     * <p>The savepoint needs an open transaction. On an autocommit connection the driver refuses
     * {@code setSavepoint}, and failing open there would skip the clear on every call with only a warning, so
     * it is refused up front with an {@link IllegalStateException} that is not caught here. Every caller runs
     * inside {@code TenantScope.withTenant}, which turns autocommit off.
     */
    static <T> T guarded(DSLContext ctx, String tenant, String cause, Callable<T> body, T fallback) {
        Connection conn = ctx.configuration().connectionProvider().acquire();
        try {
            requireTransaction(conn, tenant, cause);
            Savepoint sp = null;
            try {
                sp = conn.setSavepoint();
                T out = body.call();
                conn.releaseSavepoint(sp);
                return out;
            } catch (Exception e) {
                return failOpen(conn, sp, tenant, cause, e, fallback);
            }
        } finally {
            ctx.configuration().connectionProvider().release(conn);
        }
    }

    private static void requireTransaction(Connection conn, String tenant, String cause) {
        boolean autoCommit;
        try {
            autoCommit = conn.getAutoCommit();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException("departed-writer span clear (" + cause + ", tenant " + tenant
                + "): cannot read the connection's auto-commit mode", e);
        }
        if (autoCommit) {
            throw new IllegalStateException("departed-writer span clear (" + cause + ", tenant " + tenant
                + ") needs an open transaction (a savepoint cannot be taken in auto-commit mode); it is not run");
        }
    }

    private static <T> T failOpen(Connection conn, Savepoint sp, String tenant, String cause, Exception e, T fallback) {
        if (DeadlockRetry.isDeadlock(e)) {
            if (e instanceof RuntimeException re) throw re;
            throw new IllegalStateException(e);
        }
        log.warn("event=departed_writer_spans_failed_open tenant={} cause={} error={}", tenant, cause, e.toString());
        if (sp != null) {
            try {
                conn.rollback(sp);
                // ROLLBACK TO SAVEPOINT keeps the savepoint defined; release it so a long transaction that fails
                // open again and again does not pile them up.
                conn.releaseSavepoint(sp);
            } catch (java.sql.SQLException se) {
                log.error("event=departed_writer_spans_savepoint_rollback_failed tenant={} cause={}",
                    tenant, cause, se);
            }
        }
        return fallback;
    }

    /**
     * Clears the span fields the sole remaining holder lacks on every chunk in {@code departed}, one statement
     * per collection and batch of {@link #BATCH} chashes. The departing document's own manifest rows for those
     * chashes must already be gone in this transaction. Fail-open ({@link #guarded}).
     *
     * @return chunk rows updated, 0 when nothing was cleared or the clear failed open
     */
    static int clear(DSLContext ctx, String tenant, List<Gone> departed, Gate3 gate3) {
        if (departed.isEmpty()) return 0;
        final boolean byId = gate3.holds();
        return guarded(ctx, tenant, "clear", () -> {
            int cleared = 0;
            int unresolved = 0;
            // RDR-229 P3.5c: from gate 3 a collection is its id. A span with no collection_id is NOT resolved by its stored name (a stale
            // label may name another collection now): it is counted unresolved and logged at WARN below, and left alone (linda 2026-11-23).
            Map<Object, Map<String, List<Gone>>> byCollection = new LinkedHashMap<>();
            for (Gone g : departed) {
                Gone keyed = g;
                Object key = byId ? keyed.collectionId() : keyed.collection();
                if (key == null) {
                    unresolved++;   // never kept silently: logged and counted below
                    continue;
                }
                byCollection.computeIfAbsent(key, k -> new LinkedHashMap<>())
                    .computeIfAbsent(keyed.hex(), k -> new ArrayList<>()).add(keyed);
            }
            for (Map.Entry<Object, Map<String, List<Gone>>> e : byCollection.entrySet()) {
                List<byte[]> chashes = new ArrayList<>();
                List<Gone> tuples = new ArrayList<>();
                for (List<Gone> group : e.getValue().values()) {
                    // a chash's tuples all go in one statement: the writer test reads them together
                    if (!chashes.isEmpty() && (chashes.size() >= BATCH || tuples.size() + group.size() > MAX_TUPLES)) {
                        cleared += clearBatch(ctx, tenant, e.getKey(), chashes, tuples, byId);
                        chashes = new ArrayList<>();
                        tuples = new ArrayList<>();
                    }
                    chashes.add(group.get(0).chash());
                    tuples.addAll(group);
                }
                if (!chashes.isEmpty()) cleared += clearBatch(ctx, tenant, e.getKey(), chashes, tuples, byId);
            }
            if (unresolved > 0) {
                UNRESOLVED.addAndGet(unresolved);
                log.warn("event=departed_writer_spans_unresolved tenant={} spans={} cause=clear reason=no_collection_id", tenant, unresolved);
            }
            if (cleared > 0) {
                log.info("event=departed_writer_spans_cleared tenant={} chunks={}", tenant, cleared);
            }
            return cleared;
        }, 0);
    }

    private static int clearBatch(DSLContext ctx, String tenant, Object collection, List<byte[]> chashes,
                                  List<Gone> tuples, boolean byId) {
        @SuppressWarnings("unchecked")
        Row6<Object, byte[], Integer, Integer, Integer, Integer>[] rows = new Row6[tuples.size()];
        for (int i = 0; i < rows.length; i++) {
            Gone g = tuples.get(i);
            rows[i] = DSL.row(asObject(byId ? DSL.val(g.collectionId(), SQLDataType.BIGINT) : DSL.val(g.collection(), SQLDataType.CLOB)),
                DSL.val(g.chash(), SQLDataType.BLOB),
                DSL.val(g.lineStart(), SQLDataType.INTEGER), DSL.val(g.lineEnd(), SQLDataType.INTEGER),
                DSL.val(g.charStart(), SQLDataType.INTEGER), DSL.val(g.charEnd(), SQLDataType.INTEGER));
        }
        Table<?> dep = DSL.values(rows).asTable("dep", DEP_FIELDS);
        Table<?> holders = holders(ctx, tenant,
            keyIs(byId, CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, collection)
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(chashes)), false, byId);
        return update(ctx, tenant, collection, chashes, dep, holders, byId);
    }

    /**
     * The key the span statements join the two chunk tables on (RDR-229 P3.5c): the collection's name before gate 3, its id from
     * gate 3. It is the {@code collection} column of the derived tables {@code dep}, {@code span_holders} and {@code span_writers},
     * whose type follows. A NULL id is no collection: a row without one is never keyed.
     */
    @SuppressWarnings("unchecked")
    private static Field<Object> asObject(Field<?> f) {
        return (Field<Object>) f;
    }

    private static Field<Object> keyOf(boolean byId, Field<String> name, Field<Long> id) {
        return asObject(byId ? id : name);
    }

    private static Condition keyIs(boolean byId, Field<String> name, Field<Long> id, Object key) {
        return byId ? id.eq((Long) key) : name.eq((String) key);
    }

    /**
     * The trash purge's twin of {@link #clear}: for every chunk an AGED tombstone (deleted at or before
     * {@code threshold}, the purge's own predicate) holds and exactly one document that is NOT aged still
     * holds, clears the span fields that document lacks. Runs BEFORE the purge, because the purge's cascade
     * removes the aged documents' rows, so the aged set cannot be read afterwards; the document that stays is
     * the one every other reader sees as the remaining holder. Fail-open ({@link #guarded}).
     *
     * <p>One read finds the candidate {@code (collection, chash)} pairs: chunks an aged tombstone holds and a
     * non-aged document also holds (a chunk no one else holds is the reaper's, and has no holder to clear for).
     * They are ordered by key and run in statements of {@link #BATCH} chashes per collection, like
     * {@link #clear}; a statement reads the aged documents' manifest rows for its chashes itself (they are also
     * the departing rows the writer test reads), so unlike {@link #clear} no tuple list is sent and
     * {@link #MAX_TUPLES} does not apply. The candidate list is held in memory, about a hundred bytes a pair.
     *
     * <p>nexus-5jvny: this was one statement over every aged row of the tenant whose writer test was a
     * correlated {@code EXISTS} per holder and field, so it cost about holders x aged rows x 8; it is now one
     * aggregated join per batch ({@link #update}).
     *
     * @param threshold {@code now() - older_than}, the expression the purge itself evaluates
     * @return chunk rows updated
     */
    static int clearBeforeTrashPurge(DSLContext ctx, String tenant, Field<OffsetDateTime> threshold, Gate3 gate3) {
        final boolean byId = gate3.holds();
        return guarded(ctx, tenant, "trash_purge", () -> {
            var aged = CATALOG_DOCUMENT_CHUNKS.as("aged_rows");
            var agedDoc = CATALOG_DOCUMENTS.as("aged_doc");
            var kept = CATALOG_DOCUMENT_CHUNKS.as("kept_rows");
            var keptDoc = CATALOG_DOCUMENTS.as("kept_doc");
            Condition agedRow = agedDoc.DELETED_AT.isNotNull().and(agedDoc.DELETED_AT.le(threshold));
            Field<Object> agedKey = keyOf(byId, aged.COLLECTION, aged.COLLECTION_ID);
            Result<Record2<Object, byte[]>> candidates = ctx.selectDistinct(agedKey, aged.CHASH)
                .from(aged)
                .join(agedDoc)
                    .on(agedDoc.TENANT_ID.eq(aged.TENANT_ID).and(agedDoc.TUMBLER.eq(aged.DOC_ID)))
                .where(aged.TENANT_ID.eq(tenant).and(agedRow)
                    // a holder that stays: live, or tombstoned but still inside the grace window
                    .and(DSL.exists(ctx.selectOne().from(kept)
                        .join(keptDoc)
                            .on(keptDoc.TENANT_ID.eq(kept.TENANT_ID).and(keptDoc.TUMBLER.eq(kept.DOC_ID)))
                        .where(kept.TENANT_ID.eq(tenant)
                            .and(CollectionIdReads.same(gate3, kept.COLLECTION, kept.COLLECTION_ID, aged.COLLECTION, aged.COLLECTION_ID))
                            .and(kept.CHASH.eq(aged.CHASH))
                            .and(keptDoc.DELETED_AT.isNull().or(keptDoc.DELETED_AT.gt(threshold)))))))
                .orderBy(agedKey, aged.CHASH)
                .fetch();
            int cleared = 0;
            Object collection = null;
            List<byte[]> chashes = new ArrayList<>();
            for (Record2<Object, byte[]> c : candidates) {
                if (c.value1() == null) {   // a NULL id is no collection: logged and counted, never kept silently
                    UNRESOLVED.incrementAndGet();
                    log.warn("event=departed_writer_spans_unresolved tenant={} cause=trash_purge reason=no_collection_id", tenant);
                    continue;
                }
                if (!c.value1().equals(collection) || chashes.size() >= BATCH) {
                    if (!chashes.isEmpty()) cleared += trashBatch(ctx, tenant, threshold, collection, chashes, byId);
                    chashes = new ArrayList<>();
                    collection = c.value1();
                }
                chashes.add(c.value2());
            }
            if (!chashes.isEmpty()) cleared += trashBatch(ctx, tenant, threshold, collection, chashes, byId);
            if (cleared > 0) {
                log.info("event=departed_writer_spans_cleared tenant={} chunks={} cause=trash_purge", tenant, cleared);
            }
            return cleared;
        }, 0);
    }

    private static int trashBatch(DSLContext ctx, String tenant, Field<OffsetDateTime> threshold, Object collection,
                                  List<byte[]> chashes, boolean byId) {
        var aged = CATALOG_DOCUMENT_CHUNKS.as("aged_rows");
        var agedDoc = CATALOG_DOCUMENTS.as("aged_doc");
        Table<?> dep = ctx.select(keyOf(byId, aged.COLLECTION, aged.COLLECTION_ID), aged.CHASH, aged.LINE_START, aged.LINE_END, aged.CHAR_START,
                aged.CHAR_END)
            .from(aged)
            .join(agedDoc)
                .on(agedDoc.TENANT_ID.eq(aged.TENANT_ID).and(agedDoc.TUMBLER.eq(aged.DOC_ID)))
            .where(aged.TENANT_ID.eq(tenant)
                .and(keyIs(byId, aged.COLLECTION, aged.COLLECTION_ID, collection))
                .and(aged.CHASH.in(chashes))
                .and(agedDoc.DELETED_AT.isNotNull())
                .and(agedDoc.DELETED_AT.le(threshold)))
            .asTable("dep", DEP_FIELDS);
        // the holders that stay: live, or tombstoned but still inside the grace window
        Table<?> holders = holders(ctx, tenant,
            keyIs(byId, CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, collection)
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(chashes))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull().or(CATALOG_DOCUMENTS.DELETED_AT.gt(threshold))), true, byId);
        return update(ctx, tenant, collection, chashes, dep, holders, byId);
    }

    // ── the position-swap path (an upsert that put another chunk at a held position) ────────────────

    /**
     * The document's manifest rows at {@code positions}, read BEFORE an upsert that may put another chunk at
     * one of them, as position to {@link Gone}. Fail-open: {@code null} when the read failed (the displaced
     * clear is then skipped).
     */
    static Map<Integer, Gone> rowsAt(DSLContext ctx, String tenant, String docId, Collection<Integer> positions, Gate3 gate3) {
        if (positions.isEmpty()) return Map.of();
        return guarded(ctx, tenant, "rows_at", () -> {
            Map<Integer, Gone> out = new LinkedHashMap<>();
            var rows = ctx.select(CATALOG_DOCUMENT_CHUNKS.POSITION).select(Gone.columns(gate3))
                .from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                    .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                    .and(CATALOG_DOCUMENT_CHUNKS.POSITION.in(positions)))
                .fetch();
            for (Record r : rows) out.put(r.get(CATALOG_DOCUMENT_CHUNKS.POSITION), Gone.of(r));
            return out;
        }, null);
    }

    /**
     * After the upsert: the rows of {@code prior} that the written chunks displaced (another chunk, or the same
     * chunk under another collection, now stands at their position), minus those the document still holds at
     * another position, are cleared like any departed chunk.
     *
     * @param prior    {@link #rowsAt} before the upsert; {@code null} skips (the read failed)
     * @param written  position to the lower-case hex chash the upsert wrote there
     */
    static int clearDisplaced(DSLContext ctx, String tenant, String docId, String collection,
                              Map<Integer, Gone> prior, Map<Integer, String> written, Gate3 gate3) {
        if (prior == null || prior.isEmpty()) return 0;
        List<Gone> displaced = new ArrayList<>();
        for (Map.Entry<Integer, Gone> e : prior.entrySet()) {
            String now = written.get(e.getKey());
            Gone was = e.getValue();
            if (now != null && !(collection.equals(was.collection()) && now.equals(was.hex()))) {
                displaced.add(was);
            }
        }
        if (displaced.isEmpty()) return 0;
        return guarded(ctx, tenant, "displaced", () -> {
            List<byte[]> keys = new ArrayList<>();
            for (Gone g : displaced) keys.add(g.chash());
            Set<String> stillHeld = new HashSet<>();
            // From gate 3 the held pair and the displaced pair are keyed by (collection_id, hex), never (label, hex): a label can name two
            // rows. Before it, by the stored name, as it was.
            final boolean idKeyed = gate3.holds();
            for (Record r : ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CATALOG_DOCUMENT_CHUNKS.CHASH)
                    .from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                        .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                        .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(keys)))
                    .fetch()) {
                // the held pair is keyed by the id from gate3 (idKeyed); the name rides along for the pre-latch key
                var held = new Gone(r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION), r.get(CATALOG_DOCUMENT_CHUNKS.CHASH),
                    null, null, null, null, r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID));
                stillHeld.add(idKeyed ? held.idPairKey() : held.pairKey());
            }
            List<Gone> gone = new ArrayList<>();
            for (Gone g : departed(displaced, null, Set.of())) {
                if (!stillHeld.contains(idKeyed ? g.idPairKey() : g.pairKey())) gone.add(g);
            }
            return clear(ctx, tenant, gone, gate3);
        }, 0);
    }

    // ── the statement ────────────────────────────────────────────────────────────────────────────────

    /** The chunk-row metadata key for each manifest span column, in the order the flags are carried. */
    private static final List<String> SPAN_KEYS = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");

    /** A metadata key no chunk row has, deleted where a field is kept (deleting an absent key changes nothing). */
    private static final String NO_KEY = "pvkbv.no-such-key";

    /** The columns of the departing-rows table, {@code dep}: the chunk's key and the departing value per span field. */
    private static final String[] DEP_FIELDS = {"collection", "chash", "ls", "le", "cs", "ce"};

    /** The departing-value column of {@link #SPAN_KEYS}{@code [i]} in {@code dep}. */
    private static final List<String> DEP_COLUMNS = List.of("ls", "le", "cs", "ce");

    /** The flag column of {@link #SPAN_KEYS}{@code [i]} in the holders table: some holder row lacks that field. */
    private static String flag(int i) {
        return "lacks_" + i;
    }

    /** The flag column of {@link #SPAN_KEYS}{@code [i]} in the writers table: some departing row could have written it. */
    private static String wrote(int i) {
        return "wrote_" + i;
    }

    /**
     * The chunks with exactly one holding document and a NULL span field on some manifest row of it: one row per
     * {@code (collection, embedding_model, chash)} carrying, per span field, whether the holder lacks it.
     *
     * @param rowScope      which manifest rows count as holders, and which chunks
     * @param withDocuments whether {@code rowScope} reads {@code catalog_documents} (the trash purge's live or
     *                      in-grace test), so the holders' documents are joined
     */
    private static Table<?> holders(DSLContext ctx, String tenant, Condition rowScope, boolean withDocuments, boolean byId) {
        var rows = CATALOG_DOCUMENT_CHUNKS;
        List<Condition> lacks = List.of(
            DSL.condition(DSL.boolOr(rows.LINE_START.isNull())), DSL.condition(DSL.boolOr(rows.LINE_END.isNull())),
            DSL.condition(DSL.boolOr(rows.CHAR_START.isNull())), DSL.condition(DSL.boolOr(rows.CHAR_END.isNull())));
        Field<Object> key = keyOf(byId, rows.COLLECTION, rows.COLLECTION_ID);
        List<SelectField<?>> columns = new ArrayList<>(List.of(key.as("collection"),
            rows.CHASH.as("chash"), rows.EMBEDDING_MODEL.as("model")));
        Condition anyLacks = DSL.falseCondition();
        for (int i = 0; i < lacks.size(); i++) {
            columns.add(DSL.field(lacks.get(i)).as(flag(i)));
            anyLacks = anyLacks.or(lacks.get(i));
        }
        var select = ctx.select(columns).from(rows);
        org.jooq.SelectJoinStep<?> from = !withDocuments
            ? select
            : select.join(CATALOG_DOCUMENTS)
                .on(CATALOG_DOCUMENTS.TENANT_ID.eq(rows.TENANT_ID).and(CATALOG_DOCUMENTS.TUMBLER.eq(rows.DOC_ID)));
        return from
            .where(rows.TENANT_ID.eq(tenant).and(rowScope))
            .groupBy(key, rows.CHASH, rows.EMBEDDING_MODEL)
            .having(DSL.countDistinct(rows.DOC_ID).eq(1).and(anyLacks))
            .asTable("span_holders");
    }

    /**
     * The writer test, aggregated: one row per {@code (collection, embedding_model, chash)} of the departing rows
     * that have a chunk row, carrying per span field whether SOME departing row could have written the chunk
     * row's value: its own value is absent, or equals the chunk row's (to_jsonb of the int equals the jsonb
     * number, so 500 and 500.0 agree; a JSON null in the row equals nothing, so a departing row with a value
     * is not the writer of it). {@code bool_or} ignores the NULL a missing key gives.
     *
     * <p>nexus-5jvny: this replaces a correlated {@code EXISTS} over {@code dep} per holder row and field, which
     * PG ran as a SubPlan scanning the whole departing set each time. Here {@code dep} is read once and joined
     * to the chunk rows once, in a hash join.
     */
    private static Table<?> writers(DSLContext ctx, String tenant, Table<?> dep, boolean byId) {
        var chunk = CHUNKS.as("writer_chunk");
        Field<Object> depCollection = asObject(byId ? dep.field("collection", Long.class) : dep.field("collection", String.class));
        Field<byte[]> depChash = dep.field("chash", byte[].class);
        List<SelectField<?>> columns = new ArrayList<>(List.of(depCollection.as("collection"),
            depChash.as("chash"), chunk.EMBEDDING_MODEL.as("model")));
        for (int i = 0; i < SPAN_KEYS.size(); i++) {
            Field<String> key = DSL.cast(DSL.val(SPAN_KEYS.get(i)), SQLDataType.CLOB);
            Field<Integer> value = dep.field(DEP_COLUMNS.get(i), Integer.class);
            columns.add(DSL.boolOr(value.isNull().or(
                DSL.function("to_jsonb", JSONB.class, value)
                    .eq(DSL.function("jsonb_extract_path", JSONB.class, chunk.METADATA, key)))).as(wrote(i)));
        }
        return ctx.select(columns).from(dep)
            .join(chunk)
                .on(chunk.TENANT_ID.eq(tenant).and(keyOf(byId, chunk.COLLECTION, chunk.COLLECTION_ID).eq(depCollection))
                    .and(chunk.CHASH.eq(depChash)))
            .groupBy(depCollection, depChash, chunk.EMBEDDING_MODEL)
            .asTable("span_writers");
    }

    /**
     * RDR-229 P3.5a row 14 (nexus-p2m1x.23): the registry rows of the collections whose chunk rows this statement may rewrite, read
     * {@code FOR KEY SHARE} in id order by the id each chunk row STORES (a chunk's label can be one a one-row rename left behind, and
     * another collection may have taken it since; the stored id is the chunk's collection). A chunk row with no stored id falls back to
     * the label's rows. Returns the ids whose row is present, and the rows themselves for {@link RegistryRowLocks#forget}; a chunk whose
     * stored id has no row (the collection is gone) is left out of the statement.
     */
    private static List<CollectionRow> lockDeparted(DSLContext ctx, String tenant, Object collection, List<byte[]> chashes, boolean byId) {
        // the chunk rows of the batch: by the id the batch is keyed by from gate 3 (byId), by the label before it
        List<Long> stored = ctx.selectDistinct(CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(byId ? CHUNKS.COLLECTION_ID.eq((Long) collection) : CHUNKS.COLLECTION.eq((String) collection))
                .and(CHUNKS.CHASH.in(chashes)))
            .fetch(CHUNKS.COLLECTION_ID);
        return RegistryRowLocks.keyShareIfPresent(ctx, tenant, stored, !byId && stored.contains(null) ? List.of((String) collection) : List.of());
    }

    /**
     * @param collection the collection the chunk rows are filed under (the chunk rows are selected by it): its label before gate 3, its id from gate 3
     * @param chashes    the chunks the statement may rewrite
     * @param dep        the departing rows, {@link #DEP_FIELDS}: the writer test reads them
     */
    private static int update(DSLContext ctx, String tenant, Object collection, List<byte[]> chashes, Table<?> dep,
                              Table<?> holders, boolean byId) {
        List<CollectionRow> locked = lockDeparted(ctx, tenant, collection, chashes, byId);
        try {
            return update(ctx, tenant, dep, holders, locked.stream().map(CollectionRow::collectionId).toList(), byId);
        } catch (RuntimeException e) {
            RegistryRowLocks.forget(ctx, tenant, locked);   // the caller's savepoint rolls the locks back with the failed statement
            throw e;
        }
    }

    private static int update(DSLContext ctx, String tenant, Table<?> dep, Table<?> holders, List<Long> liveIds, boolean byId) {
        Table<?> writers = writers(ctx, tenant, dep, byId);
        Field<Object> collection = asObject(byId ? holders.field("collection", Long.class) : holders.field("collection", String.class));
        Field<byte[]> chash = holders.field("chash", byte[].class);
        Field<String> model = holders.field("model", String.class);
        // jsonb_delete(jsonb, text) and jsonb_exists(jsonb, text) are the functions behind the "-" and "?"
        // operators: typed calls, no SQL template (RawSqlGateTest). The key is cast to text so the (jsonb, text)
        // overload is chosen over (jsonb, VARIADIC text[]), as PgVectorRepository.mergeMetadata does. Only a row
        // that has a key the holder lacks is rewritten, so a chunk whose row never carried the span costs no new
        // tuple version.
        //
        // A field is cleared when the holder lacks it and some departing row could have written the row's value
        // (the writers table).
        Field<JSONB> stripped = CHUNKS.METADATA;
        Condition hasAKey = DSL.falseCondition();
        for (int i = 0; i < SPAN_KEYS.size(); i++) {
            Field<String> key = DSL.cast(DSL.val(SPAN_KEYS.get(i)), SQLDataType.CLOB);
            Condition clearIt = DSL.condition(holders.field(flag(i), Boolean.class))
                .and(DSL.condition(writers.field(wrote(i), Boolean.class)));
            // one reference to the previous level per level (a CASE with the previous level in both arms would
            // double the expression at every field): delete the key when cleared, a key no row has otherwise
            Field<String> target = DSL.when(clearIt, key).otherwise(DSL.cast(DSL.val(NO_KEY), SQLDataType.CLOB));
            stripped = DSL.function("jsonb_delete", JSONB.class, stripped, target);
            hasAKey = hasAKey.or(clearIt.and(DSL.condition(DSL.function("jsonb_exists", Boolean.class,
                CHUNKS.METADATA, key))));
        }
        // RDR-229 P3.1 stage 2b (nexus-p2m1x.17): the update also sets the collection's id, correlated on the
        // row's own name. chunks carries a NOT VALID CHECK evaluated on every updated row, so an UPDATE that left
        // the id alone would be refused (23514) on a chunk the stamp has not reached, and this clear is fail-open:
        // the refusal would leave the spans in place and report nothing. From gate 3 (P3.5c) every row is stamped and the
        // collection is read by id, so the id stays as it is: re-deriving it from a name a rename left stale would be wrong.
        return ctx.update(CHUNKS)
            .set(CHUNKS.METADATA, stripped)
            // the stored id wins, as every other id stamp of a ledger or maintenance write does: the label's id fills only an unstamped row.
            // A row whose label another collection now bears must keep the id of the collection it is in (RDR-229 P3.5a row 14). From gate 3
            // every row is stamped and the collection is read by id, so the id stays as it is
            .set(CHUNKS.COLLECTION_ID, byId ? CHUNKS.COLLECTION_ID
                : DSL.coalesce(CHUNKS.COLLECTION_ID, CollectionIds.idOf(DSL.val(tenant), CHUNKS.COLLECTION)))
            .from(holders.join(writers)
                .on(asObject(byId ? writers.field("collection", Long.class) : writers.field("collection", String.class)).eq(collection)
                    .and(writers.field("chash", byte[].class).eq(chash))
                    .and(writers.field("model", String.class).eq(model))))
            .where(CHUNKS.TENANT_ID.eq(tenant)
                .and(keyOf(byId, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID).eq(collection))
                .and(CHUNKS.CHASH.eq(chash))
                .and(CHUNKS.EMBEDDING_MODEL.eq(model))
                // a chunk whose stored collection has no registry row any more (locked above) is left alone
                .and(CHUNKS.COLLECTION_ID.isNull().or(CHUNKS.COLLECTION_ID.in(liveIds)))
                .and(hasAKey))
            .execute();
    }
}
