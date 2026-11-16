// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Map;

/** The import settings (RDR-248 Step 5, bead nexus-csayy.6): the defaults the design names, and an env value that is refused naming itself. */
class TenantImportConfigTest {

    @Test
    void theDefaultsAreEightGibibytesAndALoadBoundOfTwiceTheMeasuredLoad() {
        var c = TenantImportSessions.Config.fromEnv(Map.<String, String>of()::get);
        assertThat(c.maxBytes()).isEqualTo(8L << 30);
        // 2x the Step 6 load (1,973 s for 500,000 chunks), Sam 2026-11-14, ask rdr248-load-bound
        assertThat(c.loadMax()).isEqualTo(Duration.ofSeconds(3960));
        assertThat(c.restoreRetryAfterSeconds()).isEqualTo(60);
        assertThat(c.restoreLockWait()).isEqualTo(Duration.ofSeconds(5));
        assertThat(c.outcomeRetention()).isEqualTo(Duration.ofHours(24));
        assertThat(TenantImportSessions.PART_BYTES).isEqualTo(8 * 1024 * 1024);
    }

    @Test
    void theEnvironmentOverridesBothSettings() {
        var c = TenantImportSessions.Config.fromEnv(Map.of(TenantImportSessions.ENV_MAX_BYTES, "1000000",
            TenantImportSessions.ENV_LOAD_MAX, "90")::get);
        assertThat(c.maxBytes()).isEqualTo(1_000_000L);
        assertThat(c.loadMax()).isEqualTo(Duration.ofSeconds(90));
    }

    @Test
    void aValueThatIsNotAWholeNumberOrIsOutOfRangeIsRefusedNamingTheVariable() {
        for (String name : new String[] {TenantImportSessions.ENV_MAX_BYTES, TenantImportSessions.ENV_LOAD_MAX}) {
            for (String bad : new String[] {"ten", "0", "-5", "99999999999999999"}) {
                assertThatThrownBy(() -> TenantImportSessions.Config.fromEnv(Map.of(name, bad)::get))
                    .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(name);
            }
        }
    }
}
