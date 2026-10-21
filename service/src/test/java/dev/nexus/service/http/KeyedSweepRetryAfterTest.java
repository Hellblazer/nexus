// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.22): the 409 {@code Retry-After} is a function of the two instants the
 * database reported, and of nothing else.
 *
 * <p>The instants here are in 2001, so no host clock can agree with them. A {@code retryAfterSeconds} that
 * measured from the JVM's own clock instead of {@code observedAt} would see a lease that ended twenty-five
 * years ago and answer the floor, 1; these cases expect 40, 41 and 7. (A component test on one host cannot
 * tell the two apart, because the JVM and the container share a clock; that is why this is a pure-function test.)
 */
class KeyedSweepRetryAfterTest {

    private static final OffsetDateTime OBSERVED = OffsetDateTime.of(2001, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC);

    @Test
    void measuresFromTheDatabaseReading_notFromTheJvmClock() {
        assertThat(KeyedSweep.retryAfterSeconds(OBSERVED, OBSERVED.plusSeconds(40))).isEqualTo(40L);
    }

    @Test
    void roundsAPartialSecondUp() {
        assertThat(KeyedSweep.retryAfterSeconds(OBSERVED, OBSERVED.plusSeconds(40).plusNanos(200_000_000L)))
            .isEqualTo(41L);
        assertThat(KeyedSweep.retryAfterSeconds(OBSERVED, OBSERVED.plusSeconds(6).plusNanos(1_000_000L)))
            .isEqualTo(7L);
    }

    @Test
    void aLeaseAlreadyBehindTheReading_answersTheFloorOfOneSecond() {
        // A row an in-flight marker transaction holds past its lease (nexus-yye8l.19).
        assertThat(KeyedSweep.retryAfterSeconds(OBSERVED, OBSERVED.minusSeconds(30))).isEqualTo(1L);
        assertThat(KeyedSweep.retryAfterSeconds(OBSERVED, OBSERVED)).isEqualTo(1L);
    }
}
