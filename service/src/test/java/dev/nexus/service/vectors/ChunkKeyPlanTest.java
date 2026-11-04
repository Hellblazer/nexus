// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.db.QuarantineOrigin;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

/**
 * nexus-6pfgb.43: {@link ChunkKeyPlan} is the engine's check on the allow-list a client renders from the Python key
 * table. The engine may not name the ROW keys, so it cannot compare against a set of its own; what it can refuse is a
 * list that contradicts the sets it does hold (CHUNK, DOCUMENT, POSITION, LEGACY).
 */
class ChunkKeyPlanTest {

    private static final List<String> ROW = List.of("origin_collection", "quarantined_at", "quarantined_by",
        "reaper_quarantined_at");
    private static final String TAXONOMY = "taxonomy" + "_".repeat(2);

    private static List<String> allowed(String... extra) {
        List<String> out = new ArrayList<>(ChunkMetadataKeys.CHUNK);
        out.addAll(ROW);
        out.addAll(List.of(extra));
        return out;
    }

    private static List<String> prefixes() {
        return List.of(TAXONOMY, QuarantineOrigin.PREFIX);
    }

    @Test
    void aListOfChunkAndRowKeysIsAPlan_andTheRowKeysAreWhatIsLeftAfterTheChunkKeys() {
        ChunkKeyPlan plan = ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by", "quarantined_at"));

        assertThat(plan.allowed()).containsExactlyInAnyOrderElementsOf(allowed()).isSorted();
        assertThat(plan.rowKeys()).containsExactlyElementsOf(ROW);
        assertThat(plan.bypassPrefixes()).contains(TAXONOMY, QuarantineOrigin.PREFIX);
        assertThat(plan.quarantineImplies()).containsExactly("quarantined_by", "quarantined_at");
    }

    @Test
    void aListThatDropsAChunkKeyIsRefused() {
        List<String> missing = new ArrayList<>(allowed());
        missing.remove("chunk_text_hash");
        assertThatThrownBy(() -> ChunkKeyPlan.of(missing, prefixes(), List.of()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("chunk_text_hash");
    }

    @Test
    void aListThatNamesADocumentPositionOrLegacyKeyIsRefused() {
        for (String retired : List.of("title", "section_title", "doc_id")) {
            assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(retired), prefixes(), List.of()))
                .as(retired).isInstanceOf(IllegalArgumentException.class).hasMessageContaining(retired);
        }
        // The three sets really do cover those keys, so the three probes above are not vacuous.
        assertThat(ChunkMetadataKeys.DOCUMENT).contains("title");
        assertThat(ChunkMetadataKeys.POSITION).contains("section_title");
        assertThat(ChunkMetadataKeys.LEGACY).contains("doc_id");
    }

    @Test
    void aKeyThatIsNotAKeyShapeIsRefused_soNoValueCanReachTheDdlAsAnythingButAKey() {
        for (String bad : List.of("Title", "a b", "x;drop", "", "1abc")) {
            assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(bad), prefixes(), List.of()))
                .as(bad).isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void thePrefixesMustNameQuarantine_andEachMustEndInASeparator() {
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(TAXONOMY), List.of()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(QuarantineOrigin.PREFIX);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(QuarantineOrigin.PREFIX, "docs"), List.of()))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("docs");
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(QuarantineOrigin.PREFIX, "x'; --" + "_".repeat(2)),
            List.of())).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theQuarantineImplicationMustBeTwoDistinctRowKeys() {
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by", "quarantined_by")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by", "chunk_text_hash")))
            .as("a CHUNK key is not an engine-owned row key")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("chunk_text_hash");
        assertThat(ChunkKeyPlan.of(allowed(), prefixes(), null).quarantineImplies()).isEmpty();
    }
}
