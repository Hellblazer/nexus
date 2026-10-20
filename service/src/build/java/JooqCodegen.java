// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.Scope;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.CompositeResourceAccessor;
import liquibase.resource.DirectoryResourceAccessor;
import liquibase.resource.ResourceAccessor;
import liquibase.ui.LoggerUIService;
import org.jooq.codegen.GenerationTool;
import org.jooq.meta.jaxb.Configuration;
import org.jooq.meta.jaxb.Jdbc;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.logging.Level;

/**
 * The jOOQ code generator of service/pom.xml (nexus-aca0q.3), run as a Java source-file program by
 * exec-maven-plugin in the generate-sources and generate-test-sources phases:
 *
 * <pre>
 *   java -classpath &lt;test classpath&gt; JooqCodegen.java &lt;generator.xml&gt; &lt;target dir&gt; &lt;master changelog&gt; &lt;changelog root&gt;[,&lt;root&gt;...]
 * </pre>
 *
 * <p>It starts a throwaway pgvector/pgvector:pg17 container, applies the master changelog (resolved against the
 * changelog roots, in order, like the testcontainers-jooq-codegen-maven-plugin 0.0.4 it replaces), and runs jOOQ's
 * GenerationTool with the generator section of {@code generator.xml} against it, writing into the target
 * directory. Any failure prints its cause and exits non-zero, so Maven fails the build.
 *
 * <p>Why not the plugin: it keeps PGDATA on the image's anonymous docker volume (about 52 MB written to the
 * host disk on every codegen cache miss) and has no setting for tmpfs, memory or server flags. The container
 * recipe below is {@code PgContainerHelper.newContainer(DockerImageName, String, long)} in
 * src/test/java/dev/nexus/service/PgContainerHelper.java: the same tmpfs PGDATA and server flags, the same
 * memory limit, sslmode=disable and three start attempts. It is copied, not called: this runs before the test
 * classes compile. Keep the two in step. Left out on purpose: the DDL counter's logging collector and event
 * trigger (a codegen run is not a counted suite), and FailFastPostgreSQLContainer (a foreign server squatting
 * the published port; it only shortens a failing start).
 *
 * <p>Container cleanup: try-with-resources stops the container on every exit path this process controls, and
 * Testcontainers' Ryuk reaps it when the JVM is killed (SIGKILL, a Maven build cancelled hard).
 */
public final class JooqCodegen {

    // Copied from PgContainerHelper (see the class comment).
    private static final String IMAGE = "pgvector/pgvector:pg17";
    private static final String DATABASE = "postgres";
    private static final String USERNAME = "postgres";
    private static final String PASSWORD = "postgres";
    private static final String PGDATA = "/var/lib/postgresql/data";
    private static final String TMPFS_SIZE = "2g";
    private static final long MEMORY_LIMIT_BYTES = 2560L * 1024 * 1024;

    /** Held so the JUL logger (weakly referenced by the LogManager) keeps the level set on it. */
    private static final java.util.logging.Logger LIQUIBASE_LOG = java.util.logging.Logger.getLogger("liquibase");

    private JooqCodegen() {}

    public static void main(String[] args) {
        int rc;
        try {
            rc = run(args);
        } catch (Throwable t) {
            System.err.println("JooqCodegen: FAILED: " + t);
            t.printStackTrace();
            rc = 1;
        }
        // Explicit exit: a leftover non-daemon thread (docker-java, a pool) must not keep a failed or finished build waiting.
        System.exit(rc);
    }

    private static int run(String[] args) throws Exception {
        if (args.length != 4) {
            System.err.println("usage: JooqCodegen.java <generator.xml> <target dir> <master changelog> <changelog root>[,<root>...]");
            return 2;
        }
        Path generator = Path.of(args[0]).toAbsolutePath();
        Path target = Path.of(args[1]).toAbsolutePath();
        String master = args[2];
        List<Path> roots = new ArrayList<>();
        for (String r : args[3].split(",")) {
            if (!r.isBlank()) {
                roots.add(Path.of(r.strip()).toAbsolutePath());
            }
        }
        if (!Files.isRegularFile(generator)) {
            throw new IllegalArgumentException("generator configuration not found: " + generator);
        }
        if (roots.isEmpty()) {
            throw new IllegalArgumentException("no changelog root given: " + args[3]);
        }
        for (Path root : roots) {
            if (!Files.isDirectory(root)) {
                throw new IllegalArgumentException("changelog root is not a directory: " + root);
            }
        }
        if (roots.stream().noneMatch(root -> Files.isRegularFile(root.resolve(master)))) {
            throw new IllegalArgumentException("master changelog " + master + " is under none of " + roots);
        }

        quietLogging();
        long start = System.nanoTime();
        try (PostgreSQLContainer<?> pg = newContainer()) {
            pg.start();
            long started = System.nanoTime();
            migrate(pg, master, roots);
            long migrated = System.nanoTime();
            generate(pg, generator, target);
            long done = System.nanoTime();
            System.out.printf("JooqCodegen: %s -> %s (container %d ms, changelog %d ms, jOOQ %d ms)%n",
                generator.getFileName(), target, (started - start) / 1_000_000, (migrated - started) / 1_000_000,
                (done - migrated) / 1_000_000);
        }
        return 0;
    }

