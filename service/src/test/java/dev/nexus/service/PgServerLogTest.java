// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.Container.ExecResult;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.List;
import java.util.stream.Stream;

/**
 * nexus-sz651 regression: the DDL counter must not be a writer. {@code log_statement=ddl} and the
 * event trigger's {@code RAISE LOG} lines (about 462k per main run) went to the container's stderr,
 * which Docker stores as json-file logs under /var/lib/docker on the host's disk: the main suite
 * wrote 1.63 GB with the counter against 0.48 GB without it (chas NVMe sampler). The server log now
 * goes to a file in the container's tmpfs data directory ({@code logging_collector}), and the counter
 * reads it from there ({@link PgContainerHelper#serverLog}).
 */
class PgServerLogTest {

    private static final int DDL_STATEMENTS = 40;

    /** Create then drop {@code n} tables and one index, through typed jOOQ DDL (RawSqlGateTest counts raw SQL). */
    private static void runDdl(Connection conn, String prefix, int n) {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        for (int i = 0; i < n; i++) {
            ctx.createTable(prefix + "_" + i).column("i", SQLDataType.INTEGER).execute();
        }
        ctx.createIndex(prefix + "_ix").on(DSL.table(DSL.name(prefix + "_0")), DSL.field(DSL.name("i"))).execute();
        for (int i = 0; i < n; i++) {
            ctx.dropTable(prefix + "_" + i).execute();
        }
    }

    private static long count(String text, String needle) {
        // jOOQ renders DDL in lower case; compare without case.
        String n = needle.toLowerCase(java.util.Locale.ROOT);
        return text.lines().filter(l -> l.toLowerCase(java.util.Locale.ROOT).contains(n)).count();
    }

    /** Poll the server log until {@code needle} appears {@code atLeast} times (the collector writes asynchronously). */
    private static String serverLogWith(PostgreSQLContainer<?> c, String needle, long atLeast) throws Exception {
        String log = "";
        for (int attempt = 0; attempt < 50; attempt++) {
            log = PgContainerHelper.serverLog(c);
            if (count(log, needle) >= atLeast) {
                return log;
            }
            Thread.sleep(100);
        }
        return log;
    }

    @Test
    void ddl_statement_lines_reach_the_server_log_file_and_never_the_docker_log() throws Exception {
        PostgreSQLContainer<?> c = PgContainerHelper.startDedicated();
        try (Connection conn = c.createConnection("")) {
            runDdl(conn, "nx_sz651_log", DDL_STATEMENTS);
            // Non-vacuity: the statements really ran and the counter's own signal is in the file.
            String file = serverLogWith(c, "drop table \"nx_sz651_log_", DDL_STATEMENTS);
            assertThat(count(file, "create table \"nx_sz651_log_")).as("top-level CREATE TABLE lines in the file")
                .isGreaterThanOrEqualTo(DDL_STATEMENTS);
            assertThat(count(file, "nx_ddl: ")).as("event-trigger lines in the file").isGreaterThanOrEqualTo(DDL_STATEMENTS);
            DdlLogParser.Parsed parsed = DdlLogParser.parse(file);
            assertThat(parsed.entries().stream().filter(e -> e.kind().equals("create_table") && !e.boot()).count())
                .as("the counter counts the creates from the file").isGreaterThanOrEqualTo(DDL_STATEMENTS);

            // What Docker's log driver stores: no statement line, no event-trigger line, no mark.
            String docker = c.getLogs();
            assertThat(docker).as("Docker's log carries no per-statement line")
                .doesNotContain("statement:").doesNotContain("execute <unnamed>").doesNotContainIgnoringCase("nx_sz651_log_");
            assertThat(docker).doesNotContain("nx_ddl");
            assertThat(docker.length()).as("Docker's log stays a few lines, not one per DDL").isLessThan(8_000);
        } finally {
            c.stop();
        }
    }

