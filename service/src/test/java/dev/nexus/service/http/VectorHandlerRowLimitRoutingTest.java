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
    void aFractionalRowLimit_isRefused_neverTruncated() {
        // Number.intValue() took 1.5 as 1 and 0.5 as 0 (then "must be >= 1" named the wrong value).
        for (Object bad : new Object[] {1.5, 0.5, 2.0000001, Double.NaN}) {
            assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", bad)))
                .as("row_limit=%s", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("row_limit must be a positive integer");
        }
    }

    @Test
    void aRowLimitPastTheIntRange_isRefused_neverWrapped() {
        // intValue() wrapped 4294967297 to 1 and 4294967296 to 0 (the same wrap resolveFloor guards for
        // floor_min_chunks).
        for (Object bad : new Object[] {4294967297L, 4294967296L, 2147483648L, Long.MAX_VALUE,
                                        new java.math.BigInteger("99999999999999999999")}) {
            assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", bad)))
                .as("row_limit=%s", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("row_limit must be <= " + Integer.MAX_VALUE);
        }
        assertThat(VectorHandler.resolveRowLimit(Map.of("row_limit", (long) Integer.MAX_VALUE)))
            .as("the int ceiling itself is a number").isEqualTo(Integer.MAX_VALUE);
    }

    @Test
    void aNegativeRowLimitPastTheIntRange_isStillTheBelowOneError() {
        assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", -4294967297L)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("row_limit must be >= 1");
    }

    @Test
    void anIntegralFloat_isAccepted() {
        // A JSON 2.0 is the integer 2; only a fractional part is refused.
        assertThat(VectorHandler.resolveRowLimit(Map.of("row_limit", 2.0))).isEqualTo(2);
    }

    @Test
    void presentNonNumber_isRefused() {
        assertThatThrownBy(() -> VectorHandler.resolveRowLimit(Map.of("row_limit", "2000")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("row_limit must be a positive integer");
    }

    // ── RDR-222 Amendment A1 (nexus-1579m): the bounded expire form's route pieces ──

    @Test
    void expireRowLimitCeiling_isTheAuditCeiling_theFunctionsOwnClamp() {
        // vectors-035 clamps a page at 5,000 distinct chashes so its audit row lists every chash it deleted;
        // the route echoes row_limit, so the two numbers must agree. The function side is pinned by
        // GcExpireQuarantineBoundedTest.anOversizedRowLimitIsClamped...
        assertThat(VectorHandler.GC_EXPIRE_ROW_LIMIT_MAX).isEqualTo(5_000)
            .isEqualTo(dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES);
    }

    @Test
    void boundedExpireBody_carriesANullRefused_andFixedKeyOrder() throws Exception {
        var counted = VectorHandler.boundedExpireBody(
            new dev.nexus.service.vectors.PgVectorRepository.ExpireBoundedOutcome(7L, 3L, true, false), 2000);
        var uncounted = VectorHandler.boundedExpireBody(
            new dev.nexus.service.vectors.PgVectorRepository.ExpireBoundedOutcome(7L, null, false, false), 5000);

        var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
        assertThat(mapper.writeValueAsString(counted))
            .isEqualTo("{\"expired\":7,\"refused\":3,\"more\":true,\"row_limit\":2000,\"floor_refused\":false}");
        assertThat(mapper.writeValueAsString(uncounted))
            .as("a null refused is JSON null, not absent and not 0")
            .isEqualTo("{\"expired\":7,\"refused\":null,\"more\":false,\"row_limit\":5000,\"floor_refused\":false}");
    }
}
