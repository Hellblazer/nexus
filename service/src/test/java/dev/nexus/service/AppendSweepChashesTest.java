// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-223 P1.3 (bead nexus-z0o2p.4) -- {@code manifest/append} takes {@code
 * sweep_chashes} and sweeps them in their own transaction AFTER the append commits,
 * under the sweep gate and the two NOT EXISTS guards {@code write_many}'s sweep uses.
 * This is the deferred sweep of a multi-batch write: batch 1 (sweep off) reports the
 * chashes it dropped, the last append sweeps them once every batch has landed.
 *
 * <p>Size rule (Sam, 2026-09-29, nexus-z0o2p.1): at most
 * {@link CatalogRepository#MAX_SWEEP_CHASHES_PER_APPEND} chashes per append, a longer
 * list refused before any transaction; a sweep-only append (empty {@code rows}) carries
 * the overflow.
 */
class AppendSweepChashesTest extends AtomicWriteTestBase {

    /** Writes {@code chashes} as a fresh document's manifest and returns its doc id. */
    private String ownerOf(Fx f, String tag, List<String> chashes) {
        String docId = freshDoc(tag, f.collection());
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < chashes.size(); i++) {
            chunks.add(chunk(chashes.get(i), tag + " text " + chashes.get(i)));
            rows.add(row(i, chashes.get(i)));
        }
        svc.writeManyCombined(TENANT, f.collection(), chunks, List.of(doc(docId, rows)),
            null, false, false);
        return docId;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> onlySweepDetail(Map<String, Object> response) {
        var detail = (List<Map<String, Object>>) response.get("sweep_detail");
        assertThat(detail).hasSize(1);
        return detail.get(0);
    }

    @Test
    void aListedChashAnotherBatchOfTheSameDocumentReAdded_survives_theRestAreSwept() throws Exception {
        Fx f = fixture("readd");
        String a = ch("readd-a"), b = ch("readd-b"), c = ch("readd-c"), d = ch("readd-d");
        // Previous run: the document owned a, b, c.
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "a"), chunk(b, "b"), chunk(c, "c")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, b), row(2, c)))),
            null, false, false);
        // New run, batch 1 (sweep off): keeps only d and reports {a, b, c} dropped.
        var batch1 = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(d, "d")),
            List.of(doc(f.docId(), List.of(row(0, d)))),
            null, false, false).response();
        @SuppressWarnings("unchecked")
        List<String> dropped = ((Map<String, List<String>>) batch1.get("dropped_chashes")).get(f.docId());
        assertThat(dropped).containsExactly(a, b, c);
        // Batch 2 re-adds b. The last append also carries the dropped list as sweep_chashes.
        var last = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(1, b)), List.of(chunk(b, "b")), false, dropped).response();

        assertThat(chunkExists(f.collection(), a)).as("dropped and unowned: swept").isFalse();
        assertThat(chunkExists(f.collection(), c)).as("dropped and unowned: swept").isFalse();
        assertThat(chunkExists(f.collection(), b)).as("re-added by batch 2: survives").isTrue();
        assertThat(chunkExists(f.collection(), d)).isTrue();
        assertThat(last).containsEntry("swept", 2).containsEntry("sweep_skipped", 0);
        assertThat(onlySweepDetail(last))
            .containsEntry("doc_id", f.docId())
            .containsEntry("dropped", 3).containsEntry("swept", 2).containsEntry("kept", 1)
            .containsEntry("errored", false);
    }

    @Test
    void aListedChashAnotherDocumentOwns_survives() throws Exception {
        Fx f = fixture("shared");
        String shared = ch("shared-x");
        String other = ownerOf(f, "shared-other", List.of(shared));

        var result = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(), List.of(), false, List.of(shared)).response();

        assertThat(chunkExists(f.collection(), shared)).as("owned by another live document").isTrue();
        assertThat(result).containsEntry("swept", 0);
        assertThat(onlySweepDetail(result)).containsEntry("dropped", 1).containsEntry("kept", 1);
        assertThat(manifestChashes(other)).containsExactly(shared);
    }

    @Test
    void aListedChashNoManifestOwns_isDeleted() throws Exception {
        Fx f = fixture("orphan");
        String x = ch("orphan-x");
        String holder = ownerOf(f, "orphan-holder", List.of(x));
        // Drop x from its owner (sweep off): x is now an ownerless chunk.
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);
        assertThat(chunkExists(f.collection(), x)).isTrue();

        var result = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(), List.of(), false, List.of(x)).response();

        assertThat(chunkExists(f.collection(), x)).isFalse();
        assertThat(result).containsEntry("swept", 1).containsEntry("sweep_skipped", 0);
    }

    @Test
    void aFailedAppend_sweepsNothing() throws Exception {
        Fx f = fixture("fail");
        String x = ch("fail-x"), ghost = ch("fail-ghost");
        String holder = ownerOf(f, "fail-holder", List.of(x));
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);

        // The row references a chash that is in neither chunks nor the store: the append fails.
        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(),
                List.of(row(0, ghost)), List.of(), false, List.of(x)))
            .isInstanceOf(IllegalArgumentException.class);
        // And an append to a document that does not exist fails too.
        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), "aw.no-such-doc",
                List.of(), List.of(), false, List.of(x)))
            .isInstanceOf(CatalogRepository.DocumentNotFoundException.class);

        assertThat(chunkExists(f.collection(), x))
            .as("the failed appends' sweep_chashes were never swept")
            .isTrue();
    }

    @Test
    void theCapHolds_atTheCapAccepted_oneOverRefusedBeforeAnyTransaction() throws Exception {
        Fx f = fixture("cap");
        int cap = CatalogRepository.MAX_SWEEP_CHASHES_PER_APPEND;
        assertThat(cap).isEqualTo(300);
        List<String> chashes = new ArrayList<>();
        for (int i = 0; i < cap + 1; i++) chashes.add(ch("cap-" + i));
        String holder = ownerOf(f, "cap-holder", chashes.subList(0, cap));
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);

        // N = cap + 1: refused, nothing swept.
        List<String> tooMany = new ArrayList<>(chashes.subList(0, cap));
        tooMany.add(chashes.get(cap));
        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(),
                List.of(), List.of(), false, tooMany))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("300");
        assertThat(chunkExists(f.collection(), chashes.get(0))).isTrue();

        // N = cap: a sweep-only append sweeps all of them.
        var result = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(), List.of(), false, chashes.subList(0, cap)).response();
        assertThat(result).containsEntry("swept", cap).containsEntry("count", 0);
        assertThat(chunkExists(f.collection(), chashes.get(0))).isFalse();
        assertThat(chunkExists(f.collection(), chashes.get(cap - 1))).isFalse();
    }

    @Test
    void aSweepOnlyAppendToATombstonedDocument_isRefusedLikeAnAppendWithRows_andSweepsNothing() throws Exception {
        Fx f = fixture("tombsweep");
        String x = ch("tombsweep-x");
        String holder = ownerOf(f, "tombsweep-holder", List.of(x));
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);
        assertThat(repo.deleteDocument(TENANT, f.docId())).isEqualTo(1);

        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(),
                List.of(), List.of(), false, List.of(x)))
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);
        assertThatThrownBy(() -> repo.appendManifestChunks(TENANT, f.docId(), f.collection(), List.of(),
                null, null, List.of(x)))
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);
        assertThat(chunkExists(f.collection(), x)).as("nothing was swept on a tombstoned document's behalf").isTrue();
    }

    @Test
    void duplicateChashesInTheListAreSweptAndCountedOnce() throws Exception {
        Fx f = fixture("dup");
        String x = ch("dup-x");
        String holder = ownerOf(f, "dup-holder", List.of(x));
        repo.writeManifestMany(TENANT, List.of(doc(holder, List.of())), f.collection(), null, false);

        var result = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(), List.of(), false, List.of(x, x, x)).response();

        assertThat(result).containsEntry("swept", 1);
        assertThat(onlySweepDetail(result)).containsEntry("dropped", 1).containsEntry("kept", 0);
    }

    @Test
    void withoutSweepChashes_theResponseHasNoSweepFields() throws Exception {
        Fx f = fixture("nosweep");
        String x = ch("nosweep-x");
        var result = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(0, x)), List.of(chunk(x, "x")), false).response();
        assertThat(result).doesNotContainKeys("swept", "sweep_skipped", "sweep_detail");
    }
}
