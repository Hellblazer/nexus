// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.ChunkReaper.Settings;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-192 Step 9 (bead nexus-2x9xa): the reaper's settings and Sam's defaults. */
class ChunkReaperSettingsTest {

    private static Settings of(Map<String, String> env) {
        return Settings.fromEnv(env::get);
    }

    @Test
    void theDefaultsAreSamsRulings() {
        Settings s = of(Map.of());
        assertThat(s.enabled()).isTrue();
        assertThat(s.interval()).as("hourly").isEqualTo(Duration.ofHours(1));
        assertThat(s.batchSize()).as("at most 300 per collection per pass").isEqualTo(300);
        assertThat(s.floorFraction()).as("NX_GC_FLOOR_FRACTION's default").isEqualTo(0.25);
        assertThat(s.floorMinChunks()).as("from 100 chunks up").isEqualTo(100);
        assertThat(s).isEqualTo(Settings.defaults());
    }

    @Test
    void everySettingIsConfigurable() {
        Settings s = of(Map.of(
            ChunkReaper.INTERVAL_SECONDS_ENV, "600", ChunkReaper.BATCH_SIZE_ENV, "50",
            ChunkReaper.FLOOR_FRACTION_ENV, "0.5", ChunkReaper.FLOOR_MIN_CHUNKS_ENV, "10",
            ChunkReaper.WALL_CLOCK_BUDGET_SECONDS_ENV, "30"));
        assertThat(s.interval()).isEqualTo(Duration.ofMinutes(10));
        assertThat(s.batchSize()).isEqualTo(50);
        assertThat(s.floorFraction()).isEqualTo(0.5);
        assertThat(s.floorMinChunks()).isEqualTo(10);
        assertThat(s.wallClockBudget()).isEqualTo(Duration.ofSeconds(30));
    }

    @Test
    void theKillSwitchTurnsItOff() {
        for (String off : new String[] {"false", "FALSE", "0", "off", "no"}) {
            assertThat(of(Map.of(ChunkReaper.ENABLED_ENV, off)).enabled()).as(off).isFalse();
        }
        assertThat(of(Map.of(ChunkReaper.ENABLED_ENV, "true")).enabled()).isTrue();
    }

    @Test
    void aBatchSizeOverThe300CapFallsBackToTheDefault_neverRaisesTheCap() {
        assertThat(of(Map.of(ChunkReaper.BATCH_SIZE_ENV, "301")).batchSize()).isEqualTo(300);
        assertThat(of(Map.of(ChunkReaper.BATCH_SIZE_ENV, "0")).batchSize()).isEqualTo(300);
    }

    @Test
    void aMalformedFloorFallsBackToTheSafeDefault_aNanOrOutOfRangeValueDoesNotNeutralizeIt() {
        for (String bad : new String[] {"nan", "NaN", "inf", "1.5", "-0.1", "abc"}) {
            assertThat(of(Map.of(ChunkReaper.FLOOR_FRACTION_ENV, bad)).floorFraction()).as(bad).isEqualTo(0.25);
        }
        assertThat(of(Map.of(ChunkReaper.FLOOR_MIN_CHUNKS_ENV, "-1")).floorMinChunks()).isEqualTo(100);
    }

    @Test
    void aMalformedIntervalFallsBackToHourly() {
        assertThat(of(Map.of(ChunkReaper.INTERVAL_SECONDS_ENV, "soon")).interval()).isEqualTo(Duration.ofHours(1));
        assertThat(of(Map.of(ChunkReaper.INTERVAL_SECONDS_ENV, "0")).interval()).isEqualTo(Duration.ofHours(1));
    }
}
