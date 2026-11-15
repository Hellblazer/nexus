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
    void aDuplicatePosition_stillFailsTheWholeReplace_andLeavesTheManifestAsItWas() throws Exception {
        Fx f = fixture("dup");
        List<Map<String, Object>> rows = seedDoc(f, 3);
        var before = snapshot(f.docId());

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.writeManifest(TENANT, f.docId(), f.collection(),
            List.of(rich(0, (String) rows.get(0).get("chash")), rich(0, (String) rows.get(1).get("chash")))))
            .isInstanceOf(RuntimeException.class);

        assertThat(snapshot(f.docId())).as("the transaction rolled back").isEqualTo(before);
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
     * Whatever the sequence of replaces, the manifest ends as the last replace said: every column of every
     * row equal to the incoming row, and a record exactly for the chunks that are listed by no row of the
     * collection's documents.
     */
    @Test
    void randomReplaces_alwaysEndWithExactlyTheIncomingRows() throws Exception {
        Fx f = fixture("rand");
        Random rnd = new Random(29);
        List<String> pool = new ArrayList<>();
        List<Map<String, Object>> chunks = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            String c = ch(f.docId() + "-p" + i);
            pool.add(c);
            chunks.add(chunk(c, "pool " + i));
        }
        List<Map<String, Object>> seedRows = new ArrayList<>();
        for (int i = 0; i < pool.size(); i++) seedRows.add(rich(i, pool.get(i)));
        svc.writeManyCombined(TENANT, f.collection(), chunks, List.of(doc(f.docId(), seedRows)), null, false, false);

        for (int round = 0; round < 40; round++) {
            List<Map<String, Object>> next = new ArrayList<>();
            for (int p = 0; p < 7; p++) {
                if (rnd.nextInt(3) == 0) continue;                         // a gap: that position is absent
                Map<String, Object> r = rich(p, pool.get(rnd.nextInt(pool.size())));
                if (rnd.nextBoolean()) r.put("section_title", "t" + rnd.nextInt(3));
                if (rnd.nextBoolean()) r.remove("line_start");
                if (rnd.nextBoolean()) r.put("labels_written", false);
                next.add(r);
            }
            repo.writeManifest(TENANT, f.docId(), f.collection(), next);

            var after = snapshot(f.docId());
            assertThat(after.keySet()).as("round %d positions", round)
                .containsExactlyElementsOf(next.stream().map(r -> (Integer) r.get("position")).toList());
            for (Map<String, Object> want : next) {
                Map<String, Object> got = columns(after.get((Integer) want.get("position")));
                assertThat(got.get("chash")).as("round %d chash", round).isEqualTo(want.get("chash"));
                for (String col : List.of("chunk_index", "line_start", "line_end", "char_start", "char_end",
                        "section_title", "section_type", "page_number", "labels_written")) {
                    assertThat(got.get(col)).as("round %d %s at %s", round, col, want.get("position"))
                        .isEqualTo(want.get(col));
                }
                assertThat(got.get("collection")).isEqualTo(f.collection());
            }
            List<String> listed = next.stream().map(r -> (String) r.get("chash")).distinct().toList();
            for (String c : pool) {
                if (listed.contains(c)) assertThat(hasRecord(f.collection(), c)).as("round %d listed %s", round, c.substring(0, 6)).isFalse();
            }
        }
    }
}
