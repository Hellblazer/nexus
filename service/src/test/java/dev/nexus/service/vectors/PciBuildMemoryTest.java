// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.OptionalInt;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd (E): the maintenance_work_mem a per-collection index build runs under. min(1 GB, 1/8 of the RAM of the
 * host Postgres runs on), the override wins within its limits, and a Postgres that is not on this host keeps its own
 * setting unless the override is set.
 */
class PciBuildMemoryTest {

    static final long GB = 1L << 30;
    static final long MB = 1L << 20;

    @Test
    void anEighthOfRam_whenThatIsBelowTheCap() {
        assertThat(PciBuildMemory.resolve(null, true, () -> 4 * GB)).hasValue(512);
        assertThat(PciBuildMemory.resolve(null, true, () -> 2 * GB)).hasValue(256);
        assertThat(PciBuildMemory.resolve(null, true, () -> 8 * GB)).as("exactly the cap").hasValue(1024);
    }

    @Test
    void oneGigabyteCap_onALargeHost() {
        assertThat(PciBuildMemory.resolve(null, true, () -> 64 * GB)).hasValue(1024);
        assertThat(PciBuildMemory.resolve(null, true, () -> 1024 * GB)).hasValue(1024);
    }

    @Test
    void aTinyHost_getsAtLeastTheMinimum_notZero() {
        assertThat(PciBuildMemory.resolve(null, true, () -> 64 * MB).getAsInt()).isEqualTo(PciBuildMemory.FLOOR_MB);
    }

    @Test
    void theOverrideWins_overTheCapAndTheFraction_whereverPostgresRuns() {
        assertThat(PciBuildMemory.resolve(2048, true, () -> 4 * GB)).as("above the 1 GB cap").hasValue(2048);
        assertThat(PciBuildMemory.resolve(64, true, () -> 64 * GB)).as("below the fraction").hasValue(64);
        assertThat(PciBuildMemory.resolve(300, false, () -> { throw new AssertionError("RAM is not read"); }))
            .as("a remote Postgres with the override set").hasValue(300);
    }

    @Test
    void aPostgresOnAnotherHost_withNoOverride_keepsItsOwnSetting_andTheEngineHostsRamIsNotRead() {
        OptionalInt none = PciBuildMemory.resolve(null, false, () -> { throw new AssertionError("RAM is not read"); });
        assertThat(none).isEmpty();
    }

    @Test
    void ramThatCannotBeRead_keepsPostgresOwnSetting() {
        assertThat(PciBuildMemory.resolve(null, true, () -> 0)).isEmpty();
        assertThat(PciBuildMemory.resolve(null, true, () -> -1)).isEmpty();
    }

    @Test
    void postgresIsOnThisHost_whenTheUrlNamesLoopbackOrASocket() {
        for (String url : new String[] {
            "jdbc:postgresql://127.0.0.1:5432/nexus", "jdbc:postgresql://localhost/nexus",
            "jdbc:postgresql://LOCALHOST:5433/nexus?sslmode=disable", "jdbc:postgresql://[::1]:5432/nexus",
            "jdbc:postgresql://127.0.1.1:5432/nexus", "jdbc:postgresql:///nexus?host=/tmp", "jdbc:postgresql:nexus"}) {
            assertThat(PciBuildMemory.pgOnThisHost(url)).as(url).isTrue();
        }
    }

    @Test
    void postgresIsOnAnotherHost_forEveryOtherName() {
        for (String url : new String[] {
            "jdbc:postgresql://db.example.com:5432/nexus", "jdbc:postgresql://10.0.0.5/nexus",
            "jdbc:postgresql://nexus-pg.internal:5432/nexus?sslmode=require",
            "jdbc:postgresql://localhost.example.com/nexus", "jdbc:postgresql://127.0.0.1.evil.example/nexus",
            "jdbc:postgresql://[2001:db8::1]:5432/nexus", "not a url"}) {
            assertThat(PciBuildMemory.pgOnThisHost(url)).as(url).isFalse();
        }
    }

    @Test
    void theHostsRamIsReadable_onThisMachine() {
        assertThat(PciBuildMemory.hostRamBytes().getAsLong()).isPositive();
    }
}
