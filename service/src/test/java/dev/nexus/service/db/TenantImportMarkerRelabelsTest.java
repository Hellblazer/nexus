// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.db.TenantImportReport.Relabel;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): the restore marker lists the collections the restore relabelled, bounded at
 * {@link TenantImportLoad#MARKER_RELABELS_MAX} entries, with the whole count beside the list so a client can say how many it
 * could not name. The end-to-end marker is asserted in {@code Rdr229RestoreMintedRelabelTest}.
 */
class TenantImportMarkerRelabelsTest {

    private static List<Relabel> relabels(int n) {
        List<Relabel> out = new ArrayList<>();
        for (int i = 0; i < n; i++) out.add(new Relabel("c" + i, "c" + (n + i)));
        return out;
    }

    @Test
    void aListWithinTheBoundIsWrittenWhole() {
        ObjectNode marker = new ObjectMapper().createObjectNode();
        TenantImportLoad.putRelabels(marker, relabels(3), TenantImportLoad.MARKER_RELABELS_MAX);
        assertThat(marker.get("collections_relabelled_count").asInt()).isEqualTo(3);
        assertThat(marker.get("collections_relabelled")).hasSize(3);
        assertThat(marker.get("collections_relabelled").get(2).get("target_label").asText()).isEqualTo("c5");
    }

    @Test
    void aLongerListIsCutAtTheBoundWithItsWholeCount() {
        ObjectNode marker = new ObjectMapper().createObjectNode();
        int n = TenantImportLoad.MARKER_RELABELS_MAX + 7;
        TenantImportLoad.putRelabels(marker, relabels(n), TenantImportLoad.MARKER_RELABELS_MAX);
        assertThat(marker.get("collections_relabelled_count").asInt()).isEqualTo(n);
        assertThat(marker.get("collections_relabelled")).hasSize(TenantImportLoad.MARKER_RELABELS_MAX);
        assertThat(marker.get("collections_relabelled").get(0).get("label").asText()).as("the first entries are kept").isEqualTo("c0");
    }

    @Test
    void aRestoreThatRelabelledNothingWritesAnEmptyListAndACountOfZero() {
        ObjectNode marker = new ObjectMapper().createObjectNode();
        TenantImportLoad.putRelabels(marker, List.of(), TenantImportLoad.MARKER_RELABELS_MAX);
        assertThat(marker.get("collections_relabelled_count").asInt()).isZero();
        assertThat(marker.get("collections_relabelled")).isEmpty();
    }
}
