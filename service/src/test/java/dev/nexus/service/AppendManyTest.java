// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.RacedEmbedActivity;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 P1.4 (bead nexus-z0o2p.5) -- {@code manifest/append_many}: several documents in
 * one request, one transaction per document, request-level {@code chunks} each document
 * inserts only the part of that its own rows reference, per-document {@code sweep_chashes}.
 * Needed by {@code .nxexp} import (RDR-223 F-6), which spreads one document over many
 * chash-ordered pages and many documents over one page.
 */
class AppendManyTest extends AtomicWriteTestBase {

    private static Map<String, Object> mdoc(String docId, List<Map<String, Object>> rows, List<String> sweep) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("doc_id", docId);
        d.put("rows", rows);
        d.put("sweep_chashes", sweep);
        return d;
    }

    private static Map<String, Object> mdoc(String docId, List<Map<String, Object>> rows) {
        return mdoc(docId, rows, List.of());
    }

    private static Map<String, Object> mdoc(String docId, List<Map<String, Object>> rows, List<String> sweep,
                                            String contentHash, int chunkCount) {
        Map<String, Object> d = mdoc(docId, rows, sweep);
        d.put("complete", Map.of("content_hash", contentHash, "chunk_count", chunkCount));
        return d;
    }

    private String indexState(String docId) {
        return (String) repo.getDocument(TENANT, docId).get("index_state");
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> results(Map<String, Object> response) {
        return (List<Map<String, Object>>) response.get("results");
    }

    @Test
    void twoDocuments_oneMissing_theExistingCommits_theMissingReportsAndInsertsNoChunkOfItsOwn() throws Exception {
        Fx f = fixture("miss");
        String onlyForA = ch("miss-a"), onlyForMissing = ch("miss-only"), shared = ch("miss-shared");

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, onlyForA), row(1, shared))),
                    mdoc("aw.no-such-doc", List.of(row(0, onlyForMissing), row(1, shared)))),
            List.of(chunk(onlyForA, "a text"), chunk(onlyForMissing, "only the missing doc's"),
                    chunk(shared, "shared text")),
            false).response();

        assertThat(response).containsEntry("docs", 1).containsEntry("rows", 2)
            .containsEntry("failed_doc_ids", List.of("aw.no-such-doc"));
        assertThat(manifestChashes(f.docId())).containsExactly(onlyForA, shared);
        assertThat(chunkExists(f.collection(), onlyForA)).isTrue();
        assertThat(chunkExists(f.collection(), shared)).isTrue();
        assertThat(chunkExists(f.collection(), onlyForMissing))
            .as("a chunk only the failed document references is not inserted (its transaction never opened the upsert)")
            .isFalse();
    }

    @Test
    void perDocumentResults_comeBackInRequestOrder_withTheFailureInPlace() throws Exception {
        Fx f = fixture("order");
        String second = freshDoc("order2", f.collection());
        String x = ch("order-x"), y = ch("order-y"), z = ch("order-z");

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(second, List.of(row(0, y))),
                    mdoc("aw.order-missing", List.of(row(0, x))),
                    mdoc(f.docId(), List.of(row(0, z), row(1, x)))),
            List.of(chunk(x, "x"), chunk(y, "y"), chunk(z, "z")),
            false).response();

        var r = results(response);
        assertThat(r).extracting(m -> m.get("doc_id")).containsExactly(second, "aw.order-missing", f.docId());
        assertThat(r).extracting(m -> m.get("ok")).containsExactly(true, false, true);
        assertThat(r.get(0)).containsEntry("count", 1).containsEntry("chunks_written", 1);
        assertThat(r.get(1).get("reason").toString()).contains("document not registered");
        assertThat(r.get(2)).containsEntry("count", 2).containsEntry("chunks_written", 2);
        assertThat(response).containsEntry("chunks_written", 3);
    }

    @Test
    void eachDocumentsSweepChashes_areSweptOnlyIfThatDocumentCommitted() throws Exception {
        Fx f = fixture("swp");
        String other = freshDoc("swp-other", f.collection());
        String forCommitted = ch("swp-x"), forFailed = ch("swp-y"), kept = ch("swp-k");
        // Two ownerless chunks: x and y were dropped from a previous document version.
        String holder = freshDoc("swp-holder", f.collection());
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(forCommitted, "x"), chunk(forFailed, "y")),
            List.of(doc(holder, List.of(row(0, forCommitted), row(1, forFailed)))),
            null, false, false);
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, kept)), List.of(forCommitted)),
                    mdoc("aw.swp-missing", List.of(row(0, kept)), List.of(forFailed)),
                    mdoc(other, List.of(row(0, kept)))),
            List.of(chunk(kept, "kept")),
            false).response();

        assertThat(chunkExists(f.collection(), forCommitted)).as("swept: its document committed").isFalse();
        assertThat(chunkExists(f.collection(), forFailed)).as("not swept: its document failed").isTrue();
        assertThat(response).containsEntry("swept", 1).containsEntry("sweep_skipped", 0);
        var r = results(response);
        assertThat(r.get(0)).containsEntry("swept", 1);
        assertThat(r.get(1)).containsEntry("ok", false).doesNotContainKey("swept");
        assertThat(r.get(2)).doesNotContainKey("swept");
    }

    @Test
    void aChashOneDocumentDropsAndALaterDocumentOfTheSameRequestReferences_survives() throws Exception {
        Fx f = fixture("cross");
        String laterDoc = freshDoc("cross-later", f.collection());
        String z = ch("cross-z"), w = ch("cross-w");
        // z is owned by f's document only; the request drops it there and re-adds it to laterDoc.
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(z, "z text")),
            List.of(doc(f.docId(), List.of(row(0, z)))),
            null, false, false);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, w)), List.of(z)),        // drops z (sweep_chashes)
                    mdoc(laterDoc, List.of(row(0, z)))),                    // and a later doc takes it
            List.of(chunk(w, "w text"), chunk(z, "z text")),                // z: identical text, so skipped
            false).response();

        assertThat(response).containsEntry("failed_doc_ids", List.of());
        assertThat(manifestChashes(laterDoc)).containsExactly(z);
        assertThat(chunkExists(f.collection(), z))
            .as("the sweeps run after every document has committed, so the later document's row protects z")
            .isTrue();
        assertThat(results(response).get(0)).containsEntry("swept", 0);
    }

    @Test
    void aChashSharedByTwoDocumentsOfOneRequest_isNotCountedAsARacedEmbed() throws Exception {
        Fx f = fixture("race");
        String second = freshDoc("race2", f.collection());
        String shared = ch("race-shared");
        long before = RacedEmbedActivity.total();

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, shared))), mdoc(second, List.of(row(0, shared)))),
            List.of(chunk(shared, "shared text")),
            false).response();

        assertThat(response).containsEntry("chunks_written", 2).containsEntry("embed_embedded", 1);
        assertThat(RacedEmbedActivity.total() - before)
            .as("the second document's ON CONFLICT is the request's own fan-out, not another writer")
            .isZero();
        assertThat(embedder.calls.get()).isGreaterThan(0);
    }

    @Test
    void chunksNoDocumentReferences_areNeitherEmbeddedNorInserted() throws Exception {
        Fx f = fixture("stray");
        String used = ch("stray-used"), stray = ch("stray-stray");
        int before = embedder.calls.get();

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, used)))),
            List.of(chunk(used, "used"), chunk(stray, "stray")),
            false).response();

        assertThat(embedder.calls.get() - before).isEqualTo(1);
        assertThat(response).containsEntry("chunks_deduped", 1);
        assertThat(chunkExists(f.collection(), stray)).isFalse();
    }

    // ── import shape (RDR-223 F-6): chash-ordered pages scatter a document's positions ──

    private List<String> positionsAndChashes(String docId) {
        List<String> out = new ArrayList<>();
        for (var r : repo.getManifest(TENANT, docId)) out.add(r.get("position") + "=" + r.get("chash"));
        return out;
    }

    @Test
    void scatteredPositions_acrossAppendManyPages_endUpInManifestOrder() throws Exception {
        Fx f = fixture("scat");
        String c0 = ch("scat-0"), c1 = ch("scat-1"), c3 = ch("scat-3"), c5 = ch("scat-5"), c40 = ch("scat-40");

        // Page 1 carries positions 40, 5, 1 in that (chash) order; page 2 carries 3 and 0.
        var p1 = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(40, c40), row(5, c5), row(1, c1)))),
            List.of(chunk(c40, "40"), chunk(c5, "5"), chunk(c1, "1")), false).response();
        var p2 = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(3, c3), row(0, c0)))),
            List.of(chunk(c3, "3"), chunk(c0, "0")), false).response();

        assertThat(p1).containsEntry("failed_doc_ids", List.of());
        assertThat(p2).containsEntry("failed_doc_ids", List.of());
        assertThat(positionsAndChashes(f.docId()))
            .as("scattered positions land where they say, read back in position order")
            .containsExactly("0=" + c0, "1=" + c1, "3=" + c3, "5=" + c5, "40=" + c40);
        assertThat(repo.getManifest(TENANT, f.docId()).get(4)).containsEntry("chunk_index", 40);
    }

    @Test
    void aFirstSightingWriteManyMixedWithAppendMany_replacesTheStaleManifestOnceAndOnlyOnce() throws Exception {
        Fx f = fixture("mix");
        String docB = freshDoc("mix-b", f.collection());
        String o0 = ch("mix-o0"), o1 = ch("mix-o1"), o2 = ch("mix-o2"), o3 = ch("mix-o3");
        String a0 = ch("mix-a0"), a1 = ch("mix-a1"), a2 = ch("mix-a2");
        String b0 = ch("mix-b0"), b1 = ch("mix-b1"), b3 = ch("mix-b3");
        // A previous import left doc A with a stale four-row manifest.
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(o0, "o0"), chunk(o1, "o1"), chunk(o2, "o2"), chunk(o3, "o3")),
            List.of(doc(f.docId(), List.of(row(0, o0), row(1, o1), row(2, o2), row(3, o3)))), null, false, false);
        // Page 0: doc B's first sighting (write_many).
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(b3, "b3")),
            List.of(doc(docB, List.of(row(3, b3)))), null, false, false);

        // Page 1: doc A's FIRST sighting (write_many replaces its stale manifest) AND doc B's
        // continuation (append_many), against the same collection.
        var wm = svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(a2, "a2")),
            List.of(doc(f.docId(), List.of(row(2, a2)))), null, false, false).response();
        var am = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(docB, List.of(row(1, b1)))), List.of(chunk(b1, "b1")), false).response();
        // Page 2: continuations of BOTH documents in ONE append_many.
        var am2 = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, a0), row(1, a1))), mdoc(docB, List.of(row(0, b0)))),
            List.of(chunk(a0, "a0"), chunk(a1, "a1"), chunk(b0, "b0")), false).response();

        assertThat(wm).containsEntry("failed_doc_ids", List.of());
        assertThat(am).containsEntry("failed_doc_ids", List.of());
        assertThat(am2).containsEntry("failed_doc_ids", List.of()).containsEntry("docs", 2);
        assertThat(positionsAndChashes(f.docId())).as("first sighting replaced the stale rows; the appends only added")
            .containsExactly("0=" + a0, "1=" + a1, "2=" + a2);
        assertThat(positionsAndChashes(docB)).containsExactly("0=" + b0, "1=" + b1, "3=" + b3);
        assertThat((Map<String, List<String>>) wm.get("dropped_chashes"))
            .containsEntry(f.docId(), List.of(o0, o1, o2, o3));
        for (String stale : List.of(o0, o1, o2, o3)) {
            assertThat(chunkExists(f.collection(), stale)).as("no sweep ran in this mix").isTrue();
        }
    }

    @Test
    void chunksOnlyAnUnregisteredDocumentReferences_areNotEmbedded_andAreCountedUnreferenced() throws Exception {
        Fx f = fixture("unreg");
        String mine = ch("unreg-mine"), theirs = ch("unreg-theirs");
        int before = embedder.calls.get();

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, mine))), mdoc("aw.unreg-missing", List.of(row(0, theirs)))),
            List.of(chunk(mine, "mine"), chunk(theirs, "theirs"), chunk(ch("unreg-stray"), "stray")),
            false).response();

        assertThat(embedder.calls.get() - before).as("only the registered document's chunk is embedded").isEqualTo(1);
        assertThat(response).containsEntry("docs", 1).containsEntry("failed_doc_ids", List.of("aw.unreg-missing"))
            .containsEntry("chunks_unreferenced", 2).containsEntry("chunks_deduped", 1);
        assertThat(chunkExists(f.collection(), theirs)).isFalse();
    }

    @Test
    void manyDocumentsInOneRequest_allLand() throws Exception {
        Fx f = fixture("many");
        List<Map<String, Object>> docs = new ArrayList<>();
        List<Map<String, Object>> chunks = new ArrayList<>();
        for (int i = 0; i < 25; i++) {
            String c = ch("many-" + i);
            chunks.add(chunk(c, "many text " + i));
            docs.add(mdoc(freshDoc("many" + i, f.collection()), List.of(row(0, c))));
        }
        var response = svc.appendManyCombined(TENANT, f.collection(), docs, chunks, false).response();
        assertThat(response).containsEntry("docs", 25).containsEntry("rows", 25)
            .containsEntry("chunks_written", 25);
        assertThat(chunkCount(f.collection())).isEqualTo(25);
    }

    // ── optional per-document `complete` (RDR-223 fix round, nexus-z0o2p.19): the completion stamp
    //    rides a document's LAST append, with write_many's semantics (content hash + manifest ROW
    //    count, the same fail-closed verify, refusals reported rather than failing the append). ──

    @Test
    void completeOnALastAppend_stampsTheDocumentAfterItsRowsLand_andReportsNoRefusal() throws Exception {
        Fx f = fixture("cmp");
        String a = ch("cmp-a"), b = ch("cmp-b"), c = ch("cmp-c");

        var first = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, a), row(2, c)))), List.of(chunk(a, "a"), chunk(c, "c")),
            false).response();
        assertThat(indexState(f.docId())).as("no complete on the first append: not stamped").isNotEqualTo("complete");
        assertThat(first).containsEntry("complete_refused_count", 0);

        var last = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(1, b)), List.of(), "cmp-hash", 3)), List.of(chunk(b, "b")),
            false).response();

        assertThat(last).containsEntry("failed_doc_ids", List.of()).containsEntry("complete_refused_count", 0);
        assertThat((List<?>) last.get("complete_refused")).isEmpty();
        assertThat(indexState(f.docId())).isEqualTo("complete");
        assertThat(repo.getDocument(TENANT, f.docId())).containsEntry("index_content_hash", "cmp-hash");
    }

    @Test
    void completeWithAWrongRowCount_isRefusedInTheResponse_theRowsStillLand_andTheStateIsNotComplete() throws Exception {
        Fx f = fixture("cmpbad");
        String a = ch("cmpbad-a"), b = ch("cmpbad-b");

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, a), row(1, b)), List.of(), "cmpbad-hash", 5)),
            List.of(chunk(a, "a"), chunk(b, "b")), false).response();

        assertThat(response).containsEntry("docs", 1).containsEntry("failed_doc_ids", List.of())
            .containsEntry("complete_refused_count", 1);
        @SuppressWarnings("unchecked")
        var refused = (List<Map<String, Object>>) response.get("complete_refused");
        assertThat(refused).hasSize(1);
        assertThat(refused.get(0)).containsEntry("doc_id", f.docId()).containsEntry("referenced", 2L)
            .containsEntry("chunk_count", 5).containsEntry("missing", 0L);
        assertThat(manifestChashes(f.docId())).as("over-work, never under-work: the rows are correct").containsExactly(a, b);
        assertThat(indexState(f.docId())).isNotEqualTo("complete");
    }

    @Test
    void theStampCountsManifestRows_notDistinctChashes() throws Exception {
        Fx f = fixture("cmprow");
        String a = ch("cmprow-a");

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, a), row(1, a)), List.of(), "cmprow-hash", 2)),
            List.of(chunk(a, "a")), false).response();

        assertThat(response).containsEntry("complete_refused_count", 0);
        assertThat(indexState(f.docId())).as("one chash at two positions is two manifest rows").isEqualTo("complete");
    }

    @Test
    void completeRidesTheSameRequestAsTheDeferredSweep_andASweepOnlyAppendCanStamp() throws Exception {
        Fx f = fixture("cmpswp");
        String holder = freshDoc("cmpswp-holder", f.collection());
        String dropped = ch("cmpswp-dropped"), kept = ch("cmpswp-kept");
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(dropped, "dropped")),
            List.of(doc(holder, List.of(row(0, dropped)))), null, false, false);
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, kept)), List.of(dropped), "cmpswp-hash", 1)),
            List.of(chunk(kept, "kept")), false).response();
        assertThat(response).containsEntry("swept", 1).containsEntry("complete_refused_count", 0);
        assertThat(chunkExists(f.collection(), dropped)).isFalse();
        assertThat(indexState(f.docId())).isEqualTo("complete");

        // A sweep-only append (no rows) stamps too: the verify reads the manifest, not the request.
        Fx g = fixture("cmpso");
        String x = ch("cmpso-x");
        svc.appendManyCombined(TENANT, g.collection(), List.of(mdoc(g.docId(), List.of(row(0, x)))),
            List.of(chunk(x, "x")), false);
        var stamp = svc.appendManyCombined(TENANT, g.collection(),
            List.of(mdoc(g.docId(), List.of(), List.of(), "cmpso-hash", 1)), List.of(), false).response();
        assertThat(stamp).containsEntry("complete_refused_count", 0);
        assertThat(indexState(g.docId())).isEqualTo("complete");
    }

    @Test
    void aDocumentThatFails_isNotStamped_andItsSiblingIs() throws Exception {
        Fx f = fixture("cmpfail");
        String a = ch("cmpfail-a");

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc("aw.cmpfail-missing", List.of(row(0, a)), List.of(), "h-missing", 1),
                    mdoc(f.docId(), List.of(row(0, a)), List.of(), "h-ok", 1)),
            List.of(chunk(a, "a")), false).response();

        assertThat(response).containsEntry("failed_doc_ids", List.of("aw.cmpfail-missing"))
            .containsEntry("complete_refused_count", 0);
        assertThat(indexState(f.docId())).isEqualTo("complete");
    }

    @Test
    void withoutComplete_theStateIsUntouched_andTheRefusalFieldsAreEmpty() throws Exception {
        Fx f = fixture("cmpabs");
        String a = ch("cmpabs-a");
        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, a)))), List.of(chunk(a, "a")), false).response();
        assertThat(response).containsEntry("complete_refused_count", 0);
        assertThat((List<?>) response.get("complete_refused")).isEmpty();
        assertThat(indexState(f.docId())).isNull();
        assertThat(results(response).get(0)).doesNotContainKey("complete");
    }

    // ── nexus-tj0wu: the per-row `expected` guard. UPSERT_APPEND writes chash, labels and labels_written from
    //    the request, so a caller that re-read a row and writes it back must not revert a re-index or a label
    //    write that committed in between. ──

    private static Map<String, Object> guarded(Map<String, Object> row, Map<String, Object> expected) {
        Map<String, Object> out = new LinkedHashMap<>(row);
        out.put("expected", expected);
        return out;
    }

    private static Map<String, Object> labelled(int position, String chash, String title, String type, int page) {
        Map<String, Object> r = new LinkedHashMap<>(row(position, chash));
        r.put("section_title", title);
        r.put("section_type", type);
        r.put("page_number", page);
        r.put("labels_written", true);
        return r;
    }

    private Map<String, Object> manifestRow(String docId, int position) {
        return repo.getManifest(TENANT, docId).stream()
            .filter(r -> ((Number) r.get("position")).intValue() == position).findFirst().orElseThrow();
    }

    /** A document with the chunks a, b, c stored and the manifest rows 0:a, 1:b (legacy labels). */
    private Fx seededGuardFixture(String tag, String a, String b, String c) {
        Fx f = fixture(tag);
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "a text"), chunk(b, "b text"), chunk(c, "c text")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, b), row(2, c)))), null, false, false);
        return f;
    }

    @Test
    void anExpectedRowWhoseStoredValuesStillMatch_isWritten_andNothingIsReportedRefused() throws Exception {
        String a = ch("exp-ok-a"), b = ch("exp-ok-b"), c = ch("exp-ok-c");
        Fx f = seededGuardFixture("expok", a, b, c);
        Map<String, Object> mine = new LinkedHashMap<>(row(0, a));
        mine.put("line_start", 5);
        mine.put("labels_written", false);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(mine, Map.of(
                "chash", a, "labels_written", false))))), null, false).response();

        assertThat(response).containsEntry("docs", 1).containsEntry("rows", 1)
            .containsEntry("expected_refused_count", 0).containsEntry("failed_doc_ids", List.of());
        assertThat((List<?>) response.get("expected_refused")).isEmpty();
        assertThat(manifestRow(f.docId(), 0)).containsEntry("line_start", 5);
    }

    @Test
    void aReIndexOfThePositionBetweenTheReadAndTheWrite_isNotRevertedToTheReadChash() throws Exception {
        String a = ch("exp-race-a"), b = ch("exp-race-b"), c = ch("exp-race-c");
        Fx f = seededGuardFixture("exprace", a, b, c);
        // The caller read position 0 holding `a`; a re-index then puts `c` there ...
        repo.appendManifestChunks(TENANT, f.docId(), f.collection(), List.of(row(0, c)));
        assertThat(manifestRow(f.docId(), 0)).containsEntry("chash", c);

        // ... and the caller's write, still carrying what it read, is refused instead of putting `a` back.
        Map<String, Object> mine = new LinkedHashMap<>(row(0, a));
        mine.put("line_start", 5);
        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(mine, Map.of("chash", a))))), null, false).response();

        assertThat(response).containsEntry("docs", 1).containsEntry("rows", 0)
            .containsEntry("expected_refused_count", 1).containsEntry("failed_doc_ids", List.of());
        @SuppressWarnings("unchecked")
        var refused = (List<Map<String, Object>>) response.get("expected_refused");
        assertThat(refused).containsExactly(Map.of("doc_id", f.docId(), "position", 0, "reason", "chash_changed"));
        assertThat(results(response).get(0)).containsEntry("ok", true).containsEntry("count", 0)
            .containsEntry("expected_refused", 1);
        assertThat(manifestRow(f.docId(), 0)).containsEntry("chash", c).containsEntry("line_start", null);
    }

    @Test
    void aLabelWriteBetweenTheReadAndTheWrite_isNotRevertedToTheReadLabels() throws Exception {
        String a = ch("exp-lab-a"), b = ch("exp-lab-b"), c = ch("exp-lab-c");
        Fx f = seededGuardFixture("explab", a, b, c);
        // The caller read position 0 as a legacy row (no labels); a label write then records the labels.
        repo.appendManifestChunks(TENANT, f.docId(), f.collection(), List.of(labelled(0, a, "Intro", "body", 4)));

        Map<String, Object> mine = new LinkedHashMap<>(row(0, a));
        mine.put("line_start", 5);
        mine.put("labels_written", false);
        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(mine, Map.of(
                "chash", a, "labels_written", false))))), null, false).response();

        assertThat(response).containsEntry("rows", 0).containsEntry("expected_refused_count", 1);
        @SuppressWarnings("unchecked")
        var refused = (List<Map<String, Object>>) response.get("expected_refused");
        assertThat(refused.get(0)).containsEntry("reason", "labels_changed");
        // a guard that names only a label is compared on that label: the stored title is no longer NULL
        Map<String, Object> onlyTitle = new LinkedHashMap<>();
        onlyTitle.put("chash", a);
        onlyTitle.put("section_title", null);
        var second = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(mine, onlyTitle)))), null, false).response();
        assertThat(second).containsEntry("rows", 0).containsEntry("expected_refused_count", 1);
        assertThat(manifestRow(f.docId(), 0)).containsEntry("section_title", "Intro").containsEntry("section_type", "body")
            .containsEntry("page_number", 4).containsEntry("line_start", null);
    }

    @Test
    void oneStaleRowDoesNotStopItsFreshNeighbour_norItsDocument() throws Exception {
        String a = ch("exp-mix-a"), b = ch("exp-mix-b"), c = ch("exp-mix-c");
        Fx f = seededGuardFixture("expmix", a, b, c);
        repo.appendManifestChunks(TENANT, f.docId(), f.collection(), List.of(row(0, c)));   // position 0 moved on

        Map<String, Object> stale = new LinkedHashMap<>(row(0, a));
        stale.put("line_start", 5);
        Map<String, Object> fresh = new LinkedHashMap<>(row(1, b));
        fresh.put("line_start", 9);
        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(stale, Map.of("chash", a)), guarded(fresh, Map.of("chash", b))))),
            null, false).response();

        assertThat(response).containsEntry("docs", 1).containsEntry("rows", 1).containsEntry("expected_refused_count", 1);
        assertThat(manifestRow(f.docId(), 0)).containsEntry("chash", c).containsEntry("line_start", null);
        assertThat(manifestRow(f.docId(), 1)).containsEntry("chash", b).containsEntry("line_start", 9);
    }

    @Test
    void aPositionThatIsGone_isRefused_andCreatesNoRow() throws Exception {
        String a = ch("exp-gone-a"), b = ch("exp-gone-b"), c = ch("exp-gone-c");
        Fx f = seededGuardFixture("expgone", a, b, c);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(row(7, a), Map.of("chash", a))))), null, false).response();

        assertThat(response).containsEntry("rows", 0).containsEntry("expected_refused_count", 1);
        @SuppressWarnings("unchecked")
        var refused = (List<Map<String, Object>>) response.get("expected_refused");
        assertThat(refused.get(0)).containsEntry("position", 7).containsEntry("reason", "position_absent");
        assertThat(manifestChashes(f.docId())).containsExactly(a, b, c);
    }

    @Test
    void aRowWithoutExpected_isWrittenAsBefore_andTheRefusalFieldsAreAlwaysThere() throws Exception {
        String a = ch("exp-none-a"), b = ch("exp-none-b"), c = ch("exp-none-c");
        Fx f = seededGuardFixture("expnone", a, b, c);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(row(0, c)))), null, false).response();

        assertThat(response).containsEntry("rows", 1).containsEntry("expected_refused_count", 0);
        assertThat((List<?>) response.get("expected_refused")).isEmpty();
        assertThat(manifestRow(f.docId(), 0)).containsEntry("chash", c);
    }

    @Test
    void aSpanWrittenBetweenTheReadAndTheWrite_isNotOverwritten_whenTheGuardNamesTheSpan() throws Exception {
        String a = ch("exp-span-a"), b = ch("exp-span-b"), c = ch("exp-span-c");
        Fx f = seededGuardFixture("expspan", a, b, c);
        // The caller read position 0 with no span; the position's own writer then records one (same chunk).
        Map<String, Object> theirs = new LinkedHashMap<>(row(0, a));
        theirs.put("line_start", 70);
        theirs.put("line_end", 80);
        repo.appendManifestChunks(TENANT, f.docId(), f.collection(), List.of(theirs));

        Map<String, Object> mine = new LinkedHashMap<>(row(0, a));
        mine.put("line_start", 5);
        Map<String, Object> read = new LinkedHashMap<>();
        read.put("chash", a);
        read.put("line_start", null);
        read.put("line_end", null);
        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(mine, read)))), null, false).response();

        assertThat(response).containsEntry("rows", 0).containsEntry("expected_refused_count", 1);
        @SuppressWarnings("unchecked")
        var refused = (List<Map<String, Object>>) response.get("expected_refused");
        assertThat(refused.get(0)).containsEntry("reason", "span_changed");
        assertThat(manifestRow(f.docId(), 0)).containsEntry("line_start", 70).containsEntry("line_end", 80);
    }

    @Test
    void aTombstonedDocumentsRows_areRefusedAsAbsent_notWritten() throws Exception {
        String a = ch("exp-tomb-a"), b = ch("exp-tomb-b"), c = ch("exp-tomb-c");
        Fx f = seededGuardFixture("exptomb", a, b, c);
        assertThat(repo.deleteDocument(TENANT, f.docId())).isEqualTo(1);

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(mdoc(f.docId(), List.of(guarded(row(0, a), Map.of("chash", a))))), null, false).response();

        assertThat(response).containsEntry("rows", 0).containsEntry("expected_refused_count", 1);
        @SuppressWarnings("unchecked")
        var refused = (List<Map<String, Object>>) response.get("expected_refused");
        assertThat(refused.get(0)).containsEntry("reason", "position_absent");
    }

    @Test
    void theSingleDocumentAppend_refusesARowThatCarriesExpected_ratherThanWritingItUnguarded() throws Exception {
        String a = ch("exp-one-a"), b = ch("exp-one-b"), c = ch("exp-one-c");
        Fx f = seededGuardFixture("expone", a, b, c);

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.appendManifestChunks(
                TENANT, f.docId(), f.collection(), List.of(guarded(row(0, c), Map.of("chash", a)))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("append_many");
        assertThat(manifestRow(f.docId(), 0)).containsEntry("chash", a);
    }
}
