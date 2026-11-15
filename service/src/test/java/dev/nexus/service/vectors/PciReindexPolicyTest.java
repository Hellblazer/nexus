// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.OptionalDouble;
import java.util.OptionalLong;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-230 Technical Design 3: the install reference a size rule compares an adopted index against. */
class PciReindexPolicyTest {

    private static PciCatalog.Anchor built(long rows, long bytes) {
        return new PciCatalog.Anchor(Instant.parse("2026-11-01T00:00:00Z"), rows, bytes, PciCatalog.ORIGIN_BUILT);
    }

    private static PciCatalog.Anchor adopted(long rows, long bytes) {
        return new PciCatalog.Anchor(Instant.parse("2026-11-01T00:00:00Z"), rows, bytes, PciCatalog.ORIGIN_ADOPTED);
    }

    @Test
    void theReference_isTheMedianBytesPerRowOverBuiltAnchors() {
        assertThat(PciReindexPolicy.installReference(1024,
            List.of(built(1000, 8_000_000), built(1000, 8_400_000), built(1000, 9_000_000)))).hasValue(8_400.0);
        // An even count is the mean of the middle two.
        assertThat(PciReindexPolicy.installReference(1024,
            List.of(built(1000, 8_000_000), built(1000, 9_000_000)))).hasValue(8_500.0);
        // At a dimension that has no fallback, the anchors are all there is.
        assertThat(PciReindexPolicy.installReference(384, List.of(built(100, 100_000)))).hasValue(1_000.0);
    }

    @Test
    void anAdoptedAnchor_isNotABuildFigure_andAnAnchorWithNoRowsOrBytesCarriesNoRatio() {
        assertThat(PciReindexPolicy.installReference(1024,
            List.of(adopted(1000, 99_000_000), built(1000, 8_000_000), built(0, 5), built(10, 0))))
            .hasValue(8_000.0);
    }

    @Test
    void withNoBuiltAnchor_1024Dimensions_fallBackTo8_2KbPerRow_andOtherDimensionsHaveNone() {
        assertThat(PciReindexPolicy.installReference(1024, List.of())).hasValue(8_200.0);
        assertThat(PciReindexPolicy.installReference(1024, List.of(adopted(1000, 9_000_000)))).hasValue(8_200.0);
        assertThat(PciReindexPolicy.installReference(768, List.of())).isEmpty();
        assertThat(PciReindexPolicy.installReference(384, List.of(adopted(1000, 9_000_000)))).isEmpty();
    }

    // -- the size bound (the recount rule) and the bulk fall ---------------------------------------------------------

    @Test
    void sizeFires_isTheSizeRule_forABuiltAnchorAndForAnAdoptedOneAgainstTheReference() {
        assertThat(PciReindexPolicy.sizeFires(built(1000, 8_000_000), 11_300_000, 1000, OptionalDouble.empty())).isTrue();
        assertThat(PciReindexPolicy.sizeFires(built(1000, 8_000_000), 11_100_000, 1000, OptionalDouble.empty())).isFalse();
        assertThat(PciReindexPolicy.sizeFires(adopted(1000, 1), 11_500_000, 1000, OptionalDouble.of(8_200))).isTrue();
        assertThat(PciReindexPolicy.sizeFires(adopted(1000, 1), 80_000_000, 1000, OptionalDouble.empty()))
            .as("no reference: off").isFalse();
        assertThat(PciReindexPolicy.sizeFires(built(1000, 8_000_000), 80_000_000, 0, OptionalDouble.empty()))
            .as("no rows is never a size").isFalse();
    }

    /** The stale count is a lower bound on the rows, so bytes over it is an upper bound on bytes per row. */
    @Test
    void aBoundBelowTheTrigger_meansTheTrueRatioIsBelowItToo_andOneAboveItLeavesTheCountToBeRetaken() {
        var anchor = built(1000, 8_000_000);   // 8000 bytes a row; the trigger is 11,200
        // 50% more bytes, the stale count still 1000: bound 1.5 >= 1.4, a count could change the verdict.
        assertThat(PciReindexPolicy.sizeFires(anchor, 12_000_000, 1000, OptionalDouble.empty())).isTrue();
        // 20% more bytes: bound 1.2 < 1.4, so the true ratio (rows can only have grown) is below it too.
        assertThat(PciReindexPolicy.sizeFires(anchor, 9_600_000, 1000, OptionalDouble.empty())).isFalse();
        for (long trueRows = 1000; trueRows <= 5000; trueRows += 500) {
            assertThat(PciReindexPolicy.sizeFires(anchor, 9_600_000, trueRows, OptionalDouble.empty()))
                .as("true rows %d", trueRows).isFalse();
        }
    }

    @Test
    void bulkFall_isTenPercentOrMoreBelowThePreviousCount_andNeverWithoutOne() {
        assertThat(PciReindexPolicy.bulkFall(OptionalLong.of(1000), 900)).isTrue();
        assertThat(PciReindexPolicy.bulkFall(OptionalLong.of(1000), 901)).isFalse();
        assertThat(PciReindexPolicy.bulkFall(OptionalLong.empty(), 1)).isFalse();
        assertThat(PciReindexPolicy.bulkFall(OptionalLong.of(0), 0)).isFalse();
        assertThat(PciReindexPolicy.bulkFall(OptionalLong.of(1000), 1500)).as("growth is not a fall").isFalse();
    }
}
