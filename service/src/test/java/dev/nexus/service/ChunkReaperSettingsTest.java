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
    void theKillSwitchTakesAnExplicitTrueOrFalse() {
        for (String off : new String[] {"false", "FALSE", " False ", "0", "off", "no"}) {
            assertThat(of(Map.of(ChunkReaper.ENABLED_ENV, off)).enabled()).as(off).isFalse();
        }
        for (String on : new String[] {"true", "TRUE", "1", "on", "yes"}) {
            assertThat(of(Map.of(ChunkReaper.ENABLED_ENV, on)).enabled()).as(on).isTrue();
        }
    }

    @Test
    void aKillSwitchValueThatIsNeitherTrueNorFalse_warns_andLeavesTheReaperOnTheDefault() throws Throwable {
        // The old rule disabled only on false/0/off/no, so "disabled", "disable" and "n" left it ON in silence.
        for (String typo : new String[] {"disabled", "disable", "n", "of", "nope", "2"}) {
            ch.qos.logback.classic.Logger root =
                (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
            ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
                new ch.qos.logback.core.read.ListAppender<>();
            logs.start();
            root.addAppender(logs);
            try {
                assertThat(of(Map.of(ChunkReaper.ENABLED_ENV, typo)).enabled()).as(typo).isTrue();
            } finally {
                root.detachAppender(logs);
                logs.stop();
            }
            assertThat(logs.list).as("a WARN for %s", typo).anyMatch(e ->
                e.getLevel() == ch.qos.logback.classic.Level.WARN
                    && e.getFormattedMessage().contains("event=reaper_setting_invalid")
                    && e.getFormattedMessage().contains(ChunkReaper.ENABLED_ENV));
        }
        assertThat(of(Map.of()).enabled()).as("unset: the default, on").isTrue();
        assertThat(of(Map.of(ChunkReaper.ENABLED_ENV, "  ")).enabled()).as("blank: the default, on").isTrue();
    }

    @Test
    void theIntervalFloorsAtSixtySeconds() {
        assertThat(of(Map.of(ChunkReaper.INTERVAL_SECONDS_ENV, "1")).interval()).isEqualTo(Duration.ofSeconds(60));
        assertThat(of(Map.of(ChunkReaper.INTERVAL_SECONDS_ENV, "59")).interval()).isEqualTo(Duration.ofSeconds(60));
        assertThat(of(Map.of(ChunkReaper.INTERVAL_SECONDS_ENV, "60")).interval()).isEqualTo(Duration.ofSeconds(60));
        assertThat(of(Map.of(ChunkReaper.INTERVAL_SECONDS_ENV, "61")).interval()).isEqualTo(Duration.ofSeconds(61));
    }

    @Test
    void theCensusTimeoutDefaultsToAMinute_andIsConfigurable() {
        assertThat(of(Map.of()).censusTimeout()).isEqualTo(Duration.ofSeconds(60));
        assertThat(of(Map.of(ChunkReaper.CENSUS_TIMEOUT_SECONDS_ENV, "5")).censusTimeout())
            .isEqualTo(Duration.ofSeconds(5));
        assertThat(of(Map.of(ChunkReaper.CENSUS_TIMEOUT_SECONDS_ENV, "0")).censusTimeout())
            .as("0 would disable the bound").isEqualTo(Duration.ofSeconds(60));
    }

    @Test
    void theFirstPassIsShortlyAfterBoot_notOneIntervalAfterIt() {
        assertThat(ChunkReaper.INITIAL_DELAY).isLessThanOrEqualTo(Duration.ofMinutes(2));
        assertThat(ChunkReaper.INITIAL_DELAY).isLessThanOrEqualTo(ChunkReaper.MIN_INTERVAL);
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
