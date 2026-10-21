// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.Container.ExecResult;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.containers.wait.strategy.LogMessageWaitStrategy;

import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

/**
 * nexus-3cwsw: {@code -Dnexus.ddl.count=false} turns the Java DDL counter fully off, so a main run can be
 * compared with one at the same sha. Each test below pins one piece of "off" against a positive control
 * with the counter on, so a switch that stopped switching (or one that switched off the controls too)
 * fails here.
 *
 * <p>Off means the recipe from before the counter (nexus-sz651): no {@code log_statement}, no collector
 * and no other {@code log_*} setting the counter added, no probe script copied into the container, no
 * event trigger, the library's default wait (the ready line on Docker's log, not a {@code pg_isready}
 * exec poll), no marks, no WAL reads, no count files.
 */
class DdlCountSwitchTest {

    /** Every server setting the counter adds to the container's command line. */
    private static final List<String> COUNTER_SETTINGS = List.of(
        "log_statement", "log_line_prefix", "log_timezone", "logging_collector", "log_destination",
        "log_directory", "log_filename", "log_rotation_age", "log_rotation_size", "log_truncate_on_rotation");

    private static <T> T withCount(String value, Callable<T> body) throws Exception {
        String previous = System.getProperty(DdlCounts.COUNT_PROPERTY);
        if (value == null) {
            System.clearProperty(DdlCounts.COUNT_PROPERTY);
        } else {
            System.setProperty(DdlCounts.COUNT_PROPERTY, value);
        }
        try {
            return body.call();
        } finally {
            if (previous == null) {
                System.clearProperty(DdlCounts.COUNT_PROPERTY);
            } else {
                System.setProperty(DdlCounts.COUNT_PROPERTY, previous);
            }
        }
    }

    private static String command(PostgreSQLContainer<?> c) {
        return String.join(" ", c.getCommandParts());
    }

    @Test
    void the_switch_parses_strictly_and_defaults_on() {
        assertThat(DdlCounts.enabled(null)).as("unset is on").isTrue();
        assertThat(DdlCounts.enabled("true")).isTrue();
        assertThat(DdlCounts.enabled("false")).isFalse();
        for (String typo : List.of("", "flase", "0", "off", "FALSE", "False", "no")) {
            assertThatThrownBy(() -> DdlCounts.enabled(typo)).as("'%s' must not leave the counter silently on", typo)
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(DdlCounts.COUNT_PROPERTY);
        }
    }

    @Test
    void an_off_container_carries_none_of_the_counters_settings_and_an_on_container_carries_all() throws Exception {
        PostgreSQLContainer<?> off = withCount("false", PgContainerHelper::newContainer);
        FailFastPostgreSQLContainer offFast = (FailFastPostgreSQLContainer) off;
        for (String setting : COUNTER_SETTINGS) {
            assertThat(command(off)).as("off: no %s", setting).doesNotContain(setting);
        }
        assertThat(command(off)).as("off keeps the durability settings the counter did not add")
            .contains("fsync=off", "synchronous_commit=off", "full_page_writes=off", "max_wal_size=64MB",
                "log_checkpoints=off", "checkpoint_warning=0");
        assertThat(off.getTmpFsMapping()).containsEntry(PgContainerHelper.PGDATA, "rw,size=" + PgContainerHelper.TMPFS_SIZE);
        assertThat(offFast.getCopyToFileContainerPathMap()).as("off: the probe script is not copied in").isEmpty();
        assertThat(offFast.ddlWaitStrategy()).as("off: the library's default wait, no pg_isready exec poll")
            .isInstanceOf(LogMessageWaitStrategy.class);
        assertThat(offFast.ddlCounted()).isFalse();

        PostgreSQLContainer<?> on = withCount(null, PgContainerHelper::newContainer);
        FailFastPostgreSQLContainer onFast = (FailFastPostgreSQLContainer) on;
        for (String setting : COUNTER_SETTINGS) {
            assertThat(command(on)).as("on: %s", setting).contains(setting);
        }
        assertThat(command(on)).contains("logging_collector=on", "log_statement=ddl");
        assertThat(onFast.getCopyToFileContainerPathMap().values()).as("on: the probe script is copied in")
            .containsExactly("/docker-entrypoint-initdb.d/00-nx-ddl-probe.sql");
        assertThat(onFast.ddlWaitStrategy()).as("on: the pg_isready poll").isNotInstanceOf(LogMessageWaitStrategy.class);
        assertThat(onFast.ddlCounted()).isTrue();
    }

