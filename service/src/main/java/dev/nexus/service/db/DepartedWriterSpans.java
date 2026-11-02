/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.CommonTableExpression;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
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
 * <p>Maintenance, not a client write: only {@code metadata} is set, never {@code last_written_at}, so the
 * reaper's grace window does not restart (nexus-wbfpw.43). Each cleared row is still a chunk-row UPDATE, and
 * a chunk-row UPDATE re-inserts the row into the vector index (vectors-021), so a purge that clears many
 * chunks pays that per row, in the purging transaction.
 *
 * <p>Fail-open: the clear is best-effort maintenance. {@link #guarded} runs it under a savepoint; a SQL error
 * that is not a deadlock is logged and the manifest write goes on, as the sweep's sub-steps do.
 *
 * <p>This class is not on {@code TombstoneFilterGateTest}'s corpus, and it reads tombstoned holders on purpose:
 * a tombstoned document may be the one that wrote the row, exactly as in {@code ManifestLabelStage}.
 */
final class DepartedWriterSpans {

    private static final Logger log = LoggerFactory.getLogger(DepartedWriterSpans.class);

    /** Chashes per statement, the bind budget {@code PgVectorRepository.SOURCE_URI_JOIN_BATCH} also uses. */
    static final int BATCH = 300;

    /** Departing-row tuples per statement: six binds each, in one CTE the statement references four times. */
    private static final int MAX_TUPLES = 1000;

    private DepartedWriterSpans() {
    }

    /**
     * One manifest row a document stopped holding: where the chunk lives and the departing document's OWN
     * span for it ({@code null} for a field the manifest row never had).
     */
    record Gone(String collection, byte[] chash, Integer lineStart, Integer lineEnd, Integer charStart,
                Integer charEnd) {

        /** The columns a caller selects (or RETURNs) to build {@link Gone}s with {@link #of}. */
        static final List<SelectField<?>> COLUMNS = List.of(CATALOG_DOCUMENT_CHUNKS.COLLECTION,
            CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END,
            CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END);

        static Gone of(Record r) {
            return new Gone(r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION), r.get(CATALOG_DOCUMENT_CHUNKS.CHASH),
                r.get(CATALOG_DOCUMENT_CHUNKS.LINE_START), r.get(CATALOG_DOCUMENT_CHUNKS.LINE_END),
                r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_START), r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_END));
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
     * to the savepoint, and answered with {@code fallback}: the clear and its reads are maintenance, and an
     * error in them must not abort the manifest write that triggered them.
     */
    static <T> T guarded(DSLContext ctx, String tenant, String cause, Callable<T> body, T fallback) {
        Connection conn = ctx.configuration().connectionProvider().acquire();
        Savepoint sp = null;
        try {
            sp = conn.setSavepoint();
            T out = body.call();
            conn.releaseSavepoint(sp);
            return out;
        } catch (Exception e) {
            if (DeadlockRetry.isDeadlock(e)) {
                if (e instanceof RuntimeException re) throw re;
                throw new IllegalStateException(e);
            }
            log.warn("event=departed_writer_spans_failed_open tenant={} cause={} error={}", tenant, cause, e.toString());
            if (sp != null) {
                try {
                    conn.rollback(sp);
                } catch (java.sql.SQLException se) {
                    log.error("event=departed_writer_spans_savepoint_rollback_failed tenant={} cause={}",
                        tenant, cause, se);
                }
            }
            return fallback;
        } finally {
            ctx.configuration().connectionProvider().release(conn);
        }
    }

    /**
     * Clears the span fields the sole remaining holder lacks on every chunk in {@code departed}, one statement
     * per collection and batch of {@link #BATCH} chashes. The departing document's own manifest rows for those
     * chashes must already be gone in this transaction. Fail-open ({@link #guarded}).
     *
     * @return chunk rows updated, 0 when nothing was cleared or the clear failed open
     */
    static int clear(DSLContext ctx, String tenant, List<Gone> departed) {
        if (departed.isEmpty()) return 0;
        return guarded(ctx, tenant, "clear", () -> {
            int cleared = 0;
            Map<String, Map<String, List<Gone>>> byCollection = new LinkedHashMap<>();
            for (Gone g : departed) {
                byCollection.computeIfAbsent(g.collection(), k -> new LinkedHashMap<>())
                    .computeIfAbsent(g.hex(), k -> new ArrayList<>()).add(g);
            }
            for (Map.Entry<String, Map<String, List<Gone>>> e : byCollection.entrySet()) {
                List<byte[]> chashes = new ArrayList<>();
                List<Gone> tuples = new ArrayList<>();
                for (List<Gone> group : e.getValue().values()) {
                    // a chash's tuples all go in one statement: the writer test reads them together
                    if (!chashes.isEmpty() && (chashes.size() >= BATCH || tuples.size() + group.size() > MAX_TUPLES)) {
                        cleared += clearBatch(ctx, tenant, e.getKey(), chashes, tuples);
                        chashes = new ArrayList<>();
                        tuples = new ArrayList<>();
                    }
                    chashes.add(group.get(0).chash());
                    tuples.addAll(group);
                }
                if (!chashes.isEmpty()) cleared += clearBatch(ctx, tenant, e.getKey(), chashes, tuples);
            }
            if (cleared > 0) {
                log.info("event=departed_writer_spans_cleared tenant={} chunks={}", tenant, cleared);
            }
            return cleared;
        }, 0);
    }

    private static int clearBatch(DSLContext ctx, String tenant, String collection, List<byte[]> chashes,
                                  List<Gone> tuples) {
        @SuppressWarnings("unchecked")
        Row6<String, byte[], Integer, Integer, Integer, Integer>[] rows = new Row6[tuples.size()];
        for (int i = 0; i < rows.length; i++) {
            Gone g = tuples.get(i);
            rows[i] = DSL.row(DSL.val(g.collection(), SQLDataType.CLOB), DSL.val(g.chash(), SQLDataType.BLOB),
                DSL.val(g.lineStart(), SQLDataType.INTEGER), DSL.val(g.lineEnd(), SQLDataType.INTEGER),
                DSL.val(g.charStart(), SQLDataType.INTEGER), DSL.val(g.charEnd(), SQLDataType.INTEGER));
        }
        var values = DSL.values(rows).asTable("v", "collection", "chash", "ls", "le", "cs", "ce");
        var dep = DSL.name("dep").fields("collection", "chash", "ls", "le", "cs", "ce")
            .as(DSL.selectFrom(values));
        Table<?> holders = holders(ctx, tenant,
            CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection).and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(chashes)), null);
        return update(ctx, tenant, dep, holders);
    }

    /**
     * The trash purge's twin of {@link #clear}: for every chunk an AGED tombstone (deleted at or before
     * {@code threshold}, the purge's own predicate) holds and exactly one document that is NOT aged still
     * holds, clears the span fields that document lacks. One statement, driven from the aged documents'
     * rows (which are also the departing rows the writer test reads). Runs BEFORE the purge, because the
     * purge's cascade removes the aged documents' rows, so the aged set cannot be read afterwards; the
     * document that stays is the one every other reader sees as the remaining holder. Fail-open
     * ({@link #guarded}).
     *
     * @param threshold {@code now() - older_than}, the expression the purge itself evaluates
     * @return chunk rows updated
     */
    static int clearBeforeTrashPurge(DSLContext ctx, String tenant, Field<OffsetDateTime> threshold) {
        return guarded(ctx, tenant, "trash_purge", () -> {
            var aged = CATALOG_DOCUMENT_CHUNKS.as("aged_rows");
            var agedDoc = CATALOG_DOCUMENTS.as("aged_doc");
            var dep = DSL.name("dep").fields("collection", "chash", "ls", "le", "cs", "ce")
                .as(ctx.select(aged.COLLECTION, aged.CHASH, aged.LINE_START, aged.LINE_END, aged.CHAR_START,
                        aged.CHAR_END)
                    .from(aged)
                    .join(agedDoc)
                        .on(agedDoc.TENANT_ID.eq(aged.TENANT_ID).and(agedDoc.TUMBLER.eq(aged.DOC_ID)))
                    .where(aged.TENANT_ID.eq(tenant)
                        .and(agedDoc.DELETED_AT.isNotNull())
                        .and(agedDoc.DELETED_AT.le(threshold))));
            Table<?> candidates = ctx.selectDistinct(dep.field("collection", String.class).as("collection"),
                    dep.field("chash", byte[].class).as("chash"))
                .from(dep)
                .asTable("departing");
            // the holders that stay: live, or tombstoned but still inside the grace window
            Table<?> holders = holders(ctx, tenant,
                CATALOG_DOCUMENTS.DELETED_AT.isNull().or(CATALOG_DOCUMENTS.DELETED_AT.gt(threshold)), candidates);
            int cleared = update(ctx, tenant, dep, holders);
            if (cleared > 0) {
                log.info("event=departed_writer_spans_cleared tenant={} chunks={} cause=trash_purge", tenant, cleared);
            }
            return cleared;
        }, 0);
    }

    // ── the position-swap path (an upsert that put another chunk at a held position) ────────────────

    /**
     * The document's manifest rows at {@code positions}, read BEFORE an upsert that may put another chunk at
     * one of them, as position to {@link Gone}. Fail-open: {@code null} when the read failed (the displaced
     * clear is then skipped).
     */
    static Map<Integer, Gone> rowsAt(DSLContext ctx, String tenant, String docId, Collection<Integer> positions) {
        if (positions.isEmpty()) return Map.of();
        return guarded(ctx, tenant, "rows_at", () -> {
            Map<Integer, Gone> out = new LinkedHashMap<>();
            var rows = ctx.select(CATALOG_DOCUMENT_CHUNKS.POSITION).select(Gone.COLUMNS)
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
                              Map<Integer, Gone> prior, Map<Integer, String> written) {
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
            for (Record r : ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.CHASH)
                    .from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                        .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                        .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(keys)))
                    .fetch()) {
                stillHeld.add(new Gone(r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION), r.get(CATALOG_DOCUMENT_CHUNKS.CHASH),
                    null, null, null, null).pairKey());
            }
            List<Gone> gone = new ArrayList<>();
            for (Gone g : departed(displaced, null, Set.of())) {
                if (!stillHeld.contains(g.pairKey())) gone.add(g);
            }
            return clear(ctx, tenant, gone);
        }, 0);
    }

    // ── the statement ────────────────────────────────────────────────────────────────────────────────

    /** The chunk-row metadata key for each manifest span column, in the order the flags are carried. */
    private static final List<String> SPAN_KEYS = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");

    /** A metadata key no chunk row has, deleted where a field is kept (deleting an absent key changes nothing). */
    private static final String NO_KEY = "pvkbv.no-such-key";

    /** The departing-value column of {@link #SPAN_KEYS}{@code [i]} in the {@code dep} CTE. */
    private static final List<String> DEP_COLUMNS = List.of("ls", "le", "cs", "ce");

    /** The flag column of {@link #SPAN_KEYS}{@code [i]} in the holders table: some holder row lacks that field. */
    private static String flag(int i) {
        return "lacks_" + i;
    }

    /**
     * The chunks with exactly one holding document and a NULL span field on some manifest row of it: one row per
     * {@code (collection, embedding_model, chash)} carrying, per span field, whether the holder lacks it.
     *
     * @param rowScope   which manifest rows count as holders (and, without {@code candidates}, which chunks)
     * @param candidates a derived table of {@code (collection, chash)} joined for the purge's candidate set, or
     *                   {@code null}; with it the holders' documents are joined too, for {@code rowScope}
     */
    private static Table<?> holders(DSLContext ctx, String tenant, Condition rowScope, Table<?> candidates) {
        var rows = CATALOG_DOCUMENT_CHUNKS;
        List<Condition> lacks = List.of(
            DSL.condition(DSL.boolOr(rows.LINE_START.isNull())), DSL.condition(DSL.boolOr(rows.LINE_END.isNull())),
            DSL.condition(DSL.boolOr(rows.CHAR_START.isNull())), DSL.condition(DSL.boolOr(rows.CHAR_END.isNull())));
        List<SelectField<?>> columns = new ArrayList<>(List.of(rows.COLLECTION.as("collection"),
            rows.CHASH.as("chash"), rows.EMBEDDING_MODEL.as("model")));
        Condition anyLacks = DSL.falseCondition();
        for (int i = 0; i < lacks.size(); i++) {
            columns.add(DSL.field(lacks.get(i)).as(flag(i)));
            anyLacks = anyLacks.or(lacks.get(i));
        }
        var select = ctx.select(columns).from(rows);
        org.jooq.SelectJoinStep<?> from = candidates == null
            ? select
            : select
                .join(CATALOG_DOCUMENTS)
                    .on(CATALOG_DOCUMENTS.TENANT_ID.eq(rows.TENANT_ID).and(CATALOG_DOCUMENTS.TUMBLER.eq(rows.DOC_ID)))
                .join(candidates)
                    .on(candidates.field("collection", String.class).eq(rows.COLLECTION)
                        .and(candidates.field("chash", byte[].class).eq(rows.CHASH)));
        return from
            .where(rows.TENANT_ID.eq(tenant).and(rowScope))
            .groupBy(rows.COLLECTION, rows.CHASH, rows.EMBEDDING_MODEL)
            .having(DSL.countDistinct(rows.DOC_ID).eq(1).and(anyLacks))
            .asTable("span_holders");
    }

    /**
     * @param dep the departing rows, {@code (collection, chash, ls, le, cs, ce)}: the writer test reads them
     */
    private static int update(DSLContext ctx, String tenant, CommonTableExpression<?> dep, Table<?> holders) {
        Field<String> collection = holders.field("collection", String.class);
        Field<byte[]> chash = holders.field("chash", byte[].class);
        Field<String> model = holders.field("model", String.class);
        // jsonb_delete(jsonb, text) and jsonb_exists(jsonb, text) are the functions behind the "-" and "?"
        // operators: typed calls, no SQL template (RawSqlGateTest). The key is cast to text so the (jsonb, text)
        // overload is chosen over (jsonb, VARIADIC text[]), as PgVectorRepository.mergeMetadata does. Only a row
        // that has a key the holder lacks is rewritten, so a chunk whose row never carried the span costs no new
        // tuple version.
        //
        // The writer test per field: some departing row of the chunk has no value of its own, or the value the
        // chunk row holds (to_jsonb of the int equals the jsonb number, so 500 and 500.0 agree).
        Field<JSONB> stripped = CHUNKS.METADATA;
        Condition hasAKey = DSL.falseCondition();
        for (int i = 0; i < SPAN_KEYS.size(); i++) {
            Field<String> key = DSL.cast(DSL.val(SPAN_KEYS.get(i)), SQLDataType.CLOB);
            Field<Integer> value = dep.field(DEP_COLUMNS.get(i), Integer.class);
            Condition couldHaveWritten = value.isNull().or(
                DSL.function("to_jsonb", JSONB.class, value)
                    .eq(DSL.function("jsonb_extract_path", JSONB.class, CHUNKS.METADATA, key)));
            Condition clearIt = DSL.condition(holders.field(flag(i), Boolean.class)).and(DSL.exists(
                ctx.selectOne().from(dep)
                    .where(dep.field("collection", String.class).eq(CHUNKS.COLLECTION)
                        .and(dep.field("chash", byte[].class).eq(CHUNKS.CHASH))
                        .and(couldHaveWritten))));
            // one reference to the previous level per level (a CASE with the previous level in both arms would
            // double the expression at every field): delete the key when cleared, a key no row has otherwise
            Field<String> target = DSL.when(clearIt, key).otherwise(DSL.cast(DSL.val(NO_KEY), SQLDataType.CLOB));
            stripped = DSL.function("jsonb_delete", JSONB.class, stripped, target);
            hasAKey = hasAKey.or(clearIt.and(DSL.condition(DSL.function("jsonb_exists", Boolean.class,
                CHUNKS.METADATA, key))));
        }
        return ctx.with(dep).update(CHUNKS)
            .set(CHUNKS.METADATA, stripped)
            .from(holders)
            .where(CHUNKS.TENANT_ID.eq(tenant)
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(chash))
                .and(CHUNKS.EMBEDDING_MODEL.eq(model))
                .and(hasAKey))
            .execute();
    }
}
