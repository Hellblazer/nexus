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
 * nexus-6pfgb.43: {@link ChunkKeyPlan} is the engine's check on the plan a client renders from the Python key table.
 * The engine may not name the ROW keys, but it holds the exact answer anyway: the ROW keys by digest, the bypass set
 * exactly, the quarantine pair by digest. A plan that differs in any of them is refused, because the sweep strips every
 * key the plan does not allow (irreversible) and the CHECK exempts every prefix the plan names (for good).
 */
class ChunkKeyPlanTest {

    private static final List<String> ROW = List.of("origin_collection", "quarantined_at", "quarantined_by",
        "reaper_quarantined_at");
    private static final String TAXONOMY = "taxonomy" + "_".repeat(2);
    private static final List<String> PAIR = List.of("quarantined_by", "quarantined_at");

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
        ChunkKeyPlan plan = ChunkKeyPlan.of(allowed(), prefixes(), PAIR);

        assertThat(plan.allowed()).containsExactlyInAnyOrderElementsOf(allowed()).isSorted();
        assertThat(plan.rowKeys()).containsExactlyElementsOf(ROW);
        assertThat(plan.bypassPrefixes()).containsExactly(QuarantineOrigin.PREFIX, TAXONOMY);
        assertThat(plan.quarantineImplies()).containsExactly("quarantined_by", "quarantined_at");
    }

    @Test
    void theEnginesTaxonomyPrefixIsTheCollectionGrammarsContentTypeAndSeparator() {
        assertThat(ChunkKeyPlan.TAXONOMY_PREFIX).isEqualTo(TAXONOMY);
        assertThat(ChunkKeyPlan.BYPASS_PREFIXES).containsExactly(QuarantineOrigin.PREFIX, TAXONOMY);
    }

    @Test
    void aListThatDropsAChunkKeyIsRefused() {
        List<String> missing = new ArrayList<>(allowed());
        missing.remove("chunk_text_hash");
        assertThatThrownBy(() -> ChunkKeyPlan.of(missing, prefixes(), PAIR))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("chunk_text_hash");
    }

    @Test
    void aListThatNamesADocumentPositionOrLegacyKeyIsRefused() {
        for (String retired : List.of("title", "section_title", "doc_id")) {
            assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(retired), prefixes(), PAIR))
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
            assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(bad), prefixes(), PAIR))
                .as(bad).isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void anUnknownKeyIsRefused_theCheckWouldAdmitIt() {
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed("not_in_the_table"), prefixes(), PAIR))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("engine-owned (ROW) keys");
    }

    @Test
    void aDroppedRowKeyIsRefused_theSweepWouldStripItFromEveryRow() {
        for (String dropped : ROW) {
            List<String> shorter = new ArrayList<>(allowed());
            shorter.remove(dropped);
            // The pair names two row keys; keep them valid unless one of them is the dropped key.
            List<String> pair = shorter.containsAll(PAIR) ? PAIR : List.of("quarantined_by", "origin_collection");
            assertThatThrownBy(() -> ChunkKeyPlan.of(shorter, prefixes(), pair))
                .as("dropping " + dropped).isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void theBypassSetIsExactlyQuarantineAndTaxonomy() {
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(QuarantineOrigin.PREFIX), PAIR))
            .as("taxonomy omitted: the sweep would strip taxonomy rows")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(TAXONOMY);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(TAXONOMY), PAIR))
            .as("quarantine omitted").isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining(QuarantineOrigin.PREFIX);
        String extra = "knowledge" + "_".repeat(2);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(TAXONOMY, QuarantineOrigin.PREFIX, extra), PAIR))
            .as("an extra prefix would exempt a whole collection family from the CHECK")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(extra);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(QuarantineOrigin.PREFIX, "docs"), PAIR))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("docs");
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(QuarantineOrigin.PREFIX, "x'; --" + "_".repeat(2)),
            PAIR)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), List.of(TAXONOMY, TAXONOMY, QuarantineOrigin.PREFIX), PAIR))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("twice");
    }

    @Test
    void theQuarantineImplicationIsRequired_twoDistinctRowKeys_inThePinnedOrder() {
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of()))
            .as("missing").isInstanceOf(IllegalArgumentException.class).hasMessageContaining("quarantine_implies");
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), null))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by", "quarantined_by")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_by", "chunk_text_hash")))
            .as("a CHUNK key is not an engine-owned row key")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("chunk_text_hash");
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(), List.of("quarantined_at", "quarantined_by")))
            .as("the reverse implication is a different CHECK")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("[by, at]");
        assertThatThrownBy(() -> ChunkKeyPlan.of(allowed(), prefixes(),
            List.of("origin_collection", "quarantined_at")))
            .as("a different pair of row keys").isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theDigestIsTheGeneratorsOwn_sortedKeysJoinedByANewline() {
        assertThat(ChunkKeyPlan.digest(ROW)).isEqualTo(ChunkMetadataKeys.ROW_KEYS_SHA256);
        assertThat(ChunkKeyPlan.digest(PAIR)).isEqualTo(ChunkMetadataKeys.QUARANTINE_IMPLIES_SHA256);
        assertThat(ChunkKeyPlan.digest(List.of("a", "b"))).isNotEqualTo(ChunkKeyPlan.digest(List.of("ab")));
    }
}