    /** A connection that records every call it gets and fails each one. */
    private static Connection deadConnection(AtomicInteger calls) {
        return (Connection) Proxy.newProxyInstance(DdlCountSwitchTest.class.getClassLoader(), new Class<?>[]{Connection.class},
            (proxy, method, args) -> {
                calls.incrementAndGet();
                throw new java.sql.SQLException("dead connection: " + method.getName());
            });
    }

    @SuppressWarnings("unchecked")
    private static long perClassWal(Map<String, Object> state, String owner) {
        Map<String, Object> row = (Map<String, Object>) ((Map<String, Object>) state.get("per_class")).get(owner);
        return row == null ? 0L : ((Number) row.get("wal_bytes")).longValue();
    }

    @Test
    void an_off_counter_touches_no_connection_container_state_or_file_and_an_on_counter_does() throws Exception {
        Path root = Files.createTempDirectory("nx-3cwsw-counts");
        Path dir = root.resolve("never-created");
        String previousDir = System.getProperty(DdlCounts.DIR_PROPERTY);
        System.setProperty(DdlCounts.DIR_PROPERTY, dir.toString());
        // The fork's real live set and totals are put aside: this test registers unstarted containers and
        // records read failures, which the ratchet would read as a killed fork and unreadable logs.
        try (AutoCloseable isolated = DdlCounts.isolateForTest()) {
            AtomicInteger calls = new AtomicInteger();
            FailFastPostgreSQLContainer offContainer = (FailFastPostgreSQLContainer) withCount("false", PgContainerHelper::newContainer);
            FailFastPostgreSQLContainer onContainer = (FailFastPostgreSQLContainer) withCount(null, PgContainerHelper::newContainer);
            // Set up WITH the counter on: one live container (never started: reading its log fails) and one
            // database with a start LSN. Each off call below must leave all of it exactly as it is.
            withCount(null, () -> {
                DdlCounts.register(onContainer);
                DdlCounts.noteDatabase("nexus_test_on", "SwitchOwner", true, 0);
                return null;
            });
            Path file;
            try (Stream<Path> files = Files.list(dir)) {
                file = files.filter(p -> p.toString().endsWith(".json")).findFirst().orElseThrow();
            }
            Files.delete(file);
            Map<String, Object> before = DdlCounts.stateForTest();
            assertThat(before).containsEntry("containers_unread", 1).containsEntry("log_errors", 0);
            assertThat(perClassWal(before, "SwitchOwner")).isZero();

            withCount("false", () -> {
                assertThat(DdlCounts.mark(deadConnection(calls), "start")).isFalse();
                assertThat(DdlCounts.markReady(deadConnection(calls))).isFalse();
                assertThat(DdlCounts.walBytes(deadConnection(calls))).isEqualTo(-1);
                DdlCounts.register(offContainer);
                assertThat(DdlCounts.stateForTest()).as("off: register adds nothing to the live set")
                    .containsEntry("containers_unread", 1);
                DdlCounts.noteDatabase("nexus_test_off", "OffOwner", true, 0);
                DdlCounts.noteRelease("nexus_test_on", 10); // would add 10 WAL bytes to SwitchOwner
                DdlCounts.record(offContainer);
                DdlCounts.record(onContainer); // would read the unstarted container's log and count a log error
                DdlCounts.flush();
                return null;
            });
            assertThat(calls.get()).as("off: no call reached a connection").isZero();
            assertThat(DdlCounts.stateForTest()).as("off: the live set and the totals are untouched").isEqualTo(before);
            assertThat(dir.resolve(file.getFileName())).as("off: no count file written").doesNotExist();

            // Positive controls: the same calls with the counter on reach the connection, move the state
            // and write the file, so the assertions above are not looking at state nothing could change.
            withCount(null, () -> {
                assertThat(DdlCounts.mark(deadConnection(calls), "start")).as("a dead connection fails the mark").isFalse();
                assertThat(DdlCounts.walBytes(deadConnection(calls))).isEqualTo(-1);
                DdlCounts.register(offContainer);
                assertThat(DdlCounts.stateForTest()).containsEntry("containers_unread", 2);
                DdlCounts.noteDatabase("nexus_test_on2", "OnOwner", true, 0);
                DdlCounts.noteRelease("nexus_test_on", 10);
                DdlCounts.record(onContainer);
                DdlCounts.flush();
                return null;
            });
            assertThat(calls.get()).as("on: the calls reached the connection").isPositive();
            Map<String, Object> after = DdlCounts.stateForTest();
            assertThat(after).as("on: record takes the container out of the live set and counts its unreadable log").containsEntry("containers_unread", 1).containsEntry("log_errors", 1);
            assertThat(perClassWal(after, "SwitchOwner")).as("on: noteRelease adds the WAL bytes").isEqualTo(10);
            assertThat(after.get("classes_seen")).as("on: noteDatabase records the owner").isEqualTo(2);
            assertThat(dir.resolve(file.getFileName())).as("on: the count file is written").exists();
        } finally {
            if (previousDir == null) {
                System.clearProperty(DdlCounts.DIR_PROPERTY);
            } else {
                System.setProperty(DdlCounts.DIR_PROPERTY, previousDir);
            }
        }
    }

