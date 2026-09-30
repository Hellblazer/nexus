// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-223 (bead nexus-z0o2p.13) -- the wire parsing of the combined routes' metadata write mode:
 * absent fields are the replace behaviour every earlier client gets; malformed or unsafe values
 * are refused (400) rather than guessed at.
 */
class CatalogHandlerMetadataModeTest {

    @Test
    void absent_isReplace() {
        var m = CatalogHandler.parseMetadataMode(Map.of());
        assertThat(m.merge()).isFalse();
        assertThat(m.deleteKeys()).isEmpty();
    }

    @Test
    void mergeWithKeys_parses() {
        var m = CatalogHandler.parseMetadataMode(
            Map.of("metadata_merge", true, "metadata_delete_keys", List.of("a", "b")));
        assertThat(m.merge()).isTrue();
        assertThat(m.deleteKeys()).containsExactly("a", "b");
    }

    @Test
    void mergeFalseExplicitly_isReplace() {
        assertThat(CatalogHandler.parseMetadataMode(Map.of("metadata_merge", false)).merge()).isFalse();
    }

    @Test
    void deleteKeysWithoutMerge_isRefused() {
        assertThatThrownBy(() -> CatalogHandler.parseMetadataMode(Map.of("metadata_delete_keys", List.of("a"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("metadata_merge");
    }

    @Test
    void malformedValues_areRefused() {
        assertThatThrownBy(() -> CatalogHandler.parseMetadataMode(Map.of("metadata_merge", "true")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> CatalogHandler.parseMetadataMode(
            Map.of("metadata_merge", true, "metadata_delete_keys", "a")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> CatalogHandler.parseMetadataMode(
            Map.of("metadata_merge", true, "metadata_delete_keys", List.of(" "))))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> CatalogHandler.parseMetadataMode(
            Map.of("metadata_merge", true, "metadata_delete_keys", List.of(1))))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void tooManyKeys_isRefused() {
        List<String> keys = new ArrayList<>();
        for (int i = 0; i < 65; i++) keys.add("k" + i);
        assertThatThrownBy(() -> CatalogHandler.parseMetadataMode(
            Map.of("metadata_merge", true, "metadata_delete_keys", keys)))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("limit");
    }
}
