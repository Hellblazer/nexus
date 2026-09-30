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
}
