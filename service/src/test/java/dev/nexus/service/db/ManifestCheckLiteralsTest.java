// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;

/**
 * nexus-6pfgb.42: the wire literals of each manifest check, written out here and never read from {@link ManifestCheck},
 * so a typo in the enum cannot pass by agreeing with itself. The route path and the constraint name are what the Python
 * client and the operator's runbook name; the lock key is what keeps two steps from holding each other up. No database.
 */
class ManifestCheckLiteralsTest {

    @Test
    void theChunkIndexCheck_keepsTheLiteralsTheReleasedRoutesAndTheRunbookName() {
        assertThat(ManifestCheck.CHUNK_INDEX.path()).isEqualTo("/v1/admin/manifest-chunk-index-check");
        assertThat(ManifestCheck.CHUNK_INDEX.constraint()).isEqualTo("catalog_document_chunks_chunk_index_position_check");
        assertThat(ManifestCheck.CHUNK_INDEX.lockKey()).isEqualTo(0x6d6678696478636bL);
        assertThat(ManifestCheck.CHUNK_INDEX_ADVISORY_LOCK_KEY).isEqualTo(ManifestCheck.CHUNK_INDEX.lockKey());
        assertThat(asTag(ManifestCheck.CHUNK_INDEX.lockKey())).isEqualTo("mfxidxck");
        assertThat(ManifestCheck.CHUNK_INDEX.applicationName()).isEqualTo("nexus-manifest-chunk-index-check");
        assertThat(ManifestCheck.CHUNK_INDEX.event()).isEqualTo("manifest_chunk_index_check");
    }

    @Test
    void theLabelsCheck_namesItsOwnLiterals() {
        assertThat(ManifestCheck.LABELS.path()).isEqualTo("/v1/admin/manifest-labels-check");
        assertThat(ManifestCheck.LABELS.constraint()).isEqualTo("catalog_document_chunks_labels_written_check");
        assertThat(ManifestCheck.LABELS.lockKey()).isEqualTo(0x6d666c626c73636bL);
        assertThat(ManifestCheck.LABELS_ADVISORY_LOCK_KEY).isEqualTo(ManifestCheck.LABELS.lockKey());
        assertThat(asTag(ManifestCheck.LABELS.lockKey())).isEqualTo("mflblsck");
        assertThat(ManifestCheck.LABELS.applicationName()).isEqualTo("nexus-manifest-labels-check");
        assertThat(ManifestCheck.LABELS.event()).isEqualTo("manifest_labels_check");
    }

    @Test
    void theCheckExpressionsAreTheDocumentedPredicates() {
        assertThat(ManifestCheck.CHUNK_INDEX.checkExpression()).isEqualTo("\"chunk_index\" = \"position\"");
        assertThat(ManifestCheck.LABELS.checkExpression()).isEqualTo(
            "(\"labels_written\" or (\"section_title\" is null and \"section_type\" is null and \"page_number\" is null))");
    }

    private static String asTag(long key) {
        byte[] bytes = new byte[8];
        for (int i = 0; i < 8; i++) {
            bytes[i] = (byte) (key >>> (56 - 8 * i));
        }
        return new String(bytes, StandardCharsets.US_ASCII);
    }
}
