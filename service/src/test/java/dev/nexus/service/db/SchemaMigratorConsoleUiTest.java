// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-64kwn: with {@link SchemaMigrator#UI_PROPERTY} set to {@value SchemaMigrator#UI_LOGGER}
 * (the surefire forks) a walk prints no "Running Changeset" line to stdout; without it (the engine)
 * it still does, because the readiness probe reads those lines from the engine console.
 * The database is a migrated template clone: its walk re-runs only the runAlways changesets, enough
 * to print lines without the cost of a walk from nothing.
 *
 * <p>Each walk runs on a fresh thread after stdout is replaced: Liquibase's root scope is per
 * thread and its ConsoleUIService captures {@code System.out} when that scope is created.
 */
class SchemaMigratorConsoleUiTest {

    @Test
    void theLoggerUi_keepsRunningChangesetLinesOffStdout() throws Exception {
        assertThat(walkCapturingStdout(SchemaMigrator.UI_LOGGER)).doesNotContain("Running Changeset");
    }

    @Test
    void withoutTheProperty_theConsoleStillCarriesRunningChangeset() throws Exception {
        assertThat(walkCapturingStdout(null)).contains("Running Changeset: db/changelog/");
    }

    private static String walkCapturingStdout(String ui) throws Exception {
        String before = System.getProperty(SchemaMigrator.UI_PROPERTY);
        var captured = new ByteArrayOutputStream();
        PrintStream original = System.out;
        var pg = PgContainerHelper.start();
        try {
            setOrClear(ui);
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(pg.getUsername());
            cfg.setPassword(pg.getPassword());
            cfg.setMaximumPoolSize(3);
            try (var ds = new HikariDataSource(cfg)) {
                var outcome = new AtomicReference<SchemaMigrator.MigrationOutcome>();
                var failure = new AtomicReference<Throwable>();
                System.setOut(new PrintStream(captured, true, StandardCharsets.UTF_8));
                Thread walk = new Thread(() -> {
                    try {
                        outcome.set(SchemaMigrator.migrate(ds));
                    } catch (Throwable t) {
                        failure.set(t);
                    }
                }, "nexus-64kwn-walk");
                walk.start();
                walk.join(Duration.ofMinutes(5));
                System.setOut(original);
                assertThat(walk.isAlive()).as("the walk finishes inside five minutes").isFalse();
                assertThat(failure.get()).as("the walk itself succeeds").isNull();
                assertThat(outcome.get().reexecutedChangesets())
                    .as("the runAlways changesets re-run on a migrated clone, so there were lines to print")
                    .isPositive();
            }
        } finally {
            System.setOut(original);
            setOrClear(before);
            pg.stop();
        }
        return captured.toString(StandardCharsets.UTF_8);
    }

    private static void setOrClear(String value) {
        if (value == null) {
            System.clearProperty(SchemaMigrator.UI_PROPERTY);
        } else {
            System.setProperty(SchemaMigrator.UI_PROPERTY, value);
        }
    }
}
