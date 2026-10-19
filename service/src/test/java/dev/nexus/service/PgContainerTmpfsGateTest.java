// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.Container.ExecResult;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
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

    /** Files allowed to construct a PostgreSQLContainer: the factory and its subclass. */
    private static final List<String> ALLOWED = List.of(
        "PgContainerHelper.java", "FailFastPostgreSQLContainer.java");

    private static final Pattern CONSTRUCTION = Pattern.compile(
        "new\\s+(FailFast)?PostgreSQLContainer\\s*(<[^>]*>)?\\s*\\(");

    @Test
    void only_the_factory_constructs_a_postgres_container() throws IOException {
        Path root = Path.of("src", "test", "java");
        List<Path> files;
        try (Stream<Path> s = Files.walk(root)) {
            files = s.filter(p -> p.toString().endsWith(".java")).toList();
        }
        // Non-vacuity: the scan sees the test tree and the factory itself.
        assertThat(files.size()).isGreaterThan(300);
        List<String> offenders = new ArrayList<>();
        int factorySites = 0;
        for (Path p : files) {
            String src = Files.readString(p);
            if (!CONSTRUCTION.matcher(src).find()) {
                continue;
            }
            if (ALLOWED.contains(p.getFileName().toString())) {
                factorySites++;
            } else {
                offenders.add(p.toString());
            }
        }
        assertThat(factorySites).as("the factory's own construction site").isGreaterThanOrEqualTo(1);
        assertThat(offenders)
            .as("build test Postgres containers through PgContainerHelper.newContainer(), "
                + "which puts PGDATA on tmpfs under a memory limit (nexus-xynce)")
            .isEmpty();
    }

    @Test
    void the_factory_puts_pgdata_on_a_capped_tmpfs_and_relaxes_durability() {
        PostgreSQLContainer<?> c = PgContainerHelper.newContainer();
        assertThat(c.getTmpFsMapping())
            .containsEntry(PgContainerHelper.PGDATA, "rw,size=" + PgContainerHelper.TMPFS_SIZE);
        assertThat(String.join(" ", c.getCommandParts()))
            .contains("fsync=off", "synchronous_commit=off", "full_page_writes=off",
                "max_wal_size=64MB", "min_wal_size=32MB");
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
            try (Connection conn = c.createConnection("");
                 Statement st = conn.createStatement()) {
                assertThat(show(st, "synchronous_commit")).isEqualTo("off");
                assertThat(show(st, "full_page_writes")).isEqualTo("off");
                assertThat(show(st, "max_wal_size")).isEqualTo("64MB");
            }
            long memory = c.getDockerClient().inspectContainerCmd(c.getContainerId()).exec()
                .getHostConfig().getMemory();
            assertThat(memory).isEqualTo(PgContainerHelper.MEMORY_LIMIT_BYTES);
        }
    }

    private static String show(Statement st, String setting) throws Exception {
        try (ResultSet rs = st.executeQuery("SHOW " + setting)) {
            rs.next();
            return rs.getString(1);
        }
    }
}
