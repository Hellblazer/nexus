// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-z0o2p.29: a manifest replace writes the difference. The same rows leave every row in place (same
 * tuple, so nothing was written) and write no orphan record; a changed row is updated, a dropped row deleted
 * and recorded, a new row inserted; and every column of every row is what the incoming row says, whatever
 * path the replace took.
 */
class ManifestReplaceDiffTest extends AtomicWriteTestBase {

    /** A manifest row with spans and labels, so a kept row's every payload column is non-trivial. */
    private static Map<String, Object> rich(int position, String chash) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("position", position);
        r.put("chash", chash);
        r.put("chunk_index", position + 10);
        r.put("line_start", position * 10);
        r.put("line_end", position * 10 + 9);
        r.put("char_start", position * 100);
        r.put("char_end", position * 100 + 99);
        r.put("section_title", "title " + position);
        r.put("section_type", "section");
        r.put("page_number", position + 1);
        r.put("labels_written", true);
        return r;
    }

    /** A document of {@code n} chunks written through the real combined write, so the chunks exist. */
    private List<Map<String, Object>> seedDoc(Fx f, int n) {
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            String c = ch(f.docId() + "-c" + i);
            chunks.add(chunk(c, f.docId() + " text " + i));
            rows.add(rich(i, c));
        }
        svc.writeManyCombined(TENANT, f.collection(), chunks, List.of(doc(f.docId(), rows)), null, false, false);
        return rows;
    }

    /** Every column of every row of the document, plus the physical tuple (ctid, xmin), by position. */
    private Map<Integer, Map<String, Object>> snapshot(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var ctid = DSL.field(DSL.name("ctid")).cast(SQLDataType.VARCHAR);
            var xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
            Map<Integer, Map<String, Object>> out = new LinkedHashMap<>();
            for (Record r : ctx.select(CATALOG_DOCUMENT_CHUNKS.fields()).select(ctid, xmin)
                    .from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)))
                    .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION).fetch()) {
                Map<String, Object> m = new LinkedHashMap<>();
                for (var f : CATALOG_DOCUMENT_CHUNKS.fields()) {
                    Object v = r.get(f);
                    m.put(f.getName(), v instanceof byte[] b ? HexFormat.of().formatHex(b) : v);
                }
                m.put("tuple", r.get(ctid) + "/" + r.get(xmin));
                out.put(r.get(CATALOG_DOCUMENT_CHUNKS.POSITION), m);
            }
            return out;
        }
    }

    private static Map<String, Object> columns(Map<String, Object> snapshotRow) {
        Map<String, Object> m = new LinkedHashMap<>(snapshotRow);
        m.remove("tuple");
        return m;
    }

    private long records(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNK_ORPHANED_AT,
                CHUNK_ORPHANED_AT.TENANT_ID.eq(TENANT).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection)));
        }
    }

    private boolean hasRecord(String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(CHUNK_ORPHANED_AT,
                CHUNK_ORPHANED_AT.TENANT_ID.eq(TENANT).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))
                    .and(CHUNK_ORPHANED_AT.CHASH.eq(HexFormat.of().parseHex(hex))));
        }
    }

    @Test
    void aReplaceWithTheSameRows_leavesEveryRowInPlace_andWritesNoOrphanRecord() throws Exception {
        Fx f = fixture("same");
        List<Map<String, Object>> rows = seedDoc(f, 6);
        var before = snapshot(f.docId());
        assertThat(records(f.collection())).isZero();

        repo.writeManifest(TENANT, f.docId(), f.collection(), rows);

        var after = snapshot(f.docId());
        assertThat(after).as("every row, every column, the same physical tuple: nothing was rewritten")
            .isEqualTo(before);
        assertThat(records(f.collection())).as("no chunk was dropped and listed again").isZero();
    }

    @Test
    void aReplaceChangingOneRowsPayload_updatesThatRowOnly() throws Exception {
        Fx f = fixture("payload");
        List<Map<String, Object>> rows = seedDoc(f, 5);
        var before = snapshot(f.docId());

        List<Map<String, Object>> next = new ArrayList<>(rows);
        Map<String, Object> changed = new LinkedHashMap<>(rows.get(2));
        changed.put("section_title", "renamed");
        changed.put("line_start", 7);
        changed.remove("page_number");                 // an absent label: NULL, as the insert wrote it
        changed.put("labels_written", false);
        next.set(2, changed);
        repo.writeManifest(TENANT, f.docId(), f.collection(), next);

        var after = snapshot(f.docId());
        for (int p : List.of(0, 1, 3, 4)) {
            assertThat(after.get(p)).as("row %d untouched", p).isEqualTo(before.get(p));
        }
        assertThat(after.get(2).get("tuple")).as("row 2 was updated").isNotEqualTo(before.get(2).get("tuple"));
        assertThat(after.get(2).get("section_title")).isEqualTo("renamed");
        assertThat(after.get(2).get("line_start")).isEqualTo(7);
        assertThat(after.get(2).get("page_number")).isNull();
        assertThat(after.get(2).get("labels_written")).isEqualTo(false);
        assertThat(after.get(2).get("line_end")).as("an unchanged column of the updated row").isEqualTo(29);
        assertThat(records(f.collection())).isZero();
    }

    @Test
    void aReplaceThatShrinksShiftsAndGrows_endsWithExactlyTheIncomingRows_andRecordsOnlyTheDroppedChunk() throws Exception {
        Fx f = fixture("shape");
        List<Map<String, Object>> seeded = seedDoc(f, 4);              // A B C D at 0..3
        String a = (String) seeded.get(0).get("chash"), b = (String) seeded.get(1).get("chash"),
               c = (String) seeded.get(2).get("chash"), d = (String) seeded.get(3).get("chash");
        String e = ch(f.docId() + "-new");
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(e, "new text")),
            List.of(doc(freshDoc("holder", f.collection()), List.of(row(0, e)))), null, false, false);

        // B C at 0,1 (shifted up, A dropped), D kept at 3 unchanged, E new at 5
        List<Map<String, Object>> next = List.of(rich(0, b), rich(1, c), rich(3, d), rich(5, e));
        repo.writeManifest(TENANT, f.docId(), f.collection(), next);

        var after = snapshot(f.docId());
        assertThat(after.keySet()).containsExactly(0, 1, 3, 5);
        for (Map<String, Object> want : next) {
            Map<String, Object> got = columns(after.get((Integer) want.get("position")));
            assertThat(got.get("chash")).isEqualTo(want.get("chash"));
            assertThat(got.get("chunk_index")).isEqualTo(want.get("chunk_index"));
            assertThat(got.get("line_start")).isEqualTo(want.get("line_start"));
            assertThat(got.get("section_title")).isEqualTo(want.get("section_title"));
            assertThat(got.get("collection")).isEqualTo(f.collection());
        }
        assertThat(hasRecord(f.collection(), a)).as("A lost its only owner").isTrue();
        for (String kept : List.of(b, c, d, e)) {
            assertThat(hasRecord(f.collection(), kept)).as("still listed: no record").isFalse();
        }
        assertThat(records(f.collection())).isEqualTo(1);
    }

    @Test
    void aDroppedRowWhoseChunkTheNewSetListsElsewhere_leavesNoRecord() throws Exception {
        Fx f = fixture("dupkey");
        String a = ch(f.docId() + "-a"), b = ch(f.docId() + "-b");
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(a, "a text"), chunk(b, "b text")),
            List.of(doc(f.docId(), List.of(rich(0, a), rich(1, b), rich(2, a)))), null, false, false);

        // position 2 (chunk a) goes; position 0 (chunk a) stays unchanged
        repo.writeManifest(TENANT, f.docId(), f.collection(), List.of(rich(0, a), rich(1, b)));

        assertThat(snapshot(f.docId()).keySet()).containsExactly(0, 1);
        assertThat(records(f.collection())).as("a is still listed at 0").isZero();
    }

    @Test
    void aReplaceToAnEmptyManifest_deletesEveryRow_andRecordsEveryChunk() throws Exception {
        Fx f = fixture("empty");
        seedDoc(f, 3);

        repo.writeManifest(TENANT, f.docId(), f.collection(), List.of());

        assertThat(snapshot(f.docId())).isEmpty();
        assertThat(records(f.collection())).isEqualTo(3);
    }

    /**
     * Whatever the sequence of replaces, the manifest ends as the last replace said, every column of every row,
     * and a chunk carries an orphan record exactly when no row of the document lists it (every chunk starts listed,
     * so a record is only ever written by a drop and only ever cleared by a listing). The pool is small, so a chunk
     * is often at two positions, one of them changed: the shape that leaves a record on an owned chunk unless the
     * clear covers every listed key.
     */
    @Test
    void randomReplaces_alwaysEndWithExactlyTheIncomingRows_andARecordExactlyForTheUnlistedChunks() throws Exception {
        for (long seed = 1; seed <= 25; seed++) {
            Fx f = fixture("rand" + seed);
            Random rnd = new Random(seed);
            List<String> pool = new ArrayList<>();
            List<Map<String, Object>> chunks = new ArrayList<>();
            List<Map<String, Object>> seedRows = new ArrayList<>();
            for (int i = 0; i < 4; i++) {
                String c = ch(f.docId() + "-p" + i);
                pool.add(c);
                chunks.add(chunk(c, "pool " + i));
                seedRows.add(rich(i, c));
            }
            svc.writeManyCombined(TENANT, f.collection(), chunks, List.of(doc(f.docId(), seedRows)), null, false, false);

            for (int round = 0; round < 12; round++) {
                List<Map<String, Object>> next = new ArrayList<>();
                for (int p = 0; p < 6; p++) {
                    if (rnd.nextInt(4) == 0) continue;                         // a gap: that position is absent
                    Map<String, Object> r = rich(p, pool.get(rnd.nextInt(pool.size())));
                    if (rnd.nextBoolean()) r.put("section_title", "t" + rnd.nextInt(3));
                    if (rnd.nextBoolean()) r.remove("line_start");
                    if (rnd.nextBoolean()) r.put("labels_written", false);
                    next.add(r);
                }
                repo.writeManifest(TENANT, f.docId(), f.collection(), next);

                String at = "seed " + seed + " round " + round;
                var after = snapshot(f.docId());
                assertThat(after.keySet()).as(at + " positions")
                    .containsExactlyElementsOf(next.stream().map(r -> (Integer) r.get("position")).toList());
                for (Map<String, Object> want : next) {
                    Map<String, Object> got = columns(after.get((Integer) want.get("position")));
                    assertThat(got.get("chash")).as(at + " chash").isEqualTo(want.get("chash"));
                    for (String col : List.of("chunk_index", "line_start", "line_end", "char_start", "char_end",
                            "section_title", "section_type", "page_number", "labels_written")) {
                        assertThat(got.get(col)).as(at + " " + col + " at " + want.get("position")).isEqualTo(want.get(col));
                    }
                    assertThat(got.get("collection")).isEqualTo(f.collection());
                }
                List<String> listed = next.stream().map(r -> (String) r.get("chash")).distinct().toList();
                for (String c : pool) {
                    assertThat(hasRecord(f.collection(), c)).as(at + ": record on " + c.substring(0, 6)
                        + " (listed=" + listed.contains(c) + ")").isEqualTo(!listed.contains(c));
                }
            }
        }
    }

    // ── the record's end state, the cases a statement-level clear can miss ──────────────────────────

    @Test
    void aKeyUpdatedAwayAtOnePositionButKeptAtAnother_leavesNoRecord() throws Exception {
        Fx f = fixture("xy");
        String x = ch(f.docId() + "-x"), y = ch(f.docId() + "-y");
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(x, "x text"), chunk(y, "y text")),
            List.of(doc(f.docId(), List.of(rich(0, x), rich(1, x), rich(2, y)))), null, false, false);

        // p=X and q=X; p goes to Y, q is unchanged: the UPDATE stamps X (it never reads the manifest), q still lists it
        repo.writeManifest(TENANT, f.docId(), f.collection(), List.of(rich(0, y), rich(1, x), rich(2, y)));

        assertThat(hasRecord(f.collection(), x)).as("X is still listed at position 1").isFalse();
        assertThat(records(f.collection())).isZero();
    }

    @Test
    void aStaleRecordAnotherDocumentLeftOnAChunkThisOneKeeps_isClearedByAnUnchangedReplace() throws Exception {
        Fx f = fixture("stale");
        String x = ch(f.docId() + "-x");
        String other = freshDoc("stale-b", f.collection());
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(x, "x text")),
            List.of(doc(f.docId(), List.of(rich(0, x))), doc(other, List.of(rich(0, x)))), null, false, false);

        // B drops X: the stamp records it although A still lists it
        repo.writeManifest(TENANT, other, f.collection(), List.of());
        assertThat(hasRecord(f.collection(), x)).as("precondition: the drop recorded X on an owned chunk").isTrue();

        // A re-indexes unchanged: the old path's INSERT cleared the record of every key it wrote
        repo.writeManifest(TENANT, f.docId(), f.collection(), List.of(rich(0, x)));

        assertThat(hasRecord(f.collection(), x)).as("A lists X: no record").isFalse();
    }

    /**
     * A chunk key lives in one collection and a collection has one embedding model, so the stamp's four-column key
     * and the clear's three-column key name the same chunk: there is no manifest row of one collection whose chunk
     * carries another model. The case that does move a row's model is a collection change, below.
     */
    @Test
    void aCollectionChange_rehomesTheRow_recordsTheOldCollectionsChunk_andClearsTheNewOnes() throws Exception {
        Fx f = fixture("coll");
        Fx g = fixture("coll2");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        List<Map<String, Object>> chunks = new ArrayList<>();
        for (Map<String, Object> r : rows) chunks.add(chunk((String) r.get("chash"), f.docId() + " text " + ((Integer) r.get("position"))));

        // the same document, same rows, now in the other collection (its chunks written there by the same call)
        svc.writeManyCombined(TENANT, g.collection(), chunks, List.of(doc(f.docId(), rows)), null, false, false);

        var after = snapshot(f.docId());
        assertThat(after).hasSize(3);
        after.values().forEach(r -> assertThat(r.get("collection")).isEqualTo(g.collection()));
        assertThat(records(g.collection())).as("listed in the new collection: no record").isZero();
        assertThat(records(f.collection())).as("left the old collection: all three recorded there").isEqualTo(3);
    }

    // ── the diff writes every payload column ─────────────────────────────────────────────────────

    /**
     * RDR-229 P3.1: collection_id is a payload column. A replace that keeps every other column of a row whose id the
     * stamp has not reached (name right, id NULL) still rewrites that row with the registry's id, and leaves a stamped
     * row alone.
     */
    @Test
    void aReplaceOverAnUnstampedManifestRow_stampsItsId_andLeavesStampedRowsUntouched() throws Exception {
        Fx f = fixture("unstamped");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        Long id = snapshotId(f.docId(), 1);
        assertThat(id).as("precondition: the seed stamped the id").isNotNull();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            P31CheckReplay.lift(ctx, P31CheckReplay.MANIFEST);
            ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, (Long) null)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(f.docId()))
                .and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(1)).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.MANIFEST);
        }
        assertThat(snapshotId(f.docId(), 1)).as("precondition: row 1 unstamped").isNull();
        var before = snapshot(f.docId());

        repo.writeManifest(TENANT, f.docId(), f.collection(), rows);

        var after = snapshot(f.docId());
        assertThat(after.get(1).get("collection_id")).as("the replace stamped the unstamped row").isEqualTo(id);
        assertThat(after.get(1).get("tuple")).as("by rewriting it").isNotEqualTo(before.get(1).get("tuple"));
        for (int p : List.of(0, 2)) assertThat(after.get(p)).as("stamped row %d untouched", p).isEqualTo(before.get(p));
    }

    private Long snapshotId(String docId, int position) throws Exception {
        Object v = snapshot(docId).get(position).get("collection_id");
        return v == null ? null : ((Number) v).longValue();
    }

    /** Each payload column, changed alone on one row: the diff must update it. {@code collection}, {@code embedding_model} and {@code collection_id} are the tests above. */
    @Test
    void everyPayloadColumnIsComparedAndWritten_andTheListMatchesTheTable() throws Exception {
        var columns = new java.util.TreeSet<String>();
        for (var field : CATALOG_DOCUMENT_CHUNKS.fields()) columns.add(field.getName());
        columns.removeAll(List.of("tenant_id", "doc_id", "position"));
        assertThat(dev.nexus.service.db.ManifestReplaceTestHooks.payloadColumns())
            .as("the table gained or lost a column ManifestReplace does not know").isEqualTo(columns);

        Fx f = fixture("cols");
        List<Map<String, Object>> rows = seedDoc(f, 4);
        String other = ch(f.docId() + "-other");
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(other, "other text")),
            List.of(doc(freshDoc("cols-holder", f.collection()), List.of(row(0, other)))), null, false, false);

        Map<String, java.util.function.Consumer<Map<String, Object>>> mutations = new LinkedHashMap<>();
        mutations.put("chash", r -> r.put("chash", other));
        mutations.put("chunk_index", r -> r.put("chunk_index", 99));
        mutations.put("line_start", r -> r.put("line_start", 1234));
        mutations.put("line_end", r -> r.put("line_end", 1235));
        mutations.put("char_start", r -> r.put("char_start", 5678));
        mutations.put("char_end", r -> r.put("char_end", 5679));
        mutations.put("section_title", r -> r.put("section_title", "other title"));
        mutations.put("section_type", r -> r.put("section_type", "other type"));
        mutations.put("page_number", r -> r.put("page_number", 77));
        mutations.put("labels_written", r -> r.put("labels_written", false));
        assertThat(mutations.keySet()).as("every payload column but the three a collection change moves")
            .containsExactlyInAnyOrderElementsOf(columns.stream()
                .filter(c -> !c.equals("collection") && !c.equals("embedding_model") && !c.equals("collection_id")).toList());

        for (var m : mutations.entrySet()) {
            repo.writeManifest(TENANT, f.docId(), f.collection(), rows);              // back to the baseline
            var before = snapshot(f.docId());
            List<Map<String, Object>> next = new ArrayList<>();
            for (Map<String, Object> r : rows) next.add(new LinkedHashMap<>(r));
            m.getValue().accept(next.get(1));
            repo.writeManifest(TENANT, f.docId(), f.collection(), next);

            var after = snapshot(f.docId());
            assertThat(after.get(1).get(m.getKey())).as("column %s written", m.getKey())
                .isEqualTo(m.getKey().equals("chash") ? other : next.get(1).get(m.getKey()));
            assertThat(after.get(1).get("tuple")).as("column %s: the row was updated", m.getKey())
                .isNotEqualTo(before.get(1).get("tuple"));
            for (int p : List.of(0, 2, 3)) assertThat(after.get(p)).as("column %s: row %d untouched", m.getKey(), p).isEqualTo(before.get(p));
        }
    }

    // ── the cap and the fallback ─────────────────────────────────────────────────────────────────

    @Test
    void theCapOnChangedRows_countsDeletedAndUpdated_andFallsBackOnePastIt() throws Exception {
        Fx f = fixture("cap");
        List<Map<String, Object>> rows = seedDoc(f, 8);
        dev.nexus.service.db.ManifestReplaceTestHooks.maxChangedRows(3);
        try {
            // 3 updated rows: at the cap, the diff runs and the other five rows keep their tuples
            var before = snapshot(f.docId());
            List<Map<String, Object>> three = new ArrayList<>(rows);
            for (int p : List.of(0, 1, 2)) three.set(p, withTitle(rows.get(p), "changed"));
            repo.writeManifest(TENANT, f.docId(), f.collection(), three);
            var afterThree = snapshot(f.docId());
            for (int p = 3; p < 8; p++) assertThat(afterThree.get(p)).as("row %d kept at the cap", p).isEqualTo(before.get(p));

            // 4 updated rows: one past the cap, the delete-and-insert runs and every row is a new tuple
            List<Map<String, Object>> four = new ArrayList<>(three);
            for (int p : List.of(0, 1, 2, 3)) four.set(p, withTitle(rows.get(p), "changed again"));
            repo.writeManifest(TENANT, f.docId(), f.collection(), four);
            var afterFour = snapshot(f.docId());
            for (int p = 4; p < 8; p++) {
                assertThat(afterFour.get(p).get("tuple")).as("row %d rewritten past the cap", p).isNotEqualTo(afterThree.get(p).get("tuple"));
                assertThat(columns(afterFour.get(p))).isEqualTo(columns(afterThree.get(p)));
            }
            assertThat(afterFour.get(0).get("section_title")).isEqualTo("changed again");

            // 2 deleted and 2 updated also count 4
            List<Map<String, Object>> mixed = new ArrayList<>();
            for (int p = 0; p < 6; p++) mixed.add(p < 2 ? withTitle(four.get(p), "mixed") : four.get(p));
            repo.writeManifest(TENANT, f.docId(), f.collection(), mixed);
            var afterMixed = snapshot(f.docId());
            assertThat(afterMixed.keySet()).containsExactly(0, 1, 2, 3, 4, 5);
            assertThat(afterMixed.get(5).get("tuple")).as("deleted plus updated is 4: past the cap").isNotEqualTo(afterFour.get(5).get("tuple"));
        } finally {
            dev.nexus.service.db.ManifestReplaceTestHooks.maxChangedRows(0);
        }
    }

    private static Map<String, Object> withTitle(Map<String, Object> row, String title) {
        Map<String, Object> r = new LinkedHashMap<>(row);
        r.put("section_title", title);
        return r;
    }

    /** What a replace of {@code rows} raises, run by the diff and by the delete-and-insert; the manifest must be as it was after each. */
    private void sameFailureOnBothPaths(Fx f, List<Map<String, Object>> rows) throws Exception {
        var before = snapshot(f.docId());
        Throwable[] raised = new Throwable[2];
        for (int k = 0; k < 2; k++) {
            dev.nexus.service.db.ManifestReplaceTestHooks.decline(k == 1);
            try {
                repo.writeManifest(TENANT, f.docId(), f.collection(), rows);
            } catch (Throwable t) {
                raised[k] = t;
            } finally {
                dev.nexus.service.db.ManifestReplaceTestHooks.decline(false);
            }
            assertThat(snapshot(f.docId())).as("the failed replace rolled back (path %d)", k).isEqualTo(before);
        }
        assertThat(raised[0]).as("the diff path fails").isNotNull();
        assertThat(raised[1]).as("the delete-and-insert fails").isNotNull();
        assertThat(raised[0].getClass()).isEqualTo(raised[1].getClass());
        assertThat(String.valueOf(raised[0].getMessage()).split("\n")[0])
            .isEqualTo(String.valueOf(raised[1].getMessage()).split("\n")[0]);
    }

    @Test
    void aDuplicatePosition_failsTheSameWayAsTheDeleteAndInsert() throws Exception {
        Fx f = fixture("dup");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        sameFailureOnBothPaths(f, List.of(rich(0, (String) rows.get(0).get("chash")), rich(0, (String) rows.get(1).get("chash"))));
        dev.nexus.service.db.ManifestReplaceTestHooks.decline(true);
        try {
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.writeManifest(TENANT, f.docId(), f.collection(),
                List.of(rich(0, (String) rows.get(0).get("chash")), rich(0, (String) rows.get(1).get("chash")))))
                .isInstanceOf(org.jooq.exception.IntegrityConstraintViolationException.class)
                .hasMessageContaining("catalog_document_chunks_pk");
        } finally {
            dev.nexus.service.db.ManifestReplaceTestHooks.decline(false);
        }
    }

    @Test
    void aBlankChash_failsTheSameWayAsTheDeleteAndInsert() throws Exception {
        Fx f = fixture("blank");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        List<Map<String, Object>> bad = new ArrayList<>(rows);
        bad.set(1, rich(1, ""));
        sameFailureOnBothPaths(f, bad);
    }

    @Test
    void aMissingPosition_failsTheSameWayAsTheDeleteAndInsert() throws Exception {
        Fx f = fixture("nopos");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        Map<String, Object> noPosition = new LinkedHashMap<>(rows.get(1));
        noPosition.remove("position");
        noPosition.remove("chunk_index");
        List<Map<String, Object>> bad = new ArrayList<>(rows);
        bad.set(1, noPosition);
        sameFailureOnBothPaths(f, bad);
    }

    @Test
    void aSupersededCollection_refusesAReplaceThatKeepsEveryRow() throws Exception {
        Fx f = fixture("sup");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        assertThat(SupersedeSeeds.supersede(repo, TENANT, f.collection(), f.collection().replace("__v1", "__v2"), "")).isEqualTo(1);
        var before = snapshot(f.docId());

        for (boolean decline : List.of(false, true)) {
            dev.nexus.service.db.ManifestReplaceTestHooks.decline(decline);
            try {
                org.assertj.core.api.Assertions.assertThatThrownBy(
                    () -> repo.writeManifest(TENANT, f.docId(), f.collection(), rows))
                    .isInstanceOf(dev.nexus.service.db.CollectionSupersededException.class);
            } finally {
                dev.nexus.service.db.ManifestReplaceTestHooks.decline(false);
            }
        }
        assertThat(snapshot(f.docId())).isEqualTo(before);
    }
}
