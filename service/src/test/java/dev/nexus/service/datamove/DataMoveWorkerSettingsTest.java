// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Map;
import java.util.Set;

/** nexus-f3yqd.2.13: the parts of the worker that need no database: the sleep override's clamp and the in-memory states. */
class DataMoveWorkerSettingsTest {

    private static Integer override(String value) {
        Map<String, String> env = value == null ? Map.of() : Map.of(DataMoveWorker.SLEEP_ENV, value);
        return DataMoveWorker.Settings.fromEnv(env::get).sleepOverrideMs();
    }

    @Test
    void sleepOverride_isClampedToTheRegistrysBounds() {
        assertThat(override(null)).isNull();
        assertThat(override("")).isNull();
        assertThat(override("250")).isEqualTo(250);
        assertThat(override("0")).isEqualTo(0);
        assertThat(override("-5")).isEqualTo(0);
        assertThat(override("60000")).isEqualTo(60_000);
        assertThat(override("100000")).isEqualTo(60_000);
        assertThat(override("99999999999999")).isEqualTo(60_000);
        assertThat(override("fast")).as("not a number: ignored").isNull();
    }

    private static DataMove move(String label) {
        return new DataMove(label, "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 0, null, null);
    }

    @Test
    void moveStates_keepTheTimeAMoveFirstWasBlockedForTheSameReason() {
        var clock = new Clock() {
            Instant now = Instant.parse("2026-11-16T00:00:00Z");

            @Override
            public java.time.ZoneId getZone() {
                return ZoneOffset.UTC;
            }

            @Override
            public Clock withZone(java.time.ZoneId zone) {
                return this;
            }

            @Override
            public Instant instant() {
                return now;
            }
        };
        var states = new MoveStates(clock);
        var m = move("a");
        states.blocked(m, "no slot");
        Instant first = clock.now;
        clock.now = first.plus(Duration.ofMinutes(1));
        states.blocked(m, "no slot");
        var same = states.state("a").orElseThrow();
        assertThat(same.since()).isEqualTo(first);
        assertThat(same.updatedAt()).isEqualTo(clock.now);
        clock.now = clock.now.plus(Duration.ofMinutes(1));
        states.blocked(m, "no install");
        assertThat(states.state("a").orElseThrow().since()).isEqualTo(clock.now);

        states.working(move("b"));
        states.retainOnly(Set.of("b"));
        assertThat(states.state("a")).isEmpty();
        assertThat(states.snapshot()).extracting(MoveStates.State::label).containsExactly("b");
    }
}
