// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.6 and P3.8 (nexus-p2m1x.26, nexus-p2m1x.28): the minted label form, {@code c} followed by
 * decimal digits, lives in one place, so the label rule that reserves it and the minter that renders it
 * cannot drift apart.
 */
class CollectionLabelTest {

    @ParameterizedTest
    @ValueSource(strings = {"c1", "c0", "c42", "c007", "c1234567890123456789"})
    void theMintedFormIsCFollowedByAsciiDigitsWithLeadingZerosReserved(String label) {
        assertThat(CollectionLabel.isMintedForm(label)).isTrue();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", "c", "C1", "cc1", "c1a", "c-1", "c 1", " c1", "c1 ", "c1\n", "c١",
        "c１", "docs__1-1__voyage-context-3__v1", "knowledge", "1c"})
    void anythingElseIsNotTheMintedForm(String label) {
        assertThat(CollectionLabel.isMintedForm(label)).isFalse();
    }

    @Test
    void aNullLabelIsNotTheMintedForm() {
        assertThat(CollectionLabel.isMintedForm(null)).isFalse();
    }

    @Test
    void mintedLabelRendersCAndTheIdWithNoPadding() {
        assertThat(CollectionLabel.mintedLabel(1)).isEqualTo("c1");
        assertThat(CollectionLabel.mintedLabel(7)).isEqualTo("c7");
        assertThat(CollectionLabel.mintedLabel(Long.MAX_VALUE)).isEqualTo("c" + Long.MAX_VALUE);
    }

    @ParameterizedTest
    @ValueSource(longs = {0, -1, Long.MIN_VALUE})
    void mintedLabelRefusesAnIdThatIsNotPositive(long id) {
        assertThatThrownBy(() -> CollectionLabel.mintedLabel(id)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void everyRenderedLabelIsOfTheMintedForm() {
        long[] ids = {1, 2, 9, 10, 99, 100, 12345, 1L << 31, 1L << 40, Long.MAX_VALUE - 1, Long.MAX_VALUE};
        for (long id : ids) {
            assertThat(CollectionLabel.isMintedForm(CollectionLabel.mintedLabel(id))).as("id %d", id).isTrue();
        }
        java.util.Random rnd = new java.util.Random(229L);
        for (int i = 0; i < 1000; i++) {
            long id = 1 + (rnd.nextLong() & Long.MAX_VALUE) % (Long.MAX_VALUE - 1);
            assertThat(CollectionLabel.isMintedForm(CollectionLabel.mintedLabel(id))).as("id %d", id).isTrue();
        }
    }
}
