// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.Container.ExecResult;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-xynce: every test Postgres container keeps its data directory on a size-capped
 * tmpfs, runs under a memory limit just above that cap, and turns off the durability
 * settings a throwaway cluster does not need. Before this, PGDATA lived in Docker's
 * overlay on chas's NVMe and the engine suites wrote about 50 MB/s to it (measured
 * 2026-10-19). The containers run under the Docker daemon, outside every lab-run
 * grant, so the memory limit is what bounds them; the pool reserves cap times the
 * most containers a run holds.
 *
 * <p>Two halves: a source scan that keeps {@link PgContainerHelper#newContainer} the
 * only place a Postgres container is built, and a live container that proves the
 * settings took effect.
 */
class PgContainerTmpfsGateTest {

    private static final Path PKG = Path.of("src", "test", "java", "dev", "nexus", "service");

    /** The factory, the only file that may construct a container or name a Postgres image. */
    private static final Path FACTORY = PKG.resolve("PgContainerHelper.java");

    /**
     * The subclasses that may exist. SharedDatabaseHandle never starts a container of its own; PgServerLogTest's
     * StubContainer extends FailFastPostgreSQLContainer to script an exec and is never started (nexus-t4y7b).
     * Any other file subclassing a container, FailFast included, is an offender.
     */
    private static final List<Path> SUBCLASSES = List.of(
        PKG.resolve("FailFastPostgreSQLContainer.java"), PKG.resolve("SharedDatabaseHandle.java"),
        PKG.resolve("PgServerLogTest.java"));

    /** Ways to get a Postgres container without the factory. */
    private static final List<Pattern> BYPASSES = List.of(
        // new PostgreSQLContainer<>(...), qualified or not, and any subclass named *PostgreSQLContainer
        Pattern.compile("new\\s+([\\w.]+\\.)?\\w*PostgreSQLContainer\\b"),
        Pattern.compile("PostgreSQLContainer\\s*::\\s*new"),
        // a GenericContainer or ImageFromDockerfile on a Postgres image, or a jdbc:tc: URL
        Pattern.compile("\"(pgvector/pgvector[^\"]*|postgres:(?!//)[^\"]+)\""),
        Pattern.compile("jdbc:tc:"));

    // \w* so a subclass of FailFastPostgreSQLContainer (the factory's own type) is caught as well.
    private static final Pattern SUBCLASSING = Pattern.compile(
        "extends\\s+([\\w.]+\\.)?\\w*PostgreSQLContainer\\b");

    /**
     * String literals, line comments and block comments in one pass, so a {@code "/*"}
     * inside a string is not read as a comment start. {@link #stripComments} keeps the
     * strings and drops the comments, so prose that names a pattern does not trip the scan.
     */
    private static final Pattern COMMENTS = Pattern.compile(
        // Unrolled string branch: one recursion per escape, not per character (the per-character form
        // overflows the stack on a long literal; DbStartClassificationGateTest, Service CI 39328496341).
        "\"[^\"\\\\]*+(?:\\\\.[^\"\\\\]*+)*+\"|'(?:\\\\.|[^'\\\\])'|//[^\\n]*|/\\*.*?\\*/", Pattern.DOTALL);

    static String stripComments(String src) {
        return COMMENTS.matcher(src).replaceAll(
            m -> m.group().startsWith("\"") || m.group().startsWith("'")
                ? java.util.regex.Matcher.quoteReplacement(m.group()) : "");
    }

    /**
     * Files that may call {@code .getLogs()}: the factory (the counter-off branch of {@code serverLog}, where
     * Docker's log IS the server log) and the test that asserts Docker's log stays empty of statements.
     */
    private static final List<Path> GET_LOGS_ALLOWED = List.of(
        FACTORY, PKG.resolve("PgServerLogTest.java"));

    // A call, or a method reference (`c::getLogs`, `PostgreSQLContainer::getLogs`) that a stream or a retry hands on (nexus-ccg9y).
    private static final Pattern GET_LOGS = Pattern.compile("\\.\\s*getLogs\\s*\\(|::\\s*getLogs\\b");

    /**
     * nexus-t4y7b: with the DDL counter on, Docker's log of a test Postgres container holds only the
     * postmaster's first lines (the server log is a file in the container), so a NEGATIVE assertion on
     * {@code getLogs()} (no deadlock, no ERROR) passes vacuously and a positive one times out. Readers call
     * {@link PgContainerHelper#serverLog}. Non-vacuity: both allowed files really contain a call, so a scan
     * that read nothing, or a pattern that stopped matching, fails here and not silently.
     */
    @Test
    void nothing_but_the_factory_and_its_pin_reads_a_containers_docker_log() throws IOException {
        Path root = Path.of("src", "test", "java");
        List<Path> files;
        try (Stream<Path> s = Files.walk(root)) {
            files = s.filter(p -> p.toString().endsWith(".java")).toList();
        }
        assertThat(files.size()).isGreaterThan(300);
        for (Path allowed : GET_LOGS_ALLOWED) {
            assertThat(files).as("the scan reaches %s", allowed).contains(allowed);
            assertThat(GET_LOGS.matcher(stripComments(Files.readString(allowed))).find())
                .as("%s calls getLogs(), so the pattern and the scan are alive", allowed).isTrue();
        }
        List<String> offenders = new ArrayList<>();
        for (Path p : files) {
            if (GET_LOGS_ALLOWED.contains(p) || p.getFileName().toString().equals("PgContainerTmpfsGateTest.java")) {
                continue;
            }
            if (GET_LOGS.matcher(stripComments(Files.readString(p))).find()) {
                offenders.add(p.toString());
            }
        }
        assertThat(offenders)
            .as("read a Postgres container's server log with PgContainerHelper.serverLog(container); "
                + "getLogs() is Docker's log, empty of statements with the counter on (nexus-sz651, nexus-t4y7b)")
            .isEmpty();
        // The pattern sees the spellings it exists for and not prose.
        for (String hit : List.of("c.getLogs()", "pg.getLogs ( )", "container\n    .getLogs()",
                "containers.stream().map(PostgreSQLContainer::getLogs)", "retry(c::getLogs)", "Supplier<String> s = c :: getLogs;",
                "map(FailFastPostgreSQLContainer::getLogs)")) {
            assertThat(GET_LOGS.matcher(stripComments(hit)).find()).as(hit).isTrue();
        }
        assertThat(GET_LOGS.matcher(stripComments("// pg.getLogs() in a comment\nvar x = serverLog(c);")).find()).isFalse();
        assertThat(GET_LOGS.matcher(stripComments("// map(Container::getLogs) in a comment\nvar x = serverLog(c);")).find()).isFalse();
        // Near misses are not reads of the Docker log.
        for (String miss : List.of("c.getLogsDir()", "x::getLogsDir", "serverLog(c)", "String getLogs = \"a\";")) {
            assertThat(GET_LOGS.matcher(stripComments(miss)).find()).as(miss).isFalse();
        }
    }

    @Test
    void only_the_factory_constructs_a_postgres_container() throws IOException {
        Path root = Path.of("src", "test", "java");
        List<Path> files;
        try (Stream<Path> s = Files.walk(root)) {
            files = s.filter(p -> p.toString().endsWith(".java")).toList();
        }
        // Non-vacuity: the scan sees the test tree, and the factory trips its own patterns.
        assertThat(files.size()).isGreaterThan(300);
        assertThat(files).contains(FACTORY);
        String factory = stripComments(Files.readString(FACTORY));
        assertThat(BYPASSES.stream().filter(b -> b.matcher(factory).find()).count())
            .as("the factory's own construction and image literal").isGreaterThanOrEqualTo(2);
        List<String> offenders = new ArrayList<>();
        for (Path p : files) {
            if (p.equals(FACTORY) || p.getFileName().toString().equals("PgContainerTmpfsGateTest.java")) {
                continue;
            }
            String src = stripComments(Files.readString(p));
            for (Pattern b : BYPASSES) {
                if (b.matcher(src).find()) {
                    offenders.add(p + ": " + b.pattern());
                }
            }
            if (SUBCLASSING.matcher(src).find() && !SUBCLASSES.contains(p)) {
                offenders.add(p + ": extends PostgreSQLContainer");
            }
        }
        assertThat(offenders)
            .as("build test Postgres containers through PgContainerHelper.newContainer(), "
                + "which puts PGDATA on tmpfs under a memory limit (nexus-xynce)")
            .isEmpty();
    }

    /** Positive control: every bypass spelling the scan exists for is caught, and nothing near it is. */
    @Test
    void the_scan_patterns_catch_every_bypass_spelling() {
        List<String> dodges = List.of(
            "var c = new PostgreSQLContainer<>(img);",
            "c = new org.testcontainers.containers.PostgreSQLContainer<>(img);",
            "c = new\n    PostgreSQLContainer(img);",
            "c = new MyPostgreSQLContainer(img);",
            "Function<DockerImageName, ?> f = PostgreSQLContainer :: new;",
            "new GenericContainer<>(\"pgvector/pgvector:pg17\")",
            "new GenericContainer<>(\"postgres:17\")",
            "String url = \"jdbc:tc:postgresql:17:///db\";");
        for (String d : dodges) {
            assertThat(BYPASSES.stream().anyMatch(b -> b.matcher(stripComments(d)).find()))
                .as("caught: %s", d).isTrue();
        }
        assertThat(SUBCLASSING.matcher("class X extends org.testcontainers.containers.PostgreSQLContainer<X> {").find())
            .isTrue();
        assertThat(SUBCLASSING.matcher("private static final class S extends FailFastPostgreSQLContainer {").find())
            .as("a subclass of the factory's own container type").isTrue();
        assertThat(SUBCLASSING.matcher("class S extends dev.nexus.service.FailFastPostgreSQLContainer {").find())
            .as("the same, qualified").isTrue();
        List<String> clean = List.of(
            "String url = \"jdbc:postgresql://localhost:5432/postgres\";",
            "String u = \"postgres://u@h/db\";",
            ".withDatabaseName(\"postgres\")",
            "// new PostgreSQLContainer<>(img) in a comment",
            "/* \"pgvector/pgvector:pg17\" in a block comment */");
        for (String c : clean) {
            assertThat(BYPASSES.stream().noneMatch(b -> b.matcher(stripComments(c)).find()))
                .as("not a bypass: %s", c).isTrue();
        }
        // A quote in a char literal must not open a fake string.
        assertThat(stripComments("char q = '\"'; var c = new PostgreSQLContainer<>(i); char r = '\"';"))
            .contains("new PostgreSQLContainer");
        // A "/*" inside a string must not swallow the code after it.
        assertThat(stripComments("String g = \"**/*.java\"; var c = new PostgreSQLContainer<>(i); // */"))
            .contains("new PostgreSQLContainer");
    }

    @Test
    void sizes_are_whole_gibibytes_only() {
        assertThat(PgContainerHelper.gibibytes("2g")).isEqualTo(2L * 1024 * 1024 * 1024);
        for (String bad : List.of("512m", "0g", "1G", "g", "1.5g")) {
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.gibibytes(bad))
                .as(bad).isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    void a_sized_container_must_leave_room_above_its_tmpfs() {
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> PgContainerHelper.newContainer(
                org.testcontainers.utility.DockerImageName.parse(PgContainerHelper.IMAGE), "2g", 2L * 1024 * 1024 * 1024))
            .isInstanceOf(IllegalArgumentException.class);
        PostgreSQLContainer<?> big = PgContainerHelper.newContainer(
            org.testcontainers.utility.DockerImageName.parse(PgContainerHelper.IMAGE), "4g", 6L * 1024 * 1024 * 1024);
        assertThat(big.getTmpFsMapping()).containsEntry(PgContainerHelper.PGDATA, "rw,size=4g");
    }

    @Test
    void the_factory_puts_pgdata_on_a_capped_tmpfs_and_relaxes_durability() {
        PostgreSQLContainer<?> c = PgContainerHelper.newContainer();
        assertThat(c.getTmpFsMapping())
            .containsEntry(PgContainerHelper.PGDATA, "rw,size=" + PgContainerHelper.TMPFS_SIZE);
        assertThat(String.join(" ", c.getCommandParts()))
            .contains("fsync=off", "synchronous_commit=off", "full_page_writes=off",
                "max_wal_size=64MB", "min_wal_size=32MB", "wal_keep_size=0",
                "log_checkpoints=off", "checkpoint_warning=0");
        assertThat(PgContainerHelper.MEMORY_LIMIT_BYTES)
            .as("memory limit sits above the tmpfs cap, since tmpfs pages are charged to the container")
            .isGreaterThan(PgContainerHelper.tmpfsSizeBytes());
    }

    @Test
    void a_started_container_runs_on_tmpfs_with_the_settings_and_the_limit() throws Exception {
        try (PostgreSQLContainer<?> c = PgContainerHelper.newContainer()) {
            c.start();
            ExecResult mounts = c.execInContainer("cat", "/proc/mounts");
            assertThat(mounts.getStdout().lines()
                .filter(l -> l.split(" ").length > 2 && l.split(" ")[1].equals(PgContainerHelper.PGDATA))
                .map(l -> l.split(" ")[2]))
                .as("PGDATA mount type")
                .containsExactly("tmpfs");
            ExecResult pgdata = c.execInContainer("sh", "-c", "echo $PGDATA");
            assertThat(pgdata.getStdout().strip()).isEqualTo(PgContainerHelper.PGDATA);
            try (Connection conn = c.createConnection("")) {
                assertThat(show(conn, "synchronous_commit")).isEqualTo("off");
                assertThat(show(conn, "full_page_writes")).isEqualTo("off");
                assertThat(show(conn, "max_wal_size")).isEqualTo("64MB");
                assertThat(show(conn, "min_wal_size")).isEqualTo("32MB");
                assertThat(show(conn, "wal_keep_size")).isEqualTo("0");
            }
            var inspect = c.getDockerClient().inspectContainerCmd(c.getContainerId()).exec();
            assertThat(inspect.getHostConfig().getMemory()).isEqualTo(PgContainerHelper.MEMORY_LIMIT_BYTES);
            assertThat(inspect.getHostConfig().getMemorySwap())
                .as("swap capped too, so the limit is not doubled by swap")
                .isEqualTo(PgContainerHelper.MEMORY_LIMIT_BYTES);
            assertThat(inspect.getConfig().getLabels())
                .containsEntry("dev.nexus.test.run", System.getenv().getOrDefault("NX_TEST_RUN_ID", ""));
        }
    }

    /** A server setting as SHOW renders it, read through typed jOOQ (RawSqlGateTest forbids raw SQL here). */
    private static String show(Connection conn, String setting) {
        return DSL.using(conn, SQLDialect.POSTGRES)
            .select(DSL.function("current_setting", String.class, DSL.val(setting)))
            .fetchOne(0, String.class);
    }

    /** A 200k-character string survives and a comment after it goes, without a StackOverflowError: the per-character (?:a|b)* form overflowed the stack on long input (Service CI 39328496341) */
    @Test
    void stripComments_handlesAHugeLiteralWithoutOverflowingTheStack() {
        String big = "x".repeat(200_000);
        assertThat(stripComments("String a = \"" + big + "\"; // gone")).isEqualTo("String a = \"" + big + "\"; ");
    }
}
