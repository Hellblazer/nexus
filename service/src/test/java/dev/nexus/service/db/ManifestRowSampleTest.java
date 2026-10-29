// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-jypmj.6: the per-document row sample behind {@code max_rows_per_doc} on
 * {@code POST /v1/catalog/manifest/get_many}. Pure arithmetic, no database.
 */
class ManifestRowSampleTest {

    @Test
    void aDocumentWithinTheLimitIsReturnedWhole() {
        assertThat(ManifestRowSample.indexes(5, 5)).containsExactly(0, 1, 2, 3, 4);
        assertThat(ManifestRowSample.indexes(3, 10)).containsExactly(0, 1, 2);
        assertThat(ManifestRowSample.indexes(0, 4)).isEmpty();
    }

    @Test
    void aLongerDocumentGivesAnEvenSampleThatIncludesBothEnds() {
        assertThat(ManifestRowSample.indexes(9, 4)).containsExactly(0, 2, 5, 8);
        assertThat(ManifestRowSample.indexes(10, 2)).containsExactly(0, 9);
        assertThat(ManifestRowSample.indexes(10, 3)).containsExactly(0, 4, 9);
    }

    @Test
    void aLimitOfOneIsTheFirstRow() {
        assertThat(ManifestRowSample.indexes(7, 1)).containsExactly(0);
    }

    @Test
    void theSampleIsStrictlyIncreasingInRangeAndExactlyLimitLong() {
        for (int n = 1; n <= 120; n++) {
            for (int limit = 1; limit <= n + 2; limit++) {
                List<Integer> got = new ArrayList<>(ManifestRowSample.indexes(n, limit));
                assertThat(got).as("n=%d limit=%d", n, limit).hasSize(Math.min(n, limit));
                for (int i = 0; i < got.size(); i++) {
                    assertThat(got.get(i)).isBetween(0, n - 1);
                    if (i > 0) assertThat(got.get(i)).isGreaterThan(got.get(i - 1));
                }
                if (n > limit && limit >= 2) assertThat(got.getLast()).isEqualTo(n - 1);
            }
        }
    }
}
