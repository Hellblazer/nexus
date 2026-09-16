// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-a6mon (code-review finding on a990fe8f1): the route's {@code row_limit}
 * dispatch. Absent selects the unbounded sweep; present must be positive, and a
 * present non-positive value is refused at the route (400) rather than folded
 * into "absent" and handed to the unbounded transaction. Same precedent as
 * {@link VectorHandlerSampleLimitClampTest}: the repository test cannot prove
 * the handler's routing, so the routing is a static method with its own pin.
 */
class VectorHandlerRowLimitRoutingTest {

    @Test
    void absentRowLimit_isTheUnboundedSweep() {
        assertThat(VectorHandler.resolveRowLimit(Map.of("collection", "c"))).isEqualTo(0);
    }

    @Test
    void positiveRowLimit_isPassedThrough() {
        assertThat(VectorHandler.resolveRowLimit(Map.of("row_limit", 2000))).isEqualTo(2000);
        assertThat(VectorHandler.resolveRowLimit(Map.of("row_limit", 1))).isEqualTo(1);
        // JSON numbers arrive as whatever the parser minted; a long is still a number.
        assertThat(VectorHandler.resolveRowLimit(Map.of("row_limit", 500L))).isEqualTo(500);
    }

    @Test
    void presentZero_isRefused_neverUnbounded() {
        assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", 0)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("row_limit must be >= 1");
    }

    @Test
    void presentNegative_isRefused_neverUnbounded() {
        assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", -5)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("row_limit must be >= 1");
    }

    @Test
    void presentNull_isRefused_notTreatedAsAbsent() {
        Map<String, Object> body = new HashMap<>();
        body.put("row_limit", null);
        assertThatThrownBy(() -> VectorHandler.resolveRowLimit(body))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("row_limit must be a positive integer");
    }

    @Test
    void presentNonNumber_isRefused() {
        assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", "2000")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("row_limit must be a positive integer");
    }
}
