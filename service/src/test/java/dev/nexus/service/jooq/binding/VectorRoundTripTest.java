// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.jooq.binding;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/**
 * nexus-xtmtf review finding: {@link VectorBinding}'s READ path
 * ({@link Vector#parse}) had zero direct coverage — every production use
 * of the typed embedding field is write-side, so a parse bug would only
 * surface when a future caller wires the field into a SELECT. Pin the
 * text-form round-trip explicitly, including the scientific-notation and
 * edge-magnitude forms PG's float4 text output can produce.
 */
class VectorRoundTripTest {

    @Test
    void toString_parse_roundTripsExactly() {
        float[] v = {1.5f, -2.25f, 0f, 3.14159f};
        Vector out = Vector.parse(Vector.of(v).toString());
        assertThat(out.floats()).containsExactly(v);
    }

    @Test
    void parse_scientificNotation_fromPgFloat4Text() {
        // PG float4 text output uses forms like 1e-05 / -3.4028235e+38.
        Vector out = Vector.parse("[1e-05,-3.4028235e+38,3.4028235e+38]");
        assertThat(out.floats()).containsExactly(1e-05f, -Float.MAX_VALUE, Float.MAX_VALUE);
    }

    @Test
    void parse_toleratesWhitespace() {
        assertThat(Vector.parse(" [1.0, 2.0 ,3.0] ").floats())
            .containsExactly(1.0f, 2.0f, 3.0f);
    }

    @Test
    void parse_empty_isZeroLength() {
        assertThat(Vector.parse("[]").floats()).isEmpty();
    }

    @Test
    void of_null_isNull() {
        assertThat(Vector.of(null)).isNull();
    }

    @Test
    void equals_isValueBased() {
        assertThat(Vector.of(new float[]{1f, 2f}))
            .isEqualTo(Vector.of(new float[]{1f, 2f}))
            .isNotEqualTo(Vector.of(new float[]{1f, 3f}));
    }

    @Test
    void roundTrip_preservesFloatPrecisionAtEdges() {
        float[] edge = {Float.MIN_VALUE, Float.MIN_NORMAL, 1.0000001f, -0.0f};
        assertThat(Vector.parse(Vector.of(edge).toString()).floats())
            .containsExactly(edge);
    }
}
