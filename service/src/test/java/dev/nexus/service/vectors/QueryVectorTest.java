// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/** nexus-4lnn1.1.3: the admission rule's pure half. */
class QueryVectorTest {

    private static final float[] V = {1f, 0f};

    @Test
    void aSingleArmIsAdmittedToItself() {
        assertThat(QueryVector.alone("m", V, "x").collection()).isEqualTo("x");
    }

    @Test
    void theNearestArmWins_byTheSmallestDistance() {
        var q = QueryVector.nearestOf("m", V, List.of("a", "b", "c"), Arrays.asList(0.30, 0.10, 0.20));
        assertThat(q.collection()).isEqualTo("b");
        assertThat(q.model()).isEqualTo("m");
        assertThat(q.vector()).isSameAs(V);
    }

    @Test
    void aTieGoesToTheFirstArm() {
        assertThat(QueryVector.nearestOf("m", V, List.of("a", "b"), List.of(0.1, 0.1)).collection()).isEqualTo("a");
    }

    @Test
    void anArmWithNoRowOrAFailureIsNeverNearest() {
        var q = QueryVector.nearestOf("m", V, List.of("a", "b", "c"), Arrays.asList(null, 0.9, null));
        assertThat(q.collection()).isEqualTo("b");
    }

    @Test
    void whenNoArmReturnedARowNothingIsAdmitted() {
        assertThat(QueryVector.nearestOf("m", V, List.of("a", "b"), Arrays.asList(null, null)).collection()).isNull();
    }

    @Test
    void theDigestIsTheSha256OfTheValuesInOrder_andDiffersByValueAndOrder() {
        byte[] d = PciQueryRing.digest(new float[] {1f, 2f});
        assertThat(d).hasSize(32);
        assertThat(PciQueryRing.digest(new float[] {1f, 2f})).isEqualTo(d);
        assertThat(PciQueryRing.digest(new float[] {2f, 1f})).isNotEqualTo(d);
        assertThat(PciQueryRing.digest(new float[] {1f, 2.0000002f})).isNotEqualTo(d);
    }

    @Test
    void sanitizeMasksAVectorLiteralAndBoundsTheMessage() {
        var e = new IllegalStateException("bad vector \"[0.1,0.2,0.3,0.4,0.5]\" and then [1,2,3,4,5,6 cut");
        String out = PciQueryRing.sanitize(e);
        assertThat(out).startsWith("IllegalStateException: ").doesNotContain("0.1,0.2").doesNotContain("1,2,3");
        assertThat(PciQueryRing.sanitize(new RuntimeException("x".repeat(500))).length()).isLessThan(260);
    }
}
