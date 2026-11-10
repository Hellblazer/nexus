// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/manifest/fillable?kind=labels|spans&limit=N} counts, strictly and up to a
 * bound, the manifest rows the two upgrade rungs (manifest section labels, manifest spans) would fill. Each test
 * builds the one situation a guard of the rungs' scans exists for, beside a control row that IS fillable in the same
 * fixture, so a count of zero cannot come from a fixture that fills nothing. The guards both rungs share are run for
 * BOTH kinds from one chunk that carries a label key and a span key; the guards of one kind are run for that kind.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillableRouteTest extends ManifestFillableTestBase {

    // ── the envelope ────────────────────────────────────────────────────────

    @Test
    void theAnswerNamesItsKindRuleAndStampForm() throws Exception {
        String d = doc("plain doc");
        chunk(TENANT, h(1), both(OK_AT));
        manifest(d, h(1));
        for (String kind : KINDS) {
            var body = fillable(kind, 1000);
            assertThat(body).containsEntry("kind", kind).containsEntry("fillable", 1).containsEntry("limit", 1000)
                .containsEntry("capped", false).containsEntry("rule", "title rule 2").containsEntry("stamp", "strict");
            assertThat(body.keySet()).containsExactlyInAnyOrder("kind", "fillable", "limit", "capped", "rule", "stamp", "edge");
        }
    }

    @Test
    void anEmptyTenantCountsZero() throws Exception {
        for (String kind : KINDS) {
            assertThat(fillable(kind, 1000)).containsEntry("fillable", 0).containsEntry("capped", false);
        }
    }

    // ── guards both kinds share ─────────────────────────────────────────────

    @Test
    void aChashHeldByTwoLiveDocumentsIsNotCounted() throws Exception {
        String a = doc("shared a"), b = doc("shared b");
        chunk(TENANT, h(5), both(OK_AT));
        manifest(a, h(5));
        manifest(b, h(5));
        assertNotCountedByEitherKind("two live holders");
    }

    @Test
    void aChashRepeatedInsideOneDocumentIsNotCounted() throws Exception {
        String a = doc("repeated");
        chunk(TENANT, h(6), both(OK_AT));
        manifest(a, h(6), h(6));
        assertNotCountedByEitherKind("repeated in one document");
    }

    @Test
    void aChashAlsoHeldByATombstonedDocumentIsNotCounted() throws Exception {
        String live = doc("co-owner live"), gone = doc("co-owner gone");
        chunk(TENANT, h(7), both(OK_AT));
        manifest(live, h(7));
        manifest(gone, h(7));
        tombstone(gone);
        assertNotCountedByEitherKind("tombstoned co-owner");
    }

    @Test
    void aTombstonedDocumentsOwnRowIsNotCounted() throws Exception {
        String gone = doc("only gone");
        chunk(TENANT, h(8), both(OK_AT));
        manifest(gone, h(8));
        tombstone(gone);
        assertNotCountedByEitherKind("tombstoned own document");
    }

    @Test
    void aDocumentTheOrphanBackfillMintedIsNotCounted_butAnotherMarkerIs() throws Exception {
        String minted = doc(TOKEN, TENANT, "minted", "", DOC_AT, "{\"backfill_from\":\"t3_orphan\"}");
        chunk(TENANT, h(9), both(OK_AT));
        manifest(minted, h(9));
        assertNotCountedByEitherKind("orphan-minted");
        String other = doc(TOKEN, TENANT, "other marker", "", DOC_AT, "{\"backfill_from\":\"something_else\"}");
        chunk(TENANT, h(10), both(OK_AT));
        manifest(other, h(10));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind).isEqualTo(2);
    }

    @Test
    void anAliasDocumentIsNotCounted() throws Exception {
        String d = doc("alias doc");
        chunk(TENANT, h(27), both(OK_AT));
        manifest(d, h(27));
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.ALIAS_OF, "1.1.1")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(d)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
        assertNotCountedByEitherKind("alias");
    }

    // ── the attribution guard: the STRICT stamp ─────────────────────────────

    @Test
    void aChunkWrittenMoreThanFiveMinutesAfterTheDocumentIsNotCounted_andInsideTheSkewIs() throws Exception {
        String late = doc("late chunk"), inside = doc("skewed chunk");
        chunk(TENANT, h(11), both("2026-10-01T12:05:01+00:00"));
        chunk(TENANT, h(12), both("2026-10-01T12:04:59+00:00"));
        manifest(late, h(11));
        manifest(inside, h(12));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind + ": only the one inside the skew").isEqualTo(1);
    }

    @Test
    void theStampIsComparedAsAnInstantWhateverItsOffset() throws Exception {
        // 13:30+05:00 is 08:30 UTC, before the document's 12:00 UTC; read as 13:30 UTC it would be late.
        String d = doc("offset chunk");
        chunk(TENANT, h(13), both("2026-10-01T13:30:00+05:00"));
        manifest(d, h(13));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind).isEqualTo(1);
    }

    @Test
    void aStampWithNoOffsetIsReadAsUtc() throws Exception {
        String early = doc("naive early"), late = doc("naive late");
        chunk(TENANT, h(14), both("2026-10-01T11:00:00.123456"));
        chunk(TENANT, h(15), both("2026-10-01T12:10:00"));
        manifest(early, h(14));
        manifest(late, h(15));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind + ": 12:10 UTC is past the skew").isEqualTo(1);
    }

    @Test
    void aChunkWithNoIndexedAtIsNotCounted() throws Exception {
        String d = doc("unstamped chunk");
        chunk(TENANT, h(16), "{\"section_title\":\"Intro\",\"line_start\":5}");
        manifest(d, h(16));
        assertNotCountedByEitherKind("chunk without indexed_at");
    }

    @Test
    void aDocumentWithNoIndexedAtIsNotCounted() throws Exception {
        String d = doc(TOKEN, TENANT, "unstamped doc", "", null, "{}");
        chunk(TENANT, h(17), both(OK_AT));
        manifest(d, h(17));
        assertNotCountedByEitherKind("document without indexed_at");
    }

    /**
     * THE BEHAVIOUR POINT of the strict form: the live count (section_label_fillable) COUNTS a chunk stamp it cannot
     * read, because a count above the fill is harmless there; a count that must equal a fill must not. A date-only
     * stamp and impossible dates are 'cannot read': counted by the old route, not by this one, and none of them fails
     * the route for the tenant or hides the control beside them.
     */
    @Test
    void aStampTheEngineCannotReadIsNotCounted_whereTheLiveCountCountsIt() throws Exception {
        String[] stamps = {"2026-10-01", "2026-13-01T00:00:00+00:00", "2026-10-01T99:00:00", "2026-02-30 12:00:00Z"};
        for (int i = 0; i < stamps.length; i++) {
            String d = doc("unreadable " + i);
            chunk(TENANT, h(60 + i), both(stamps[i]));
            manifest(d, h(60 + i));
        }
        String control = doc("control beside unreadable stamps");
        chunk(TENANT, h(0xc1), both(OK_AT));
        manifest(control, h(0xc1));
        assertThat(oldLiveCount()).as("the live count counts every one of them, and the control").isEqualTo(5);
        for (String kind : KINDS) {
            assertThat(fillable(kind)).as(kind + ": only the control").isEqualTo(1);
        }
    }

    // ── the title guard ─────────────────────────────────────────────────────

    @Test
    void aChunkTitledForAnotherFileIsNotCounted() throws Exception {
        String d = doc(TOKEN, TENANT, "mine", "docs/mine.md", DOC_AT, "{}");
        chunk(TENANT, h(19), both(OK_AT, "docs/theirs.md:chunk-3"));
        manifest(d, h(19));
        assertNotCountedByEitherKind("chunk titled for another file");
    }

    @Test
    void aChunkTitleNamingTheDocumentIsCounted_whicheverNameAndSuffix() throws Exception {
        String byTitle = doc(TOKEN, TENANT, "My Title", "docs/a.md", DOC_AT, "{}");
        String byPath = doc(TOKEN, TENANT, "Other Title", "docs/b.md", DOC_AT, "{}");
        String byName = doc(TOKEN, TENANT, "Third Title", "docs/c.md", DOC_AT, "{}");
        String bySuffix = doc(TOKEN, TENANT, "Fourth Title", "docs/d.py", DOC_AT, "{}");
        String byPage = doc(TOKEN, TENANT, "Fifth Title", "docs/e.pdf", DOC_AT, "{}");
        String afterChunk = doc(TOKEN, TENANT, "papers/x.pdf:page-3", "", DOC_AT, "{}");
        String untitled = doc(TOKEN, TENANT, "any", "docs/any.md", DOC_AT, "{}");
        chunk(TENANT, h(20), both(OK_AT, "My Title"));
        chunk(TENANT, h(21), both(OK_AT, "docs/b.md"));
        chunk(TENANT, h(22), both(OK_AT, "c.md"));
        chunk(TENANT, h(23), both(OK_AT, "docs/d.py:12-30"));
        chunk(TENANT, h(24), both(OK_AT, "Fifth Title:page-2"));
        chunk(TENANT, h(25), both(OK_AT, "papers/x.pdf:page-11"));   // nexus-l8o33: a document titled after one of its chunks
        chunk(TENANT, h(26), both(OK_AT));                            // no title: not contradicted
        manifest(byTitle, h(20));
        manifest(byPath, h(21));
        manifest(byName, h(22));
        manifest(bySuffix, h(23));
        manifest(byPage, h(24));
        manifest(afterChunk, h(25));
        manifest(untitled, h(26));
        for (String kind : KINDS) assertThat(fillable(kind)).as(kind).isEqualTo(7);
    }

    @Test
    void aPageSuffixOnBothSidesDoesNotMakeTwoFilesOne_andAnEmptiedNameMatchesNothing() throws Exception {
        String d = doc(TOKEN, TENANT, "papers/a.pdf:page-2", "", DOC_AT, "{}");
        chunk(TENANT, h(91), both(OK_AT, "papers/b.pdf:page-6"));
        manifest(d, h(91));
        String e = doc(TOKEN, TENANT, ":page-3", "", DOC_AT, "{}");
        chunk(TENANT, h(92), both(OK_AT, ":chunk-3"));
        manifest(e, h(92));
        assertNotCountedByEitherKind("title rule 2 refusals");
    }

    // ── labels only ─────────────────────────────────────────────────────────

    @Test
    void aRowThatAlreadyHasItsLabelsIsNotCountedForLabels_butStillForSpans() throws Exception {
        String d = doc("written doc");
        chunk(TENANT, h(2), both(OK_AT));
        var resp = post(TOKEN, TENANT, "/v1/catalog/manifest/write", mapper.writeValueAsString(Map.of(
            "doc_id", d, "collection", COLL,
            "rows", List.of(Map.of("position", 0, "chash", h(2), "section_title", "Mine", "section_type", "")))));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        restamp(TENANT, d);
        assertThat(fillable("labels")).isEqualTo(0);
        assertThat(fillable("spans")).as("its spans are still NULL and recorded").isEqualTo(1);
        assertThat(beside("labels")).isEqualTo(0);
    }

    @Test
    void aChunkWithNoLabelKeyIsNotCountedForLabels_butStillForSpans() throws Exception {
        String d = doc("span-only chunk");
        chunk(TENANT, h(3), labelsOnly(OK_AT, "\"line_start\":5"));
        manifest(d, h(3));
        assertThat(fillable("labels")).isEqualTo(0);
        assertThat(fillable("spans")).isEqualTo(1);
        String e = doc("no-metadata chunk");
        chunk(TENANT, h(4), "{}");
        manifest(e, h(4));
        assertThat(fillable("labels")).isEqualTo(0);
        assertThat(fillable("spans")).isEqualTo(1);
    }

    @Test
    void aLabelKeyHoldingJsonNullOrAnEmptyStringIsStillAKey_andIsCounted() throws Exception {
        String a = doc("null label"), b = doc("empty label"), c = doc("type only"), p = doc("page only");
        chunk(TENANT, h(65), labelsOnly(OK_AT, "\"section_title\":null"));
        chunk(TENANT, h(66), labelsOnly(OK_AT, "\"section_title\":\"\""));
        chunk(TENANT, h(67), labelsOnly(OK_AT, "\"section_type\":\"heading\""));
        chunk(TENANT, h(68), labelsOnly(OK_AT, "\"page_number\":4"));
        manifest(a, h(65));
        manifest(b, h(66));
        manifest(c, h(67));
        manifest(p, h(68));
        assertThat(fillable("labels")).isEqualTo(4);
        assertThat(fillable("spans")).as("none of these chunks records a span").isEqualTo(0);
    }

    /**
     * HARD-TO-MATCH (T2 nexus/rung-detect-route-spec-2026-11-10 section 3): the client sends a non-string label, or a
     * string or fractional page_number, as it finds it and lets the engine coerce it; SQL leaves such a row alone
     * (bucket label_value_not_scalar) rather than guess, so it is NOT counted.
     */
    @Test
    void aLabelValueThatIsNotAScalarTheClientWouldSendAsIsIsNotCounted() throws Exception {
        String[] odd = {
            "\"section_title\":7", "\"section_title\":{\"a\":1}", "\"section_title\":[\"x\"]", "\"section_title\":true",
            "\"section_type\":3", "\"page_number\":\"3\"", "\"page_number\":3.5", "\"page_number\":true",
            "\"page_number\":{\"n\":1}", "\"page_number\":12345678901"};
        for (int i = 0; i < odd.length; i++) {
            String d = doc("odd " + i);
            chunk(TENANT, h(100 + i), labelsOnly(OK_AT, odd[i] + ",\"line_start\":5"));
            manifest(d, h(100 + i));
        }
        String control = doc("control beside odd values");
        chunk(TENANT, h(0xc2), both(OK_AT));
        manifest(control, h(0xc2));
        assertThat(fillable("labels")).as("only the control").isEqualTo(1);
        assertThat(fillable("spans")).as("a span is a span whatever the label looks like").isEqualTo(odd.length + 1);
    }

    // ── spans only ──────────────────────────────────────────────────────────

    @Test
    void aRowWithEverySpanColumnSetIsNotCountedForSpans_butStillForLabels() throws Exception {
        String d = doc("complete span");
        chunk(TENANT, h(30), both(OK_AT));
        manifest(d, h(30));
        spans(d, 0, 5, 9, 0, 40);
        assertThat(fillable("spans")).isEqualTo(0);
        assertThat(fillable("labels")).isEqualTo(1);
        assertThat(beside("spans")).isEqualTo(0);
    }

    @Test
    void aChunkWithNoSpanKeyForTheNullColumnsIsNotCounted() throws Exception {
        // the manifest has line_start; the chunk records only line_start; the NULL columns have no key to copy
        String d = doc("keys for the set column only");
        chunk(TENANT, h(31), labelsOnly(OK_AT, "\"section_title\":\"Intro\",\"line_start\":5"));
        manifest(d, h(31));
        spans(d, 0, 5, null, null, null);
        assertThat(fillable("spans")).isEqualTo(0);
        String e = doc("no span keys at all");
        chunk(TENANT, h(32), labelsOnly(OK_AT, "\"section_title\":\"Intro\""));
        manifest(e, h(32));
        assertThat(fillable("spans")).isEqualTo(0);
        assertThat(beside("spans")).isEqualTo(0);
    }

    @Test
    void aChunkWhoseSpanKeysAreZeroOrNotANumberRecordsNothing() throws Exception {
        String[] unrecorded = {
            "\"line_start\":0,\"line_end\":0,\"chunk_start_char\":0,\"chunk_end_char\":0",
            "\"chunk_start_char\":0",                  // a 0 start is real only beside a recorded end
            "\"line_start\":\"5\"", "\"line_start\":5.0", "\"line_start\":null", "\"line_start\":true",
            "\"line_start\":-3", "\"chunk_end_char\":-4"};
        for (int i = 0; i < unrecorded.length; i++) {
            String d = doc("unrecorded " + i);
            chunk(TENANT, h(110 + i), labelsOnly(OK_AT, unrecorded[i]));
            manifest(d, h(110 + i));
        }
        assertThat(beside("spans")).isEqualTo(0);
    }

    @Test
    void aSpanTheChunkRecordsForANullColumnIsCounted_eachColumnAndTheZeroStartBesideAnEnd() throws Exception {
        String[] recorded = {
            "\"line_start\":5", "\"line_end\":9", "\"chunk_end_char\":40",
            "\"chunk_start_char\":12",
            "\"chunk_start_char\":0,\"chunk_end_char\":40",   // a head-of-text start is real beside a recorded end
            "\"chunk_end_char\":0,\"chunk_start_char\":7"};    // an unrecorded end does not stop a positive start
        for (int i = 0; i < recorded.length; i++) {
            String d = doc("recorded " + i);
            chunk(TENANT, h(130 + i), labelsOnly(OK_AT, recorded[i]));
            manifest(d, h(130 + i));
        }
        assertThat(fillable("spans")).isEqualTo(recorded.length);
    }

    @Test
    void aChunkSpanTheManifestRowContradictsIsNotCounted_andOneItAgreesWithIs() throws Exception {
        String clash = doc("clash"), agree = doc("agree");
        chunk(TENANT, h(40), labelsOnly(OK_AT, "\"line_start\":4,\"line_end\":9"));
        chunk(TENANT, h(41), labelsOnly(OK_AT, "\"line_start\":4,\"line_end\":9"));
        manifest(clash, h(40));
        manifest(agree, h(41));
        spans(clash, 0, 3, null, null, null);   // line_start 3 against the chunk's 4: another position's span
        spans(agree, 0, 4, null, null, null);   // line_start 4 agrees, so line_end 9 is filled
        assertThat(fillable("spans")).isEqualTo(1);
    }

    @Test
    void aSpanValueThatDoesNotFitAnInt4IsNotRecorded_andDoesNotFailTheRoute() throws Exception {
        String d = doc("huge");
        chunk(TENANT, h(45), labelsOnly(OK_AT, "\"line_start\":99999999999,\"chunk_start_char\":-5,\"chunk_end_char\":12345678901"));
        manifest(d, h(45));
        assertThat(beside("spans")).isEqualTo(0);
    }

    // ── the bound, the tenant, the request ──────────────────────────────────

    @Test
    void theCountStopsAtTheLimitAndSaysSo() throws Exception {
        for (int i = 0; i < 3; i++) {
            String d = doc("bounded " + i);
            chunk(TENANT, h(150 + i), both(OK_AT));
            manifest(d, h(150 + i));
        }
        for (String kind : KINDS) {
            assertThat(fillable(kind, 2)).containsEntry("fillable", 2).containsEntry("limit", 2).containsEntry("capped", true);
            assertThat(fillable(kind, 3)).containsEntry("fillable", 3).containsEntry("capped", true);
            assertThat(fillable(kind, 4)).containsEntry("fillable", 3).containsEntry("capped", false);
        }
    }

    @Test
    void anotherTenantsRowsAreNotCounted() throws Exception {
        String theirs = doc(TOKEN_B, TENANT_B, "their doc", "", DOC_AT, "{}");
        chunk(TENANT_B, h(50), both(OK_AT));
        manifestAs(TOKEN_B, TENANT_B, theirs, h(50));
        assertThat(beside("labels")).as("the default tenant sees only its own control").isEqualTo(0);
        assertThat(fillable("spans")).as("the default tenant sees only its own control").isEqualTo(1);
        for (String kind : KINDS) {
            var resp = get(TOKEN_B, TENANT_B, "/v1/catalog/manifest/fillable?kind=" + kind);
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
            assertThat(mapper.readValue(resp.body(), MAP_T)).containsEntry("fillable", 1).containsEntry("limit", 1000);
        }
    }

    @Test
    void aBadKindOrLimitIsA400_andOnlyGetIsAllowed() throws Exception {
        for (String bad : List.of("", "?kind=", "?kind=foo", "?kind=LABELS", "?kind=labels,spans", "?limit=5")) {
            var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/fillable" + bad);
            assertThat(resp.statusCode()).as("query " + bad).isEqualTo(400);
        }
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/fillable?kind=spans&limit=" + bad);
            assertThat(resp.statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(post(TOKEN, TENANT, "/v1/catalog/manifest/fillable?kind=labels", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void theDefaultLimitIsOneThousand() throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/fillable?kind=labels");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T)).containsEntry("limit", 1000);
    }

    @Test
    void aCountThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextCountAnswers() throws Exception {
        // Deterministic: another session holds ACCESS EXCLUSIVE on the manifest table, so the count waits on the lock
        // until its statement_timeout fires (a lock wait is bounded by statement_timeout too).
        String d = doc("bounded by time");
        chunk(TENANT, h(70), both(OK_AT));
        manifest(d, h(70));
        System.setProperty(dev.nexus.service.db.CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENT_CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            for (String kind : KINDS) {
                long t0 = System.nanoTime();
                var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/fillable?kind=" + kind);
                long ms = (System.nanoTime() - t0) / 1_000_000;
                assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
                var body = mapper.readValue(resp.body(), MAP_T);
                assertThat(body).containsEntry("reason", "statement_timeout");
                assertThat(body).doesNotContainKeys("fillable", "kind", "capped");
                assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            }
            holder.rollback();
        } finally {
            System.clearProperty(dev.nexus.service.db.CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        for (String kind : KINDS) assertThat(fillable(kind)).as("the cancelled count left the pool usable").isEqualTo(1);
    }
}
