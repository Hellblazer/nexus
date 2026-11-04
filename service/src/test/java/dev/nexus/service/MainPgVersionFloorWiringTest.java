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
 * nexus-3s5r1: {@code Main.main} cannot run in a test, so the PostgreSQL 17 floor's place in the boot order is pinned
 * on Main's text: after the pool exists, before the singleton lock and the schema migration, and the refusal closes
 * the pool and exits non-zero. {@code PgVersionFloorTest} proves the decision.
 */
class MainPgVersionFloorWiringTest {

    private static String code() throws Exception {
        String src = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        return Arrays.stream(src.split("\n")).filter(l -> !l.trim().startsWith("//"))
            .collect(Collectors.joining("\n"));
    }

    @Test
    void theFloorIsCheckedAfterThePool_beforeTheLockAndTheMigration_andARefusalExitsOne() throws Exception {
        String src = code();
        int pool = src.indexOf("var ds = new HikariDataSource(hikari);");
        int check = src.indexOf("PgVersionFloor.verifyAtBoot(ds);");
        int lock = src.indexOf("bootSingletonLock(ds, dbUrl, dbUser, dbPass, applicationName);");
        int migration = src.indexOf("SchemaMigrator.migrate(migrationDs)");

        assertThat(pool).isPositive();
        assertThat(check).as("the floor check is in main").isGreaterThan(pool);
        assertThat(lock).as("before the singleton lock").isGreaterThan(check);
        assertThat(migration).as("before the schema migration").isGreaterThan(lock);
        assertThat(src.indexOf("PgVersionFloor.verifyAtBoot(", check + 1)).as("checked once").isNegative();

        int catchAt = src.indexOf("catch (dev.nexus.service.db.PgVersionFloor.FloorException e) {", check);
        assertThat(catchAt).as("the refusal is caught right after the check").isPositive().isLessThan(lock);
        String handler = src.substring(catchAt, src.indexOf("System.exit(1);", catchAt) + "System.exit(1);".length());
        assertThat(handler).contains("ds.close();").contains("event=pg_version_below_floor");
    }
}
