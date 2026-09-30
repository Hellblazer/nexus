// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 P1.2 (bead nexus-z0o2p.3) -- {@code write_many} reports, per document, the
 * chashes its write dropped from that document's previous manifest
 * ({@code dropped_chashes}), whether or not {@code sweep} is on. A multi-batch writer
 * writes its first batch with {@code sweep} off, carries this list to the document's
 * last append as {@code sweep_chashes}, and sweeps only then (RDR-223 Technical
 * Design 1). Before this bead the previous manifest was read only when {@code sweep}
 * was on, so the sweep-off response could not name the dropped chashes.
 */
class WriteManyDroppedChashesTest extends AtomicWriteTestBase {

    @SuppressWarnings("unchecked")
    private static Map<String, List<String>> dropped(Map<String, Object> response) {
        return (Map<String, List<String>>) response.get("dropped_chashes");
    }

    /** Seeds {@code docId} with chunks a,b,c at positions 0..2 (sweep off, so nothing is dropped). */
    private String[] seedABC(Fx f, String tag) {
        String a = ch(tag + "-a"), b = ch(tag + "-b"), c = ch(tag + "-c");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, tag + " a"), chunk(b, tag + " b"), chunk(c, tag + " c")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, b), row(2, c)))),
            null, false, false);
        return new String[] {a, b, c};
    }

    @Test
    void sweepOff_returnsPreviousManifestMinusNew_inManifestOrder() throws Exception {
        Fx f = fixture("off");
        String[] abc = seedABC(f, "off");
        String d = ch("off-d");

        var result = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(d, "off d")),
            List.of(doc(f.docId(), List.of(row(0, abc[1]), row(1, d)))),   // keeps b, adds d; drops a, c
            null, false, false).response();

        assertThat(dropped(result))
            .as("previous {a,b,c} minus new {b,d}, in the previous manifest's position order")
            .containsExactly(Map.entry(f.docId(), List.of(abc[0], abc[2])));
    }

    @Test
    void sweepOff_onTheNonCombinedPath_returnsTheDiffToo() throws Exception {
        Fx f = fixture("nc");
        String[] abc = seedABC(f, "nc");

        var result = repo.writeManifestMany(TENANT,
            List.of(doc(f.docId(), List.of(row(0, abc[2])))), f.collection(), null, false);

        assertThat(dropped(result)).containsExactly(Map.entry(f.docId(), List.of(abc[0], abc[1])));
    }

    @Test
    void firstWriteOfANewDocument_returnsAnEmptyList() throws Exception {
        Fx f = fixture("new");
        String x = ch("new-x");

        var result = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(x, "new x")),
            List.of(doc(f.docId(), List.of(row(0, x)))),
            null, false, false).response();

        assertThat(dropped(result)).containsExactly(Map.entry(f.docId(), List.<String>of()));
    }

    @Test
    void sweepOff_deletesNoChunk() throws Exception {
        Fx f = fixture("keep");
        String[] abc = seedABC(f, "keep");
        String d = ch("keep-d");

        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(d, "keep d")),
            List.of(doc(f.docId(), List.of(row(0, d)))),
            null, false, false);

        for (String dropped : abc) {
            assertThat(chunkExists(f.collection(), dropped))
                .as("sweep off: a dropped chunk stays (ownerless, hidden by live(c)) until a later sweep")
                .isTrue();
        }
    }

    @Test
    void sweepOn_returnsTheListItSweeps_andSweepsAsBefore() throws Exception {
        Fx f = fixture("on");
        String[] abc = seedABC(f, "on");

        var result = repo.writeManifestMany(TENANT,
            List.of(doc(f.docId(), List.of(row(0, abc[2])))), f.collection(), null, true);

        assertThat(dropped(result)).containsExactly(Map.entry(f.docId(), List.of(abc[0], abc[1])));
        assertThat(result.get("swept")).as("both dropped chunks are swept").isEqualTo(2);
        assertThat(chunkExists(f.collection(), abc[0])).isFalse();
        assertThat(chunkExists(f.collection(), abc[1])).isFalse();
        assertThat(chunkExists(f.collection(), abc[2])).as("the kept chunk survives").isTrue();
    }

    @Test
    void severalDocuments_getOneEntryEach_andAFailedDocumentGetsNone() throws Exception {
        Fx f = fixture("multi");
        String other = freshDoc("multi-other", f.collection());
        String[] abc = seedABC(f, "multi");
        String x = ch("multi-x");

        var result = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(x, "multi x")),
            List.of(doc(f.docId(), List.of(row(0, abc[0]))),      // drops b, c
                    doc(other, List.of(row(0, x))),               // new document
                    doc("aw.no-such-doc", List.of(row(0, x)))),   // fails: not registered
            null, false, false).response();

        assertThat(dropped(result))
            .containsOnlyKeys(f.docId(), other)
            .containsEntry(f.docId(), List.of(abc[1], abc[2]))
            .containsEntry(other, List.of());
        assertThat(result.get("failed_doc_ids")).isEqualTo(List.of("aw.no-such-doc"));
    }

    @Test
    void anUnchangedRewrite_dropsNothing() throws Exception {
        Fx f = fixture("same");
        String[] abc = seedABC(f, "same");
        int embedsBefore = embedder.calls.get();

        var result = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(abc[0], "same a"), chunk(abc[1], "same b"), chunk(abc[2], "same c")),
            List.of(doc(f.docId(), List.of(row(0, abc[0]), row(1, abc[1]), row(2, abc[2])))),
            null, false, false).response();

        assertThat(dropped(result)).containsExactly(Map.entry(f.docId(), List.<String>of()));
        assertThat(embedder.calls.get() - embedsBefore).as("nothing changed, nothing re-embedded").isZero();
    }
}
