// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd round r1: the guards of {@code GET /v1/catalog/manifest/fillable} that {@link ManifestFillableRouteTest}
 * left without a test that fails when the guard is removed (each was mutated and shown red, see the bead), the int4
 * bound of the recorded values, and the {@code edge} count a client reads beside {@code fillable}. Every case sits
 * beside a control row that IS fillable, so a count of zero cannot come from a fixture that fills nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillableGuardTest extends ManifestFillableTestBase {

    private static final String COLL_2 = "knowledge__f3yqd-fill-second__v1";
    private static final String SPAN_KEYS = "\"line_start\":4,\"line_end\":9,\"chunk_start_char\":10,\"chunk_end_char\":40";

    // ── span_disagrees, one test per column ─────────────────────────────────

    /**
     * The chunk records every span column; the manifest row holds NULL in line_start (so it is fillable) and a value in
     * ONE other column. When that value equals the chunk's the row counts; when it differs the row is another
     * position's and counts for nothing. Only the disagreeing column differs, so only its disagree term can refuse.
     */
    private void assertAColumnThatDisagreesRefusesTheRow(String column, Integer[] agree, Integer[] clash) throws Exception {
        String agreeDoc = doc("agrees on " + column), clashDoc = doc("disagrees on " + column);
        chunk(TENANT, h(0x41), labelsOnly(OK_AT, SPAN_KEYS));
        chunk(TENANT, h(0x42), labelsOnly(OK_AT, SPAN_KEYS));
        manifest(agreeDoc, h(0x41));
        manifest(clashDoc, h(0x42));
        spans(agreeDoc, 0, agree[0], agree[1], agree[2], agree[3]);
        spans(clashDoc, 0, clash[0], clash[1], clash[2], clash[3]);
        assertThat(fillable("spans")).as(column + ": the agreeing row counts, the clashing one does not").isEqualTo(1);
    }

    @Test
    void aLineEndTheChunkContradictsRefusesTheRow() throws Exception {
        assertAColumnThatDisagreesRefusesTheRow("line_end",
            new Integer[] {null, 9, null, null}, new Integer[] {null, 8, null, null});
    }

    @Test
    void aCharStartTheChunkContradictsRefusesTheRow() throws Exception {
        assertAColumnThatDisagreesRefusesTheRow("char_start",
            new Integer[] {null, null, 10, null}, new Integer[] {null, null, 11, null});
    }

    @Test
    void aCharEndTheChunkContradictsRefusesTheRow() throws Exception {
        assertAColumnThatDisagreesRefusesTheRow("char_end",
            new Integer[] {null, null, null, 40}, new Integer[] {null, null, null, 41});
    }

    // ── title rule 2: a path with a per-chunk suffix, named by its file name ─

    @Test
    void aChunkTitledWithAPathAndASuffixNamesTheDocumentByItsFileName() throws Exception {
        // chunk title x/y/c.md:12-30 -> without the suffix x/y/c.md -> file name c.md, which is the document's TITLE.
        // Neither the title nor the unsuffixed title equals "c.md", so only the file-name candidate can match.
        String yes = doc(TOKEN, TENANT, "c.md", "", DOC_AT, "{}");
        String no = doc(TOKEN, TENANT, "c.md", "", DOC_AT, "{}");
        chunk(TENANT, h(0x43), both(OK_AT, "x/y/c.md:12-30"));
        chunk(TENANT, h(0x44), both(OK_AT, "x/y/d.md:12-30"));
        manifest(yes, h(0x43));
        manifest(no, h(0x44));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind + ": c.md counts, d.md does not").isEqualTo(1);
    }

    // ── the join to chunks ──────────────────────────────────────────────────

    @Test
    void theSameChashInTwoCollectionsIsOneRowNotTwo() throws Exception {
        // A chunk of the same text exists in a second collection, stamped and titled the same. The manifest row names
        // its own collection, so only that chunk row is its own: joining on the chash alone would count it twice.
        String d = doc("two collections");
        chunk(TENANT, h(0x45), both(OK_AT));
        chunkIn(TENANT, COLL_2, h(0x45), both(OK_AT));
        manifest(d, h(0x45));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind).isEqualTo(1);
    }

    // ── the skew boundary ───────────────────────────────────────────────────

    @Test
    void aChunkExactlyFiveMinutesAfterTheDocumentIsCounted_andAMillisecondLaterIsNot() throws Exception {
        String on = doc("on the boundary"), over = doc("a moment over");
        chunk(TENANT, h(0x46), both("2026-10-01T12:05:00+00:00"));
        chunk(TENANT, h(0x47), both("2026-10-01T12:05:00.001+00:00"));
        manifest(on, h(0x46));
        manifest(over, h(0x47));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind + ": 300 s is inside, 300.001 s is not").isEqualTo(1);
    }

    // ── a negative start beside a recorded end ──────────────────────────────

    @Test
    void aNegativeCharStartIsRecordedBesideARecordedEnd_andOnlyThen() throws Exception {
        // The manifest row already holds char_end 40 (the chunk's own), so the only thing left to fill is char_start:
        // the row is fillable exactly when the chunk's -5 counts as a recorded start.
        String beside = doc("negative start, end recorded"), alone = doc("negative start, no end");
        String endZero = doc("negative start, end zero");
        chunk(TENANT, h(0x48), labelsOnly(OK_AT, "\"chunk_start_char\":-5,\"chunk_end_char\":40"));
        chunk(TENANT, h(0x49), labelsOnly(OK_AT, "\"chunk_start_char\":-5"));
        chunk(TENANT, h(0x4a), labelsOnly(OK_AT, "\"chunk_start_char\":-5,\"chunk_end_char\":0"));
        manifest(beside, h(0x48));
        manifest(alone, h(0x49));
        manifest(endZero, h(0x4a));
        spans(beside, 0, null, null, null, 40);
        spans(alone, 0, null, null, null, 40);
        spans(endZero, 0, null, null, null, 40);
        assertThat(fillable("spans")).as("only the start beside a recorded end").isEqualTo(1);
    }

    // ── the int4 bound: 10-digit values that fit are recorded, the first that does not is not ─────────────────

    @Test
    void aSpanOrPageValueUpToInt4MaxIsCounted_andOneAboveIsNot() throws Exception {
        String[] counted = {
            "\"line_start\":1000000000", "\"line_start\":2147483647", "\"line_end\":2147483647",
            "\"chunk_start_char\":2147483647", "\"chunk_end_char\":2147483647",
            "\"chunk_start_char\":-2147483648,\"chunk_end_char\":5"};
        String[] refused = {
            "\"line_start\":2147483648", "\"line_end\":2147483648", "\"chunk_start_char\":2147483648",
            "\"chunk_end_char\":2147483648", "\"line_start\":9999999999",
            "\"chunk_start_char\":-2147483649,\"chunk_end_char\":2147483648"};
        for (int i = 0; i < counted.length; i++) {
            String d = doc("counted " + i);
            chunk(TENANT, h(0x50 + i), labelsOnly(OK_AT, counted[i]));
            manifest(d, h(0x50 + i));
        }
        for (int i = 0; i < refused.length; i++) {
            String d = doc("refused " + i);
            chunk(TENANT, h(0x60 + i), labelsOnly(OK_AT, refused[i]));
            manifest(d, h(0x60 + i));
        }
        assertThat(fillable("spans")).as("a value a Python fill writes (it fits INTEGER) is counted; above int4 is not")
            .isEqualTo(counted.length);
    }

    @Test
    void aPageNumberUpToInt4MaxIsCounted_andOneAboveIsNot() throws Exception {
        String[] counted = {"2147483647", "1000000000", "-2147483648"};
        String[] refused = {"2147483648", "9999999999", "-2147483649"};
        for (int i = 0; i < counted.length; i++) {
            String d = doc("page counted " + i);
            chunk(TENANT, h(0x70 + i), labelsOnly(OK_AT, "\"page_number\":" + counted[i]));
            manifest(d, h(0x70 + i));
        }
        for (int i = 0; i < refused.length; i++) {
            String d = doc("page refused " + i);
            chunk(TENANT, h(0x78 + i), labelsOnly(OK_AT, "\"page_number\":" + refused[i]));
            manifest(d, h(0x78 + i));
        }
        assertThat(fillable("labels")).isEqualTo(counted.length);
        // Not counted, but not decidable in SQL either: the client would send it and the write would fail, so it is an
        // edge row (scan once and see), never a silently converged one.
        assertThat(edge("labels")).as("an out-of-range page is an edge row").isEqualTo(refused.length);
    }

    // ── edge: rows that pass every guard but one the SQL form cannot decide ─────────────────────────────────

    private int edge(String kind) throws Exception {
        return ((Number) fillable(kind, 1000).get("edge")).intValue();
    }

    @Test
    void aStampTheStrictFormRejectsIsAnEdgeRow_notFillable() throws Exception {
        // Python's fromisoformat reads a date-only stamp and the strict SQL form does not: the fill might write it.
        String[] stamps = {"2026-10-01", "2026-10-01T11", "20261001T115900"};
        for (int i = 0; i < stamps.length; i++) {
            String d = doc("edge stamp " + i);
            chunk(TENANT, h(0x80 + i), both(stamps[i]));
            manifest(d, h(0x80 + i));
        }
        for (String kind : KINDS) {
            assertThat(fillable(kind)).as(kind + ": not counted").isEqualTo(0);
            assertThat(edge(kind)).as(kind + ": but a client must scan once").isEqualTo(stamps.length);
        }
    }

    @Test
    void aLabelValueTheSqlFormCannotDecideIsAnEdgeRowForLabelsOnly() throws Exception {
        // 0, false, [] and {} are falsy non-strings Python turns into "" and fills; 7 and a string page are sent as found.
        String[] odd = {"\"section_title\":0", "\"section_title\":false", "\"section_title\":[]", "\"section_title\":{}",
            "\"section_type\":7", "\"page_number\":\"3\"", "\"page_number\":3.5"};
        for (int i = 0; i < odd.length; i++) {
            String d = doc("edge label " + i);
            chunk(TENANT, h(0x90 + i), labelsOnly(OK_AT, odd[i]));
            manifest(d, h(0x90 + i));
        }
        assertThat(fillable("labels")).isEqualTo(0);
        assertThat(edge("labels")).isEqualTo(odd.length);
        assertThat(edge("spans")).as("a label value is no edge for the span rung").isEqualTo(0);
    }

    @Test
    void anEdgeRowStillHasToPassEveryOtherGuard() throws Exception {
        // Each row below carries an edge value AND fails a guard that is not an edge: it is not "might be filled".
        String twoHolders1 = doc("held twice 1"), twoHolders2 = doc("held twice 2");
        chunk(TENANT, h(0xa0), both("2026-10-01"));
        manifest(twoHolders1, h(0xa0));
        manifest(twoHolders2, h(0xa0));
        String late = doc("late and odd");
        chunk(TENANT, h(0xa1), labelsOnly("2026-10-01T12:30:00+00:00", "\"section_title\":0,\"line_start\":5"));
        manifest(late, h(0xa1));
        String titled = doc(TOKEN, TENANT, "mine", "docs/mine.md", DOC_AT, "{}");
        chunk(TENANT, h(0xa2), both("2026-10-01", "docs/theirs.md"));
        manifest(titled, h(0xa2));
        String unstamped = doc("no stamp at all");
        chunk(TENANT, h(0xa3), "{\"section_title\":0,\"line_start\":5}");
        manifest(unstamped, h(0xa3));
        String written = doc("labels done");
        chunk(TENANT, h(0xa4), both("2026-10-01"));
        manifest(written, h(0xa4));
        var resp = post(TOKEN, TENANT, "/v1/catalog/manifest/write", mapper.writeValueAsString(Map.of(
            "doc_id", written, "collection", COLL,
            "rows", List.of(Map.of("position", 0, "chash", h(0xa4), "section_title", "Mine", "section_type", "")))));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        restamp(TENANT, written);
        for (String kind : KINDS) assertThat(edge(kind)).as(kind + ", before the control").isEqualTo(kind.equals("spans") ? 1 : 0);
        // the one spans edge above is the already-written row: labels are done, its spans still might be filled
        String control = doc("edge control");
        chunk(TENANT, h(0xa5), both("2026-10-01"));
        manifest(control, h(0xa5));
        assertThat(edge("labels")).as("the control is the only labels edge").isEqualTo(1);
        assertThat(edge("spans")).as("the control and the written row").isEqualTo(2);
    }

    @Test
    void aFillableRowIsNotAnEdgeRow_andAConvergedTenantHasNeither() throws Exception {
        String d = doc("plain");
        chunk(TENANT, h(0xb0), both(OK_AT));
        manifest(d, h(0xb0));
        for (String kind : KINDS) {
            assertThat(fillable(kind, 1000)).containsEntry("fillable", 1).containsEntry("edge", 0);
        }
        var resp = post(TOKEN, TENANT, "/v1/catalog/manifest/write", mapper.writeValueAsString(Map.of(
            "doc_id", d, "collection", COLL,
            "rows", List.of(Map.of("position", 0, "chash", h(0xb0), "section_title", "Intro", "section_type", "")))));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        spans(d, 0, 5, 9, 0, 40);
        for (String kind : KINDS) {
            assertThat(fillable(kind, 1000)).as(kind + " once written").containsEntry("fillable", 0).containsEntry("edge", 0);
        }
    }

    /**
     * The LIMIT applies to the relaxed candidate rows, so the edge is bounded with the rest, and a sample that is all edge
     * rows is CAPPED with fillable 0 and edge == limit: a client must read that as pending, since strict rows may lie
     * beyond the sample. Only an uncapped answer is exact.
     */
    @Test
    void aCappedSampleOfOnlyEdgeRowsIsCappedWithFillableZero_theClientReadsItAsPending() throws Exception {
        for (int i = 0; i < 3; i++) {
            String d = doc("edge " + i);
            chunk(TENANT, h(0xc0 + i), both("2026-10-01"));
            manifest(d, h(0xc0 + i));
        }
        for (String kind : KINDS) {
            assertThat(fillable(kind, 2)).as(kind + ": limit 2").containsEntry("fillable", 0).containsEntry("edge", 2)
                .containsEntry("capped", true);
            assertThat(fillable(kind, 3)).as(kind + ": limit 3 is reached").containsEntry("edge", 3).containsEntry("capped", true);
            assertThat(fillable(kind, 4)).as(kind + ": limit 4 is not").containsEntry("fillable", 0).containsEntry("edge", 3)
                .containsEntry("capped", false);
        }
    }

    /** A pending backlog stops at the limit: 5 fillable rows, limit 2 -> fillable 2, capped, nothing else counted. */
    @Test
    void aCappedPendingTenantStopsAtTheLimit() throws Exception {
        for (int i = 0; i < 5; i++) {
            String d = doc("pending " + i);
            chunk(TENANT, h(0xd0 + i), both(OK_AT));
            manifest(d, h(0xd0 + i));
        }
        for (String kind : KINDS) {
            assertThat(fillable(kind, 2)).as(kind).containsEntry("fillable", 2).containsEntry("edge", 0)
                .containsEntry("limit", 2).containsEntry("capped", true);
            assertThat(fillable(kind, 6)).as(kind + ": below the limit it is exact").containsEntry("fillable", 5)
                .containsEntry("capped", false);
        }
    }
}
