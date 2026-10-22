// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** nexus-5tykh: the key and the three settings of {@link EngineSingletonLock}, no database. */
class EngineSingletonLockSettingsTest {

    private static EngineSingletonLock.Settings from(Map<String, String> env) {
        return EngineSingletonLock.Settings.fromEnv(env::get);
    }

    @Test
    void theKeyIsTheFirstEightBytesOfTheSha256OfItsDocumentedName() throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256")
            .digest(EngineSingletonLock.KEY_NAME.getBytes(StandardCharsets.UTF_8));
        assertThat(EngineSingletonLock.LOCK_KEY).isEqualTo(ByteBuffer.wrap(digest, 0, 8).getLong());
    }

    @Test
    void theKeyCannotCollideWithTheHashtextLocksTheEngineAlreadyTakes() {
        // pg_advisory_xact_lock(hashtext(...)) widens an int4 to the bigint form, so its classid is 0 or
        // 0xFFFFFFFF. A key whose high word is neither can never equal one of those.
        int high = (int) (EngineSingletonLock.LOCK_KEY >>> 32);
        assertThat(high).isNotEqualTo(0).isNotEqualTo(-1);
        assertThat(EngineSingletonLock.LOCK_KEY).as("the migration walk's own session lock")
            .isNotEqualTo(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY);
    }

    @Test
    void theDefaultsEnforceTheLockAndWaitThirtySeconds() {
        var s = from(Map.of());
        assertThat(s.mode()).isEqualTo(EngineSingletonLock.Mode.ENFORCE);
        assertThat(s.bootWait()).isEqualTo(Duration.ofSeconds(30));
        assertThat(s.checkInterval()).isEqualTo(Duration.ofSeconds(10));
    }

    @Test
    void offDisablesTheLock_andOnIsTheSameAsUnset() {
        assertThat(from(Map.of(EngineSingletonLock.MODE_ENV, "off")).mode())
            .isEqualTo(EngineSingletonLock.Mode.OFF);
        assertThat(from(Map.of(EngineSingletonLock.MODE_ENV, "on")).mode())
            .isEqualTo(EngineSingletonLock.Mode.ENFORCE);
    }

    @Test
    void aBlankValueIsTheDefault_asForTheEnginesOtherIntegerSettings() {
        var s = from(Map.of(EngineSingletonLock.MODE_ENV, " ", EngineSingletonLock.WAIT_ENV, "",
            EngineSingletonLock.CHECK_ENV, " "));
        assertThat(s.mode()).isEqualTo(EngineSingletonLock.Mode.ENFORCE);
        assertThat(s.bootWait()).isEqualTo(Duration.ofSeconds(30));
        assertThat(s.checkInterval()).isEqualTo(Duration.ofSeconds(10));
    }

    @Test
    void anyOtherModeRefusesToStart_itIsNeverReadAsOff() {
        for (String bad : new String[] {"OFF", "0", "false", "no", "yes"}) {
            assertThatThrownBy(() -> from(Map.of(EngineSingletonLock.MODE_ENV, bad)))
                .as("mode '%s'", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(EngineSingletonLock.MODE_ENV);
        }
    }

    @Test
    void theWaitAndTheCheckIntervalAreBounded() {
        assertThat(from(Map.of(EngineSingletonLock.WAIT_ENV, "0")).bootWait()).isEqualTo(Duration.ZERO);
        assertThat(from(Map.of(EngineSingletonLock.WAIT_ENV, "600")).bootWait()).isEqualTo(Duration.ofSeconds(600));
        assertThat(from(Map.of(EngineSingletonLock.CHECK_ENV, "1")).checkInterval()).isEqualTo(Duration.ofSeconds(1));
        for (String bad : new String[] {"-1", "601", "abc", "1.5"}) {
            assertThatThrownBy(() -> from(Map.of(EngineSingletonLock.WAIT_ENV, bad)))
                .as("wait '%s'", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(EngineSingletonLock.WAIT_ENV);
        }
        for (String bad : new String[] {"0", "301", "abc"}) {
            assertThatThrownBy(() -> from(Map.of(EngineSingletonLock.CHECK_ENV, bad)))
                .as("check '%s'", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(EngineSingletonLock.CHECK_ENV);
        }
    }

    @Test
    void aPoolerAdminUrlMeansTheSessionLockCannotBeTrusted_soItIsSkippedNotEnforced() {
        var s = from(Map.of(PoolerModeCheck.ADMIN_URL_ENV, "jdbc:postgresql://127.0.0.1:6432/pgbouncer"));
        assertThat(s.mode()).isEqualTo(EngineSingletonLock.Mode.POOLED_SKIP);
        // The explicit off still reads as off, so the boot logs the operator's own choice.
        assertThat(from(Map.of(PoolerModeCheck.ADMIN_URL_ENV, "jdbc:x", EngineSingletonLock.MODE_ENV, "off")).mode())
            .isEqualTo(EngineSingletonLock.Mode.OFF);
        // A blank admin url is the disabled probe (PoolerModeCheck), not a pooler.
        assertThat(from(Map.of(PoolerModeCheck.ADMIN_URL_ENV, "  ")).mode())
            .isEqualTo(EngineSingletonLock.Mode.ENFORCE);
    }

    @Test
    void anApplicationNameIsTruncatedTheWayPostgresTruncatesIt() {
        assertThat(EngineSingletonLock.pgApplicationName("short")).isEqualTo("short");
        String long70 = "x".repeat(70);
        assertThat(EngineSingletonLock.pgApplicationName(long70)).hasSize(63);
    }
}