    /** PgContainerHelper.newContainer(image, tmpfsSize, memoryBytes) without the DDL counter. */
    static PostgreSQLContainer<?> newContainer() {
        PostgreSQLContainer<?> container = new PostgreSQLContainer<>(DockerImageName.parse(IMAGE).asCompatibleSubstituteFor("postgres"));
        container
            .withDatabaseName(DATABASE)
            .withUsername(USERNAME)
            .withPassword(PASSWORD)
            .withUrlParam("sslmode", "disable")
            .withStartupAttempts(3)
            // PGDATA on a capped tmpfs, nothing a throwaway cluster needs to survive a crash written durably, pg_wal held
            // near 64 MB (max_wal_size is a soft target; the tmpfs size is the hard bound).
            .withTmpFs(Map.of(PGDATA, "rw,size=" + TMPFS_SIZE))
            .withCommand("postgres",
                "-c", "fsync=off",
                "-c", "synchronous_commit=off",
                "-c", "full_page_writes=off",
                "-c", "max_wal_size=64MB",
                "-c", "min_wal_size=32MB",
                "-c", "wal_keep_size=0",
                "-c", "log_checkpoints=off",
                "-c", "checkpoint_warning=0")
            .withCreateContainerCmdModifier(cmd -> cmd.getHostConfig()
                .withMemory(MEMORY_LIMIT_BYTES)
                .withMemorySwap(MEMORY_LIMIT_BYTES))
            // The labels the test containers carry, so a host can count a run's containers: docker ps --filter label=dev.nexus.test.run=<id>.
            .withLabel("dev.nexus.test.run", System.getenv().getOrDefault("NX_TEST_RUN_ID", ""))
            .withLabel("dev.nexus.test.caller", "JooqCodegen");
        return container;
    }

    private static void migrate(PostgreSQLContainer<?> pg, String master, List<Path> roots) throws Exception {
        List<ResourceAccessor> accessors = new ArrayList<>();
        for (Path root : roots) {
            accessors.add(new DirectoryResourceAccessor(root));
        }
        try (Connection connection = DriverManager.getConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
             CompositeResourceAccessor resources = new CompositeResourceAccessor(accessors)) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(connection));
            // The default UI prints a "Running Changeset" line per changeset to stdout (about 500 lines); the logger UI
            // sends them to JUL, where quietLogging() has set WARNING.
            Scope.child(Map.of(Scope.Attr.ui.name(), new LoggerUIService()), () -> {
                try (Liquibase liquibase = new Liquibase(master, resources, database)) {
                    liquibase.update(new Contexts());
                }
            });
        }
    }

    private static void generate(PostgreSQLContainer<?> pg, Path generator, Path target) throws Exception {
        Configuration configuration;
        try (InputStream in = Files.newInputStream(generator)) {
            configuration = GenerationTool.load(in);
        }
        if (configuration.getGenerator() == null || configuration.getGenerator().getTarget() == null) {
            throw new IllegalArgumentException(generator + " has no generator/target section");
        }
        configuration.setJdbc(new Jdbc()
            .withDriver("org.postgresql.Driver")
            .withUrl(pg.getJdbcUrl())
            .withUser(pg.getUsername())
            .withPassword(pg.getPassword()));
        configuration.getGenerator().getTarget().setDirectory(target.toString());
        GenerationTool.generate(configuration);
    }

    /** Liquibase logs one INFO line per step of a walk through JUL; logback with no logback.xml on the classpath logs DEBUG. */
    private static void quietLogging() {
        System.setProperty("liquibase.command.showSummary", "OFF");
        LIQUIBASE_LOG.setLevel(Level.WARNING);
        if (LoggerFactory.getILoggerFactory() instanceof ch.qos.logback.classic.LoggerContext context) {
            context.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME).setLevel(ch.qos.logback.classic.Level.WARN);
        }
    }
}
