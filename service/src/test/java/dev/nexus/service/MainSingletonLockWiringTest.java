// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-5tykh: {@code Main.main} cannot run in a test, so what only it does is pinned on its text. The refusal and
 * the opt-out are proven by a real boot in {@code MainBootEnvValidationIntegrationTest}; this class pins the three
 * facts a boot test cannot see: the lock comes before the schema migration, a lock lost and taken back clears the
 * {@code CollectionRegistry} cache, and the hook releases the lock last.
 */
class MainSingletonLockWiringTest {

    /** Main's source without its {@code //} comment lines, which mention the same names the pins look for. */
    private static String code() throws Exception {
        String src = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        return Arrays.stream(src.split("\n")).filter(l -> !l.trim().startsWith("//"))
            .collect(Collectors.joining("\n"));
    }

    @Test
    void theLockIsTakenAfterTheEnvValidation_andBeforeTheSchemaMigration() throws Exception {
        String src = code();
        int validation = src.indexOf("event=pg_session_env_invalid");
        int lock = src.indexOf("bootSingletonLock(ds, dbUrl, dbUser, dbPass, applicationName);");
        int migration = src.indexOf("SchemaMigrator.migrate(migrationDs)");

        assertThat(validation).isPositive();
        assertThat(lock).as("the lock is taken in main").isGreaterThan(validation);
        assertThat(migration).as("a second engine must not migrate either").isGreaterThan(lock);
        assertThat(src.indexOf("bootSingletonLock(ds,", lock + 1)).as("taken once").isNegative();
    }

    @Test
    void aLockLostAndTakenBackClearsTheRegistryCache_andALockLostToAnotherEngineEndsTheProcessNonZero()
            throws Exception {
        String src = code();
        int helper = src.indexOf("private static EngineSingletonLock bootSingletonLock(");
        assertThat(helper).isPositive();
        int helperEnd = src.indexOf("private static HikariDataSource buildMigrationDataSource(", helper);
        assertThat(helperEnd).as("the helper ends where the next method starts").isGreaterThan(helper);
        String body = src.substring(helper, helperEnd);

        assertThat(body).as("the monitor is started with the registry clear as its regain hook")
            .contains("lock.startMonitor(settings.checkInterval(), dev.nexus.service.db.CollectionRegistry::evictAll,");
        assertThat(body).as("a loss to another engine exits with the lock's status, off the checking thread")
            .contains("System.exit(EngineSingletonLock.EXIT_STATUS)").contains("new Thread(");
        assertThat(dev.nexus.service.db.EngineSingletonLock.EXIT_STATUS).isNotZero();
        for (String refusal : new String[] {
                "event=engine_singleton_lock_env_invalid", "e.getMessage()", "event=engine_singleton_lock_connect_failed"}) {
            assertThat(body).contains(refusal);
        }
        assertThat(body).as("every refusal closes the pool and then exits 1").contains("ds.close();")
            .contains("System.exit(1);");
    }

    @Test
    void theShutdownHookReleasesTheLockLast_afterThePoolCloses() throws Exception {
        String src = code();
        int hook = src.indexOf("Runtime.getRuntime().addShutdownHook");
        int embedders = src.indexOf("docEmbedRouter.close();", hook);
        int poolClose = src.indexOf("ds.close();", embedders);
        int release = src.indexOf("singletonLock.close();", hook);

        assertThat(hook).isPositive();
        assertThat(release).as("the hook releases the lock").isPositive();
        assertThat(release).as("after the pool closes, so a successor cannot boot while this engine still ends"
            + " its backends").isGreaterThan(poolClose);
        assertThat(src.indexOf("singletonLock.close();", release + 1)).as("once").isNegative();
    }
}