    private static boolean schemaExists(DSLContext ctx, String schema) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_namespace")),
            DSL.field(DSL.name("nspname"), String.class).eq(schema)) > 0;
    }

    private static int eventTriggers(DSLContext ctx) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_event_trigger")));
    }

    private static String show(DSLContext ctx, String setting) {
        return ctx.select(DSL.function("current_setting", String.class, DSL.val(setting))).fetchOne(0, String.class);
    }

    /** A real off container: boots on the default wait, has no collector, probe, event trigger or file, and writes no count file. */
    @Test
    void an_off_container_boots_without_the_probe_the_collector_or_a_count_file() throws Exception {
        Path root = Files.createTempDirectory("nx-3cwsw-boot");
        Path dir = root.resolve("counts");
        String previousDir = System.getProperty(DdlCounts.DIR_PROPERTY);
        System.setProperty(DdlCounts.DIR_PROPERTY, dir.toString());
        try {
            PostgreSQLContainer<?> c = withCount("false", PgContainerHelper::startDedicated);
            try (Connection conn = c.createConnection("")) {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                assertThat(show(ctx, "log_statement")).isEqualTo("none");
                assertThat(show(ctx, "logging_collector")).isEqualTo("off");
                assertThat(schemaExists(ctx, "nx_ddl_probe")).as("no probe schema").isFalse();
                assertThat(eventTriggers(ctx)).as("no event trigger").isZero();
                ExecResult script = c.execInContainer("ls", "/docker-entrypoint-initdb.d");
                assertThat(script.getStdout()).as("no probe script in the init directory").doesNotContain("nx-ddl-probe");
                assertThat(c.execInContainer("test", "-e", PgContainerHelper.SERVER_LOG_FILE).getExitCode())
                    .as("no server log file").isNotZero();
                assertThat(PgContainerHelper.serverLog(c)).as("the server log is Docker's log again")
                    .contains("database system is ready to accept connections");
            } finally {
                withCount("false", () -> {
                    c.stop();
                    return null;
                });
            }
            assertThat(dir).as("off: stopping a container writes no count file").doesNotExist();
        } finally {
            if (previousDir == null) {
                System.clearProperty(DdlCounts.DIR_PROPERTY);
            } else {
                System.setProperty(DdlCounts.DIR_PROPERTY, previousDir);
            }
        }
    }

    /** The empty shared-cluster database gets the probe only with the counter on. */
    @Test
    void an_empty_shared_database_gets_the_probe_only_with_the_counter_on() throws Exception {
        PgContainerHelper.start().stop(); // boot the fork's shared cluster with the counter on, as every other class does
        PostgreSQLContainer<?> off = withCount("false", PgContainerHelper::startEmptyDatabase);
        try (Connection su = off.createConnection("")) {
            assertThat(schemaExists(DSL.using(su, SQLDialect.POSTGRES), "nx_ddl_probe")).as("off: no probe installed").isFalse();
        } finally {
            withCount("false", () -> {
                off.stop();
                return null;
            });
        }
        PostgreSQLContainer<?> on = withCount(null, PgContainerHelper::startEmptyDatabase);
        try (Connection su = on.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(schemaExists(ctx, "nx_ddl_probe")).as("on: the probe is installed").isTrue();
            assertThat(eventTriggers(ctx)).isEqualTo(1);
        } finally {
            on.stop();
        }
    }
}
