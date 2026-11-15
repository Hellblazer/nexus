// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

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
}