    @Test
    void the_server_log_file_sits_on_the_tmpfs_data_directory_and_is_not_rotated_away() throws Exception {
        assertThat(PgContainerHelper.SERVER_LOG_FILE).startsWith(PgContainerHelper.PGDATA + "/");
        PostgreSQLContainer<?> c = PgContainerHelper.startDedicated();
        try {
            ExecResult mount = c.execInContainer("sh", "-c", "df -P " + PgContainerHelper.SERVER_LOG_FILE + " | tail -1");
            assertThat(mount.getStdout()).as("the file's filesystem: %s", mount.getStdout()).containsAnyOf("tmpfs", PgContainerHelper.PGDATA);
            try (Connection conn = c.createConnection("")) {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                assertThat(show(ctx, "logging_collector")).isEqualTo("on");
                assertThat(show(ctx, "log_directory")).isEqualTo(PgContainerHelper.SERVER_LOG_DIR);
                assertThat(show(ctx, "log_rotation_size")).as("rotation off, so a read at an offset stays valid").isEqualTo("0");
                assertThat(show(ctx, "log_rotation_age")).isEqualTo("0");
            }
        } finally {
            c.stop();
        }
    }

    private static String show(DSLContext ctx, String setting) {
        return ctx.select(DSL.function("current_setting", String.class, DSL.val(setting))).fetchOne(0, String.class);
    }

    /**
     * The counter's read path: stopping a container folds its server log into the fork's totals. Reading
     * Docker's log instead finds no statement and counts none, so the delta below is the proof that
     * {@link DdlCounts#record} reads the file.
     */
    @Test
    void stopping_a_container_counts_its_ddl_from_the_server_log() throws Exception {
        Path dir = Files.createTempDirectory("nx-sz651-counts");
        String previous = System.getProperty(DdlCounts.DIR_PROPERTY);
        System.setProperty(DdlCounts.DIR_PROPERTY, dir.toString());
        try {
            DdlCounts.flush();
            JsonNode before = onlyFile(dir);
            PostgreSQLContainer<?> c = PgContainerHelper.startDedicated();
            try (Connection conn = c.createConnection("")) {
                runDdl(conn, "nx_sz651_fold", DDL_STATEMENTS);
            } finally {
                c.stop();
            }
            JsonNode after = onlyFile(dir);
            assertThat(after.at("/buckets/dedicated/total").asLong() - before.at("/buckets/dedicated/total").asLong())
                .as("the dedicated bucket grew by the DDL this test ran").isGreaterThanOrEqualTo(2L * DDL_STATEMENTS);
            assertThat(after.at("/log_errors").asLong()).as("the log was readable").isEqualTo(before.at("/log_errors").asLong());
            assertThat(after.at("/wal/incomplete_containers").asLong())
                .as("start and end marks were both read from the file").isEqualTo(before.at("/wal/incomplete_containers").asLong());
        } finally {
            if (previous == null) {
                System.clearProperty(DdlCounts.DIR_PROPERTY);
            } else {
                System.setProperty(DdlCounts.DIR_PROPERTY, previous);
            }
        }
    }

    private static JsonNode onlyFile(Path dir) throws Exception {
        List<Path> files;
        try (Stream<Path> s = Files.list(dir)) {
            files = s.filter(p -> p.toString().endsWith(".json")).toList();
        }
        assertThat(files).as("this fork's count file").hasSize(1);
        return new ObjectMapper().readTree(files.get(0).toFile());
    }

    /** A read whose exec never returns is abandoned and retried; with every try stuck it fails, it does not hang. */
    @Test
    void a_stuck_server_log_read_is_abandoned_and_retried_not_waited_on_forever() {
        java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
        String got = PgContainerHelper.retryingRead(() -> {
            if (calls.incrementAndGet() == 1) {
                Thread.sleep(60_000); // the first exec never returns
            }
            return "log";
        }, java.time.Duration.ofMillis(300), 3);
        assertThat(got).isEqualTo("log");
        assertThat(calls.get()).as("the stuck try was abandoned and one more ran").isEqualTo(2);

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.retryingRead(() -> {
            Thread.sleep(60_000);
            return "never";
        }, java.time.Duration.ofMillis(100), 2)).isInstanceOf(IllegalStateException.class).hasMessageContaining("timed out 2 times");

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.retryingRead(() -> {
            throw new IllegalStateException("cat exited 1");
        }, java.time.Duration.ofSeconds(5), 3)).isInstanceOf(IllegalStateException.class).hasMessage("cat exited 1");
    }
}
