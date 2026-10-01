// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-192 Step 8 (bead nexus-wbfpw.17): the request parsing of {@code POST /v1/vectors/reapable}
 * that needs no database. The route itself is covered over HTTP in {@code VectorHandlerReapableRouteTest}.
 */
class VectorHandlerReapableRoutingTest {

    @Test
    void graceSecondsAbsentOrNull_isTheEngineDefault() {
        assertThat(VectorHandler.parseGraceSeconds(null)).isNull();
    }

    @Test
    void graceSecondsIsAWholeNumberInRange() {
        assertThat(VectorHandler.parseGraceSeconds(0)).isEqualTo(0L);
        assertThat(VectorHandler.parseGraceSeconds(3600)).isEqualTo(3600L);
        assertThat(VectorHandler.parseGraceSeconds(2_592_000L)).isEqualTo(2_592_000L);
        assertThat(VectorHandler.parseGraceSeconds(VectorHandler.MAX_REAPABLE_GRACE_SECONDS))
            .isEqualTo(VectorHandler.MAX_REAPABLE_GRACE_SECONDS);
    }

    @Test
    void graceSecondsOutOfRangeOrNotWholeIsRefused() {
        assertThatThrownBy(() -> VectorHandler.parseGraceSeconds(-1))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("grace_seconds");
        assertThatThrownBy(() -> VectorHandler.parseGraceSeconds(VectorHandler.MAX_REAPABLE_GRACE_SECONDS + 1))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> VectorHandler.parseGraceSeconds("3600"))
            .as("a string is not a number of seconds").isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> VectorHandler.parseGraceSeconds(1.5))
            .as("a fraction is not a whole number of seconds").isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theListingRefusesAQuarantineCollection_likeTheCensus() {
        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection("quarantine-docs__x__m__v1"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("quarantine");
    }

    @Test
    void theLimitCapIsThreeHundred() {
        assertThat(VectorHandler.MAX_REAPABLE_LIMIT).isEqualTo(300);
    }
}
