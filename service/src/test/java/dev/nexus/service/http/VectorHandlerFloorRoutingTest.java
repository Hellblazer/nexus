// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-wbfpw.52: the route's optional floor fields. Same precedent as {@link VectorHandlerRowLimitRoutingTest}:
 * the routing is a static method with its own pin; {@code GcQuarantineOrphansFloorRouteTest} drives the same fields
 * over real HTTP against the engine's function.
 */
class VectorHandlerFloorRoutingTest {

    @Test
    void noFraction_isNoFloor_whateverForceSays() {
        assertThat(VectorHandler.resolveFloor(Map.of("collection", "c")).given()).isFalse();
        var withNull = new LinkedHashMap<String, Object>();
        withNull.put("floor_fraction", null);
        assertThat(VectorHandler.resolveFloor(withNull).given()).as("an explicit null is no floor").isFalse();
        assertThat(VectorHandler.resolveFloor(Map.of("force", true)).given()).isFalse();
        assertThat(VectorHandler.resolveFloor(Map.of()).echo()).isEqualTo(Map.of("given", false));
    }

    @Test
    void aFractionAlone_takesTheGcFamilysMinimum() {
        var f = VectorHandler.resolveFloor(Map.of("floor_fraction", 0.25));
        assertThat(f.given()).isTrue();
        assertThat(f.fraction()).isEqualTo(0.25);
        assertThat(f.minChunks()).isEqualTo(100).isEqualTo(VectorHandler.GC_FLOOR_MIN_CHUNKS_DEFAULT);
        assertThat(f.force()).isFalse();
    }

    @Test
    void integerJsonNumbersAreAccepted_andTheEchoCarriesEverything() {
        var f = VectorHandler.resolveFloor(Map.of("floor_fraction", 1, "floor_min_chunks", 0L, "force", true));
        assertThat(f.fraction()).isEqualTo(1.0);
        assertThat(f.minChunks()).isZero();
        assertThat(f.echo()).isEqualTo(Map.of("given", true, "fraction", 1.0, "min_chunks", 0, "force", true));
    }

    @Test
    void aMalformedFloor_isRefused_neverDropped() {
        for (Map<String, Object> bad : java.util.List.of(
                Map.<String, Object>of("floor_fraction", 1.01),
                Map.<String, Object>of("floor_fraction", -0.01),
                Map.<String, Object>of("floor_fraction", Double.NaN),
                Map.<String, Object>of("floor_fraction", "0.5"),
                Map.<String, Object>of("floor_fraction", 0.5, "floor_min_chunks", -1),
                Map.<String, Object>of("floor_fraction", 0.5, "floor_min_chunks", 1.5),
                Map.<String, Object>of("floor_fraction", 0.5, "floor_min_chunks", "ten"),
                Map.<String, Object>of("floor_min_chunks", 10),
                Map.<String, Object>of("force", "true"))) {
            assertThatThrownBy(() -> VectorHandler.resolveFloor(bad))
                .as("%s", bad).isInstanceOf(IllegalArgumentException.class);
        }
    }
}
