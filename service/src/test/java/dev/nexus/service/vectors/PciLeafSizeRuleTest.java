// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciLeafSizeRule.Verdict;
import org.junit.jupiter.api.Test;

import java.util.OptionalDouble;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** RDR-230 P2.3 (nexus-4lnn1.1.10): the leaf size rule, 1.5 x the install reference. */
class PciLeafSizeRuleTest {

    /** 8.2 KB a row, the RDR-227 A3 figure at 1024 dimensions. */
    private static final OptionalDouble REF_1024 = OptionalDouble.of(8_200);

    @Test
    void atExactlyOneAndAHalfTimesTheReference_fires() {
        // 1,000 rows x 8,200 x 1.5 = 12,300,000 bytes.
        assertThat(PciLeafSizeRule.evaluate(12_300_000, 1_000, REF_1024)).isEqualTo(Verdict.FIRE);
    }

    @Test
    void aboveTheMultiple_fires() {
        assertThat(PciLeafSizeRule.evaluate(20_000_000, 1_000, REF_1024)).isEqualTo(Verdict.FIRE);
    }

    @Test
    void oneByteBelowTheMultiple_isQuiet() {
        assertThat(PciLeafSizeRule.evaluate(12_299_999, 1_000, REF_1024)).isEqualTo(Verdict.QUIET);
    }

    @Test
    void aFreshBuild_atTheReference_isQuiet() {
        assertThat(PciLeafSizeRule.evaluate(8_200_000, 1_000, REF_1024)).isEqualTo(Verdict.QUIET);
    }

    @Test
    void theMultipleIsAppliedToTheReferenceItIsGiven_notToThe1024Figure() {
        OptionalDouble ref768 = OptionalDouble.of(6_000);
        assertThat(PciLeafSizeRule.evaluate(9_000_000, 1_000, ref768)).isEqualTo(Verdict.FIRE);
        assertThat(PciLeafSizeRule.evaluate(8_999_999, 1_000, ref768)).isEqualTo(Verdict.QUIET);
    }

    @Test
    void noReference_isUnanchored_atAnySize_includingTheDimensionWithNoAnchors384() {
        // 384 dimensions, no built anchors on this install: there is no reference, so the rule is inactive.
        OptionalDouble none = OptionalDouble.empty();
        assertThat(PciLeafSizeRule.evaluate(1L << 40, 1_000, none)).isEqualTo(Verdict.UNANCHORED);
        assertThat(PciLeafSizeRule.evaluate(1, 1_000, none)).isEqualTo(Verdict.UNANCHORED);
        assertThat(PciLeafSizeRule.evaluate(0, 0, none)).as("unanchored is decided before rows").isEqualTo(Verdict.UNANCHORED);
    }

    @Test
    void aLeafWithNoLiveRows_isQuiet_whateverItsIndexWeighs() {
        assertThat(PciLeafSizeRule.evaluate(50_000_000, 0, REF_1024)).isEqualTo(Verdict.QUIET);
        assertThat(PciLeafSizeRule.evaluate(0, 0, REF_1024)).isEqualTo(Verdict.QUIET);
    }

    @Test
    void anEmptyIndexOnALeafWithRows_isQuiet() {
        assertThat(PciLeafSizeRule.evaluate(0, 1_000, REF_1024)).isEqualTo(Verdict.QUIET);
    }

    @Test
    void largeFigures_doNotOverflow() {
        // 2^40 bytes over 2^31 rows is 512 bytes a row, far under 8,200 x 1.5.
        assertThat(PciLeafSizeRule.evaluate(1L << 40, 1L << 31, REF_1024)).isEqualTo(Verdict.QUIET);
        assertThat(PciLeafSizeRule.evaluate(Long.MAX_VALUE, 1L << 31, REF_1024)).isEqualTo(Verdict.FIRE);
    }

    @Test
    void negativeFigures_andAnUnusableReference_areRefused() {
        assertThatThrownBy(() -> PciLeafSizeRule.evaluate(-1, 1, REF_1024)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciLeafSizeRule.evaluate(1, -1, REF_1024)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciLeafSizeRule.evaluate(1, 1, OptionalDouble.of(0)))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciLeafSizeRule.evaluate(1, 1, OptionalDouble.of(-5)))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciLeafSizeRule.evaluate(1, 1, OptionalDouble.of(Double.NaN)))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciLeafSizeRule.evaluate(1, 1, OptionalDouble.of(Double.POSITIVE_INFINITY)))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
