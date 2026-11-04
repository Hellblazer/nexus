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
 * nexus-b1adx: {@code Main.main} cannot run in a test, so the service-role check's place in the boot order is pinned
 * on Main's text: after the schema migration and the chunks isolation check, before the engine serves (the token
 * seed), run on the service pool {@code ds}, and a refusal closes the pool and exits non-zero.
 */
class MainServiceRoleCheckWiringTest {

    private static String code() throws Exception {
        String src = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        return Arrays.stream(src.split("\n")).filter(l -> !l.trim().startsWith("//"))
            .collect(Collectors.joining("\n"));
    }

    @Test
    void theRoleIsCheckedOnTheServicePool_afterTheMigrationAndTheIsolationCheck_andARefusalExitsOne() throws Exception {
        String src = code();
        int migration = src.indexOf("SchemaMigrator.migrate(migrationDs)");
        int isolation = src.indexOf("ChunksIsolationCheck.verifyAtStartup(ds);");
        int check = src.indexOf("ServiceRoleBypassRlsCheck.verifyAtStartup(ds);");
        int tokenSeed = src.indexOf("new dev.nexus.service.db.TokenStore(ds,");

        assertThat(migration).isPositive();
        assertThat(isolation).as("the isolation check is in main").isGreaterThan(migration);
        assertThat(check).as("the role check runs on the service pool, after the migration and the isolation check")
            .isGreaterThan(isolation);
        assertThat(tokenSeed).as("and before the engine seeds its root token").isGreaterThan(check);
        assertThat(src.indexOf("ServiceRoleBypassRlsCheck.verifyAtStartup(", check + 1)).as("checked once").isNegative();

        int catchAt = src.indexOf("catch (dev.nexus.service.db.ServiceRoleBypassRlsCheck.BypassRlsException e) {", check);
        assertThat(catchAt).as("the refusal is caught right after the check").isPositive().isLessThan(tokenSeed);
        String handler = src.substring(catchAt, src.indexOf("System.exit(1);", catchAt) + "System.exit(1);".length());
        assertThat(handler).contains("ds.close();").contains("event=service_role_bypassrls_refused");
    }
}
