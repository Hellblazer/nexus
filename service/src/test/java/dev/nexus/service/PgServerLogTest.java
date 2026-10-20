// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.Container.ExecResult;
import org.testcontainers.containers.PostgreSQLContainer;

import dev.nexus.service.FailFastPostgreSQLContainer.ContainerExit;

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
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false has no collector, probe or marks");
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
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false has no collector, probe or marks");
        assertThat(PgContainerHelper.SERVER_LOG_FILE).startsWith(PgContainerHelper.PGDATA + "/");
        PostgreSQLContainer<?> c = PgContainerHelper.startDedicated();
        try {
            // df -PT: Filesystem Type 1024-blocks Used Available Capacity Mounted-on. The image declares PGDATA a
            // VOLUME, so SOME mount sits there either way; only the type tells tmpfs from a Docker volume on disk.
            ExecResult mount = c.execInContainer("sh", "-c", "df -PT " + PgContainerHelper.SERVER_LOG_FILE + " | tail -1");
            String[] cols = mount.getStdout().trim().split("\\s+");
            assertThat(cols).as("df -PT: %s", mount.getStdout()).hasSize(7);
            assertThat(cols[1]).as("the log file's filesystem type").isEqualTo("tmpfs");
            assertThat(cols[6]).as("mounted at the data directory, not a tmpfs elsewhere").isEqualTo(PgContainerHelper.PGDATA);
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
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false has no collector, probe or marks");
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
        }, java.time.Duration.ofMillis(300), 3, "PgServerLogTest");
        assertThat(got).isEqualTo("log");
        assertThat(calls.get()).as("the stuck try was abandoned and one more ran").isEqualTo(2);

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.retryingRead(() -> {
            Thread.sleep(60_000);
            return "never";
        }, java.time.Duration.ofMillis(100), 2, "PgServerLogTest")).isInstanceOf(IllegalStateException.class).hasMessageContaining("timed out 2 times");

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.retryingRead(() -> {
            throw new IllegalStateException("cat exited 1");
        }, java.time.Duration.ofSeconds(5), 3, "PgServerLogTest")).isInstanceOf(IllegalStateException.class).hasMessage("cat exited 1");
    }

    /** Capture what {@link PgContainerHelper}'s logger writes while {@code body} runs. */
    private static List<ch.qos.logback.classic.spi.ILoggingEvent> logged(Runnable body) {
        ch.qos.logback.classic.Logger logger =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(PgContainerHelper.class);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> appender =
            new ch.qos.logback.core.read.ListAppender<>();
        appender.start();
        logger.addAppender(appender);
        try {
            body.run();
        } finally {
            logger.detachAppender(appender);
        }
        return List.copyOf(appender.list);
    }

    /**
     * Every abandoned try leaves a WARN naming the class that booted the container (owner), the class that asked
     * for the read (reader), the attempt and the time waited (nexus-t4y7b).
     */
    @Test
    void an_abandoned_read_is_logged_at_warn_with_owner_attempt_and_elapsed() {
        var calls = new java.util.concurrent.atomic.AtomicInteger();
        var events = logged(() -> PgContainerHelper.retryingRead(() -> {
            if (calls.incrementAndGet() <= 2) {
                Thread.sleep(60_000);
            }
            return "log";
        }, java.time.Duration.ofMillis(200), 3, "SomeIntegrationTest"));
        assertThat(events).as("one WARN per abandoned try").hasSize(2);
        for (int i = 0; i < 2; i++) {
            var e = events.get(i);
            assertThat(e.getLevel()).isEqualTo(ch.qos.logback.classic.Level.WARN);
            assertThat(e.getFormattedMessage()).contains("server_log_read_abandoned", "owner=SomeIntegrationTest",
                "reader=PgServerLogTest", "attempt=" + (i + 1), "of=3");
            long elapsedMs = Long.parseLong(e.getFormattedMessage().replaceAll(".*elapsed_ms=(\\d+).*", "$1"));
            assertThat(elapsedMs).as("waited about the timeout").isBetween(150L, 5_000L);
        }
        // A read that returns in time logs nothing.
        assertThat(logged(() -> PgContainerHelper.retryingRead(() -> "ok", java.time.Duration.ofSeconds(5), 3, "x"))).isEmpty();
    }

    /**
     * The wait is the TCP probe with -h 127.0.0.1: without it pg_isready asks the unix socket, which the image's
     * init-time temp server also answers, so the container would count as ready before the real server is up.
     * The strategy's command is a private field, read here by reflection.
     */
    @Test
    void the_counter_on_wait_is_the_tcp_pg_isready_probe_with_a_60_second_attempt() throws Exception {
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false keeps the library's default wait");
        FailFastPostgreSQLContainer c = (FailFastPostgreSQLContainer) PgContainerHelper.newContainer();
        var strategy = c.ddlWaitStrategy();
        assertThat(strategy).isInstanceOf(org.testcontainers.containers.wait.strategy.ShellStrategy.class);
        var command = org.testcontainers.containers.wait.strategy.ShellStrategy.class.getDeclaredField("command");
        command.setAccessible(true);
        var startupTimeout = org.testcontainers.containers.wait.strategy.AbstractWaitStrategy.class.getDeclaredField("startupTimeout");
        startupTimeout.setAccessible(true);
        assertThat((String) command.get(strategy)).isEqualTo(PgContainerHelper.READY_PROBE)
            .contains("pg_isready -h 127.0.0.1 ", "-p 5432");
        assertThat((java.time.Duration) startupTimeout.get(strategy)).isEqualTo(PgContainerHelper.STARTUP_TIMEOUT)
            .isEqualTo(java.time.Duration.ofSeconds(60));
    }

    /** {@code ExecResult} is final with a package-private constructor; a stub has to build one reflectively. */
    private static ExecResult execResult(int exit, String out, String err) {
        try {
            var ctor = ExecResult.class.getDeclaredConstructor(int.class, String.class, String.class);
            ctor.setAccessible(true);
            return ctor.newInstance(exit, out, err);
        } catch (ReflectiveOperationException e) {
            throw new AssertionError(e);
        }
    }

    /** A counter-on container whose exec and Docker log are scripted (it never reaches Docker: nothing starts it). */
    private static final class StubContainer extends FailFastPostgreSQLContainer {
        interface Exec {
            ExecResult run(String... command) throws Exception;
        }

        volatile Exec exec = command -> execResult(0, "", "");
        volatile String dockerLog = "docker log";
        /** Scripted answer to the daemon's inspect (nexus-ccg9y); a throwing one models a daemon that cannot be asked. */
        volatile java.util.concurrent.Callable<ContainerExit> inspect =
            () -> new ContainerExit("running", 0L, false);
        final java.util.List<List<String>> execs = new java.util.concurrent.CopyOnWriteArrayList<>();

        StubContainer() {
            super(org.testcontainers.utility.DockerImageName.parse(PgContainerHelper.IMAGE));
            ddlOwner("StubbedTest");
        }

        @Override
        public ExecResult execInContainer(String... command) throws java.io.IOException, InterruptedException {
            execs.add(List.of(command));
            try {
                return exec.run(command);
            } catch (java.io.IOException | InterruptedException | RuntimeException e) {
                throw e;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }

        @Override
        ContainerExit inspectExit() {
            try {
                return inspect.call();
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }

        @Override
        public String getLogs() {
            return dockerLog;
        }
    }

    /** What {@link PgContainerHelper#serverLog} does is pinned through the public method on a counter-on container stub. */
    @Test
    void serverLog_cats_the_file_and_retries_a_stuck_exec_three_times() {
        StubContainer c = new StubContainer();
        assertThat(c.ddlCounted()).isTrue();

        // Path and shape: one `cat` of the log file, its stdout returned.
        c.exec = command -> execResult(0, "the log", "");
        assertThat(PgContainerHelper.serverLog(c)).isEqualTo("the log");
        assertThat(c.execs).containsExactly(List.of("cat", PgContainerHelper.SERVER_LOG_FILE));

        // The production numbers: 60 s a try, 3 tries.
        assertThat(PgContainerHelper.SERVER_LOG_READ_TIMEOUT).isEqualTo(java.time.Duration.ofSeconds(60));
        assertThat(PgContainerHelper.SERVER_LOG_READ_ATTEMPTS).isEqualTo(3);

        // Wiring: with the per-try wait shortened for this thread, the PUBLIC method abandons a stuck exec and
        // tries again, twice over, and gives up on the third.
        PgContainerHelper.SERVER_LOG_READ_TIMEOUT_OVERRIDE.set(java.time.Duration.ofMillis(200));
        try {
            var calls = new java.util.concurrent.atomic.AtomicInteger();
            c.exec = command -> {
                if (calls.incrementAndGet() <= 2) {
                    Thread.sleep(60_000);
                }
                return execResult(0, "after two stuck", "");
            };
            assertThat(PgContainerHelper.serverLog(c)).isEqualTo("after two stuck");
            assertThat(calls.get()).as("two abandoned tries, the third read").isEqualTo(3);

            var stuck = new java.util.concurrent.atomic.AtomicInteger();
            c.exec = command -> {
                stuck.incrementAndGet();
                Thread.sleep(60_000);
                return execResult(0, "never", "");
            };
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.serverLog(c))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("timed out 3 times");
            assertThat(stuck.get()).isEqualTo(3);
        } finally {
            PgContainerHelper.SERVER_LOG_READ_TIMEOUT_OVERRIDE.remove();
        }
    }

    /**
     * nexus-ccg9y (2): with no per-thread override, the PUBLIC read path hands its reader the 60 s per-try wait and the
     * 3 tries. The reader is injected so nothing waits 60 s; the same call with the override set shows the capture
     * does see the other path, so a reader that recorded a constant would not pass both.
     */
    @Test
    void serverLog_with_no_override_hands_the_reader_the_production_timeout_and_attempts() {
        StubContainer c = new StubContainer();
        c.exec = command -> execResult(0, "the log", "");
        var seen = new java.util.ArrayList<Object[]>();
        PgContainerHelper.LogReader recording = (read, timeout, attempts, owner) -> {
            seen.add(new Object[] {timeout, attempts, owner});
            try {
                return read.call();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        assertThat(PgContainerHelper.SERVER_LOG_READ_TIMEOUT_OVERRIDE.get()).as("no override on this thread").isNull();

        assertThat(PgContainerHelper.serverLog(c, recording)).isEqualTo("the log");

        assertThat(seen).hasSize(1);
        assertThat(seen.get(0)[0]).as("the per-try wait").isEqualTo(java.time.Duration.ofSeconds(60));
        assertThat(seen.get(0)[1]).as("the tries").isEqualTo(3);
        assertThat(seen.get(0)[2]).as("the container's owner").isEqualTo("StubbedTest");
        assertThat(c.execs).hasSize(1);
        assertThat(c.execs.get(0)).startsWith("cat");

        PgContainerHelper.SERVER_LOG_READ_TIMEOUT_OVERRIDE.set(java.time.Duration.ofMillis(200));
        try {
            PgContainerHelper.serverLog(c, recording);
        } finally {
            PgContainerHelper.SERVER_LOG_READ_TIMEOUT_OVERRIDE.remove();
        }
        assertThat(seen).hasSize(2);
        assertThat(seen.get(1)[0]).as("control: the override is what the reader sees when it is set")
            .isEqualTo(java.time.Duration.ofMillis(200));
    }

    private static <C extends FailFastPostgreSQLContainer> List<ch.qos.logback.classic.spi.ILoggingEvent> failureEvents(
            java.util.function.Consumer<C> body, C c) {
        ch.qos.logback.classic.Logger logger =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(FailFastPostgreSQLContainer.class);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> appender =
            new ch.qos.logback.core.read.ListAppender<>();
        appender.start();
        logger.addAppender(appender);
        try {
            body.accept(c);
            return List.copyOf(appender.list);
        } finally {
            logger.detachAppender(appender);
        }
    }

    /**
     * nexus-ccg9y (1): when the postmaster dies the container exits and its tmpfs log goes with it. The failure line then
     * carries the container's state and exit code, which is the only diagnosis left, and the unreadable-log branch
     * prints its own marker.
     */
    @Test
    void a_start_wait_failure_with_an_unreadable_log_still_names_the_container_state_and_exit_code() {
        StubContainer c = new StubContainer();
        c.exec = command -> {
            throw new IllegalStateException("container is not running");
        };
        c.inspect = () -> new ContainerExit("exited", 137L, true);

        var events = failureEvents(x -> x.reportStartWaitFailure(new IllegalStateException("wait timed out")), c);

        assertThat(c.startFailureLogTail()).startsWith("(server log unreadable:").contains("container is not running");
        assertThat(events).singleElement().satisfies(e -> {
            assertThat(e.getLevel()).isEqualTo(ch.qos.logback.classic.Level.ERROR);
            assertThat(e.getFormattedMessage())
                .contains("event=start_wait_failed", "owner=StubbedTest", "wait timed out",
                    "container_state=exited exit_code=137 oom_killed=true", "(server log unreadable:");
        });
    }

    @Test
    void a_daemon_that_cannot_be_inspected_never_masks_the_original_wait_failure() {
        StubContainer c = new StubContainer();
        c.exec = command -> execResult(0, "tail of the log", "");
        c.inspect = () -> {
            throw new IllegalStateException("docker is gone");
        };

        var events = failureEvents(x -> x.reportStartWaitFailure(new IllegalStateException("wait timed out")), c);

        assertThat(events).singleElement().satisfies(e -> assertThat(e.getFormattedMessage())
            .contains("wait timed out", "container_state=unreadable", "docker is gone", "tail of the log"));
        c.inspect = () -> null;
        assertThat(c.containerStateForLog()).isEqualTo("container_state=unknown (inspect returned no state)");
    }

    /**
     * nexus-ccg9y (F1): the inspect is one unbounded docker-java call on the failure path. A daemon that never answers
     * costs the report the inspect's timeout, not the whole run: the line still goes out, says the state timed out, and
     * carries the log tail and the original wait failure.
     */
    @Test
    void an_inspect_that_never_returns_is_abandoned_and_the_failure_line_still_goes_out() throws Exception {
        StubContainer c = new StubContainer();
        c.exec = command -> execResult(0, "tail of the log", "");
        java.util.concurrent.CountDownLatch release = new java.util.concurrent.CountDownLatch(1);
        c.inspect = () -> {
            release.await(20, java.util.concurrent.TimeUnit.SECONDS);
            return new ContainerExit("running", 0L, false);
        };
        FailFastPostgreSQLContainer.INSPECT_TIMEOUT_OVERRIDE.set(java.time.Duration.ofMillis(300));
        try {
            long t0 = System.nanoTime();
            var events = failureEvents(x -> x.reportStartWaitFailure(new IllegalStateException("wait timed out")), c);
            long millis = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);

            assertThat(millis).as("the report waited for the inspect's timeout and no longer").isBetween(250L, 10_000L);
            assertThat(events).singleElement().satisfies(e -> assertThat(e.getFormattedMessage())
                .contains("wait timed out", "container_state=unreadable", "timed out after", "tail of the log"));
        } finally {
            FailFastPostgreSQLContainer.INSPECT_TIMEOUT_OVERRIDE.remove();
            release.countDown();
        }
        assertThat(FailFastPostgreSQLContainer.INSPECT_TIMEOUT).as("the production wait is bounded too")
            .isEqualTo(java.time.Duration.ofSeconds(15));
    }

    /**
     * nexus-ccg9y (B2), against a real daemon: a container whose process was killed is {@code exited} with code 137 to
     * a FRESH inspect, while the response cached when the container was created (what {@code getContainerInfo()}
     * returns) still says it is running, which is why {@code inspectExit} asks the daemon again. The failure line then
     * carries the real state although the log file went with the container's tmpfs.
     */
    @Test
    void a_killed_container_is_exited_137_to_a_fresh_inspect_and_the_failure_line_says_so() throws Exception {
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false has no collector");
        FailFastPostgreSQLContainer c = (FailFastPostgreSQLContainer) PgContainerHelper.newContainer();
        try {
            c.start();
            String id = c.getContainerId();
            assertThat(c.inspectExit().status()).as("before the kill").isEqualTo("running");

            c.getDockerClient().killContainerCmd(id).exec();
            long end = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(30);
            while (c.getDockerClient().inspectContainerCmd(id).exec().getState().getRunning() == Boolean.TRUE
                && System.nanoTime() < end) {
                Thread.sleep(100);
            }

            ContainerExit exit = c.inspectExit();
            assertThat(exit.status()).isEqualTo("exited");
            assertThat(exit.exitCode()).as("SIGKILL").isEqualTo(137L);
            assertThat(c.getContainerInfo().getState().getStatus())
                .as("control: the cached info is the creation-time response, so reading it would miss the exit")
                .isNotEqualTo("exited");

            var events = failureEvents(x -> x.reportStartWaitFailure(new IllegalStateException("wait timed out")), c);
            assertThat(events).singleElement().satisfies(e -> assertThat(e.getFormattedMessage())
                .contains("event=start_wait_failed", "container_state=exited exit_code=137"));
        } finally {
            c.stop();
        }
    }

    @Test
    void a_counter_off_container_reads_dockers_log_not_the_file() {
        StubContainer off = new StubContainer();
        off.ddlCounted(false);
        assertThat(PgContainerHelper.serverLog(off)).isEqualTo("docker log");
        assertThat(off.execs).as("no exec: there is no file").isEmpty();
    }

    /** Written only by the real server (the init-time temp server has listen_addresses empty). */
    private static final String REAL_SERVER_LISTENING = "listening on IPv4 address \"0.0.0.0\"";

    /**
     * A start whose wait times out while the server is up prints the end of the log FILE, which Testcontainers'
     * own failed-start dump (Docker's log) does not carry. The wait is the production probe, so the REAL
     * server is up and listening, followed by a probe that can never pass: the failure comes only after the
     * server is ready, however slow the host. The asserted line is one only the real server writes (the
     * image's init-time temp server listens on the unix socket only).
     */
    @Test
    void a_failed_start_wait_prints_the_tail_of_the_server_log_file() {
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false has no collector");
        FailFastPostgreSQLContainer c = (FailFastPostgreSQLContainer) PgContainerHelper.newContainer();
        c.withStartupAttempts(1).waitingFor(new org.testcontainers.containers.wait.strategy.WaitAllStrategy(
                org.testcontainers.containers.wait.strategy.WaitAllStrategy.Mode.WITH_INDIVIDUAL_TIMEOUTS_ONLY)
            .withStrategy(org.testcontainers.containers.wait.strategy.Wait.forSuccessfulCommand(PgContainerHelper.READY_PROBE)
                .withStartupTimeout(PgContainerHelper.STARTUP_TIMEOUT))
            .withStrategy(org.testcontainers.containers.wait.strategy.Wait.forSuccessfulCommand("false")
                .withStartupTimeout(java.time.Duration.ofSeconds(2))));
        var events = new java.util.ArrayList<ch.qos.logback.classic.spi.ILoggingEvent>();
        ch.qos.logback.classic.Logger logger =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(FailFastPostgreSQLContainer.class);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> appender =
            new ch.qos.logback.core.read.ListAppender<>();
        appender.start();
        logger.addAppender(appender);
        try {
            org.assertj.core.api.Assertions.assertThatThrownBy(c::start)
                .isInstanceOf(org.testcontainers.containers.ContainerLaunchException.class);
        } finally {
            logger.detachAppender(appender);
            events.addAll(appender.list);
            c.stop();
        }
        assertThat(c.startFailureLogTail()).as("the file's tail, captured while the container was up")
            .contains(REAL_SERVER_LISTENING);
        assertThat(events).anySatisfy(e -> {
            assertThat(e.getLevel()).isEqualTo(ch.qos.logback.classic.Level.ERROR);
            assertThat(e.getFormattedMessage()).contains("start_wait_failed", REAL_SERVER_LISTENING)
                .as("the failed wait names the container's state (nexus-ccg9y); here the server is up")
                .contains("container_state=running exit_code=0");
        });
    }
}
