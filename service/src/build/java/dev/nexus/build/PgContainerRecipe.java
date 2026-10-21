// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.build;

import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * The one recipe for a throwaway Postgres container (nexus-xynce, nexus-llba1): image, credentials, PGDATA on a
 * capped tmpfs, the memory limit, the relaxed server flags, {@code sslmode=disable}, three start attempts and the
 * run labels. Both builders of such a container call it:
 *
 * <ul>
 *   <li>{@code dev.nexus.service.PgContainerHelper.newContainer(...)}, the test suite's factory (it adds the DDL
 *       counter's flags and wait, and builds a {@code FailFastPostgreSQLContainer});</li>
 *   <li>{@code JooqCodegen}, the jOOQ code generator, which runs before the test classes compile.</li>
 * </ul>
 *
 * <p>Where it lives: this file is under {@code service/src/build/java} in a named package, so that (a) the JDK
 * source launcher (JEP 458, multi-file mode: JDK 22 and later; the build is on 25) compiles it with
 * {@code JooqCodegen.java} by finding {@code dev/nexus/build/PgContainerRecipe.java} relative to the directory of
 * the file it was asked to run, and (b) service/pom.xml adds {@code src/build/java} as a test source root (with
 * the unnamed-package source programs excluded), so the test tree can call it. A class in the unnamed package
 * cannot be imported from {@code dev.nexus.service}.
 *
 * <p>It compiles against Testcontainers only. Keep it that way: the runner's classpath is the test classpath and
 * nothing else.
 */
public final class PgContainerRecipe {

    /** Image used for every throwaway Postgres container of the service module. */
    public static final String IMAGE = "pgvector/pgvector:pg17";

    /** Superuser database name (matches io.zonky default). */
    public static final String DATABASE = "postgres";

    /** Superuser username (matches io.zonky default). */
    public static final String USERNAME = "postgres";

    /** Superuser password. */
    public static final String PASSWORD = "postgres";

    /**
     * The image's data directory. nexus-xynce: it is mounted as tmpfs, because in Docker's overlay it sat on the
     * host disk and the engine suites wrote about 50 MB/s to chas's NVMe (measured 2026-10-19).
     */
    public static final String PGDATA = "/var/lib/postgresql/data";

    /**
     * tmpfs cap for {@link #PGDATA}. Measured live containers on chas held 19 to 171 MB (most of it WAL before
     * the max_wal_size=64MB setting below, a soft target). 1 GB was too tight: on CI the shared per-fork cluster
     * hit ENOSPC in ChashVectorConcurrencyTest. The cap makes a runaway fail with "No space left on device"
     * instead of eating host memory.
     */
    public static final String TMPFS_SIZE = "2g";

    /**
     * Container memory limit (swap included). tmpfs pages are charged to the container, so the limit is the tmpfs
     * cap plus room for shared_buffers and backends. Containers run under the Docker daemon, outside any lab-run
     * grant: this limit is what bounds each one, and the pool reserves limit times the most containers a run holds.
     */
    public static final long MEMORY_LIMIT_BYTES = 2560L * 1024 * 1024;

    private PgContainerRecipe() {}

    /** {@code "<n>g"} in bytes; the only form these sizes are written in. */
    public static long gibibytes(String size) {
        if (!size.matches("[1-9][0-9]*g")) {
            throw new IllegalArgumentException("tmpfs size must be <n>g, got " + size);
        }
        return Long.parseLong(size.substring(0, size.length() - 1)) * 1024 * 1024 * 1024;
    }

    /** The server command every throwaway container runs, before any caller's extra flags. */
    public static List<String> baseCommand() {
        return List.of("postgres",
            "-c", "fsync=off",
            "-c", "synchronous_commit=off",
            "-c", "full_page_writes=off",
            "-c", "max_wal_size=64MB",
            "-c", "min_wal_size=32MB",
            "-c", "wal_keep_size=0",
            // The smaller max_wal_size checkpoints every ~25 MB of WAL; do not log each one.
            "-c", "log_checkpoints=off",
            "-c", "checkpoint_warning=0");
    }

    /** A configured, unstarted container on {@link #IMAGE} with the default tmpfs and memory limit. */
    public static PostgreSQLContainer<?> newContainer(String caller) {
        PostgreSQLContainer<?> container =
            new PostgreSQLContainer<>(DockerImageName.parse(IMAGE).asCompatibleSubstituteFor("postgres"));
        configure(container, TMPFS_SIZE, MEMORY_LIMIT_BYTES, List.of(), caller);
        return container;
    }

    /**
     * Apply the recipe to {@code container}, which the caller constructed on its image: credentials, sslmode,
     * start attempts, tmpfs PGDATA of {@code tmpfsSize}, the {@link #baseCommand} followed by
     * {@code extraServerArgs}, the memory limit {@code memoryBytes} (swap equal), and the labels that let a host
     * count a run's containers ({@code docker ps --filter label=dev.nexus.test.run=<id>}) and rank call sites
     * ({@code dev.nexus.test.caller}, {@code caller}).
     *
     * <p>{@code sslmode=disable}: the flake signature was the JDBC startup probe failing "setting up the SSL
     * connection" for the whole 120s window against a restarting/half-up server under Docker pressure (pg JDBC
     * defaults to sslmode=prefer; a local throwaway container needs no TLS). {@code withStartupAttempts(3)}: a
     * genuinely failed startup recreates the whole container (fresh initdb) instead of flaking the class.
     *
     * @throws IllegalArgumentException when the memory limit does not exceed the tmpfs size (tmpfs pages are
     *         charged to the container) or the size is not whole gibibytes
     */
    public static void configure(PostgreSQLContainer<?> container, String tmpfsSize, long memoryBytes,
                                 List<String> extraServerArgs, String caller) {
        if (memoryBytes <= gibibytes(tmpfsSize)) {
            throw new IllegalArgumentException("memory limit " + memoryBytes + " must exceed tmpfs " + tmpfsSize);
        }
        List<String> command = new ArrayList<>(baseCommand());
        command.addAll(extraServerArgs);
        container
            .withDatabaseName(DATABASE)
            .withUsername(USERNAME)
            .withPassword(PASSWORD)
            .withUrlParam("sslmode", "disable")
            .withStartupAttempts(3)
            // nexus-xynce: PGDATA on a capped tmpfs, nothing a throwaway cluster needs to survive a crash written
            // durably, pg_wal held near 64 MB (max_wal_size is a soft target; the tmpfs size is the hard bound).
            .withTmpFs(Map.of(PGDATA, "rw,size=" + tmpfsSize))
            .withCommand(command.toArray(new String[0]))
            .withCreateContainerCmdModifier(cmd -> cmd.getHostConfig()
                .withMemory(memoryBytes)
                .withMemorySwap(memoryBytes))
            // Lets a host count one run's containers among everyone else's.
            .withLabel("dev.nexus.test.run", System.getenv().getOrDefault("NX_TEST_RUN_ID", ""))
            // nexus-7xfh7: which caller asked for this boot, so `docker events` can rank call sites:
            // docker events --filter label=dev.nexus.test.run=<id> --format '{{index .Actor.Attributes "dev.nexus.test.caller"}}'
            .withLabel("dev.nexus.test.caller", caller);
    }
}
