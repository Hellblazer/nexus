// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-mtheb.7 - {@link TelemetryRepository#agentDispatchTs}: the boundary of the future clamp and the
 * strict parse, against a fixed clock. The handler test drives the same rule end to end against Postgres.
 */
class AgentDispatchTsTest {

    private static final OffsetDateTime NOW = OffsetDateTime.parse("2026-11-01T12:00:00Z");

    @Test
    void exactlyFiveMinutesAheadIsKeptAndOneNanosecondMoreIsClamped() {
        OffsetDateTime edge = NOW.plusMinutes(5);
        assertThat(TelemetryRepository.agentDispatchTs(edge.toString(), NOW)).isEqualTo(edge);
        assertThat(TelemetryRepository.agentDispatchTs(edge.plusNanos(1).toString(), NOW))
            .as("one nanosecond past the window is skew").isEqualTo(NOW);
    }

    @Test
    void thePastAndTheWindowAreKeptAsGivenInAnyOffset() {
        assertThat(TelemetryRepository.agentDispatchTs("2026-01-01T00:00:00Z", NOW))
            .isEqualTo(OffsetDateTime.parse("2026-01-01T00:00:00Z"));
        // 17:03 at +05:00 is 12:03Z, three minutes ahead: kept, and compared as an instant
        OffsetDateTime inWindow = TelemetryRepository.agentDispatchTs("2026-11-01T17:03:00+05:00", NOW);
        assertThat(inWindow.toInstant()).isEqualTo(NOW.plusMinutes(3).toInstant());
        // 17:10 at +05:00 is 12:10Z, ten minutes ahead: clamped
        assertThat(TelemetryRepository.agentDispatchTs("2026-11-01T17:10:00+05:00", NOW)).isEqualTo(NOW);
    }

    @Test
    void nullAndBlankAreNow() {
        assertThat(TelemetryRepository.agentDispatchTs(null, NOW)).isEqualTo(NOW);
        assertThat(TelemetryRepository.agentDispatchTs("", NOW)).isEqualTo(NOW);
        assertThat(TelemetryRepository.agentDispatchTs("   ", NOW)).isEqualTo(NOW);
    }

    @Test
    void anythingElseIsAnIllegalArgumentNamingTheField() {
        for (String bad : new String[] {"yesterday", "2026-13-45T00:00:00Z", "2026-11-01", "2026-11-01T12:00:00", "1730000000"}) {
            assertThatThrownBy(() -> TelemetryRepository.agentDispatchTs(bad, NOW))
                .as(bad).isInstanceOf(IllegalArgumentException.class).hasMessageContaining("Field 'ts'");
        }
    }
}
