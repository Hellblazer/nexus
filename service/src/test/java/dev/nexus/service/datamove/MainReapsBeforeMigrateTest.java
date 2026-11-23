// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Main ends the leftover {@code nexus-data-move-} backends BEFORE the schema migration (nexus-p2m1x.21): a validation with no statement bound
 * outlives its engine and holds locks a migration would wait on. A source-order check; it needs no database.
 */
class MainReapsBeforeMigrateTest {

    @Test
    void mainReapsLeftoverBackendsBeforeItMigrates() throws Exception {
        String main = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        int reap = main.indexOf("DataMoveOrphans.terminateLeftover(");
        int migrate = main.indexOf("SchemaMigrator.migrate(migrationDs");
        assertThat(reap).as("Main calls the reaper").isPositive();
        assertThat(migrate).as("and the migration").isPositive();
        assertThat(reap).as("the reaper runs first").isLessThan(migrate);
    }
}
