// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

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
    void theKeyDiffersFromEveryAdvisoryLockKeyConstantInTheMainSources() throws Exception {
        // Not a list kept by hand: every `static final long ..._ADVISORY_LOCK_KEY` in src/main is found, loaded and
        // compared, so a key added later is checked without anyone remembering to.
        Pattern decl = Pattern.compile("static\\s+final\\s+long\\s+(\\w*ADVISORY_LOCK_KEY)\\s*=");
        Path root = Path.of("src/main/java");
        Map<String, Long> found = new java.util.TreeMap<>();
        try (Stream<Path> files = Files.walk(root)) {
            for (Path f : (Iterable<Path>) files.filter(x -> x.toString().endsWith(".java"))::iterator) {
                Matcher m = decl.matcher(Files.readString(f));
                while (m.find()) {
                    String rel = root.relativize(f).toString();
                    String fqn = rel.substring(0, rel.length() - ".java".length()).replace('/', '.');
                    var field = Class.forName(fqn).getDeclaredField(m.group(1));
                    field.setAccessible(true);
                    found.put(fqn + "." + m.group(1), field.getLong(null));
                }
            }
        }
        assertThat(found.keySet()).as("non-vacuity: the census finds the two known keys, PciBuilderSession's included")
            .contains("dev.nexus.service.db.SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY",
                "dev.nexus.service.vectors.PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY");
        found.forEach((name, value) -> {
            assertThat(value).as(name).isNotEqualTo(EngineSingletonLock.LOCK_KEY);
            // As pg_locks reports it, the classid/objid halves: neither may match ours.
            assertThat(value >>> 32 == EngineSingletonLock.LOCK_KEY >>> 32
                && (value & 0xFFFFFFFFL) == (EngineSingletonLock.LOCK_KEY & 0xFFFFFFFFL)).as(name + " halves").isFalse();
        });
    }

    @Test
    void everyPoolApplicationNameLeavesRoomForTheLockSuffix_andKeepsItsNonce() {
        // Postgres cuts application_name at 63 bytes. The release part is the stamped tag and was unbounded, so a
        // long one cut the nonce, which is the end of the name, and the lock connection's name stopped matching
        // the string compared against pg_stat_activity.
        String longest = "r".repeat(BackendReaper.MAX_RELEASE_BYTES);
        for (String release : new String[] {null, "", "  ", "0.1.163", "0.1.163-rc.1", longest, longest + "x",
                "x".repeat(300), "\u00e9".repeat(40), "\uD83D\uDE00".repeat(30)}) {
            String pool = BackendReaper.newApplicationName(release);
            String lock = pool + EngineSingletonLock.NAME_SUFFIX;
            assertThat(lock.getBytes(StandardCharsets.UTF_8).length).as("lock name for release '%s'", release)
                .isLessThanOrEqualTo(63);
            assertThat(EngineSingletonLock.pgApplicationName(lock)).as("not truncated by Postgres").isEqualTo(lock);
            assertThat(BackendReaper.bootNonce(pool)).as("the nonce survives whole").hasSize(8).matches("[0-9a-f]{8}");
            assertThat(lock).endsWith(BackendReaper.bootNonce(pool) + EngineSingletonLock.NAME_SUFFIX);
            assertThat((pool + "/reaper").getBytes(StandardCharsets.UTF_8).length).as("the reaper's name too")
                .isLessThanOrEqualTo(63);
            // Postgres rewrites non-ASCII bytes in application_name; the name must survive the round trip verbatim.
            assertThat(lock).as("printable ASCII only").matches("[\\x20-\\x7E]+");
        }
        assertThat(BackendReaper.newApplicationName("0.1.163-\u00e9")).contains("/0.1.163-_/");
        assertThat(BackendReaper.newApplicationName(longest)).as("the longest allowed release is kept whole")
            .contains("/" + longest + "/");
        assertThat(BackendReaper.MAX_RELEASE_BYTES).isEqualTo(25);
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
        assertThat(from(Map.of(EngineSingletonLock.CHECK_ENV, "60")).checkInterval())
            .as("the longest interval bounds the window in which two engines can serve").isEqualTo(Duration.ofSeconds(60));
        assertThat(EngineSingletonLock.MAX_CHECK_SECONDS).isEqualTo(60);
        for (String bad : new String[] {"-1", "601", "abc", "1.5"}) {
            assertThatThrownBy(() -> from(Map.of(EngineSingletonLock.WAIT_ENV, bad)))
                .as("wait '%s'", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(EngineSingletonLock.WAIT_ENV);
        }
        for (String bad : new String[] {"0", "61", "300", "abc"}) {
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
