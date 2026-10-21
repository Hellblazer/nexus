// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.build.PgContainerRecipe;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.test.Routines;
import org.jooq.Configuration;
import org.jooq.DSLContext;
import org.jooq.ExecuteContext;
import org.jooq.ExecuteListener;
import org.jooq.Field;
import org.jooq.Name;
import org.jooq.Query;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.conf.StatementType;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.utility.DockerImageName;
import org.testcontainers.utility.MountableFile;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;


/**
 * Shared factory for per-class Testcontainers PostgreSQL containers.
 *
 * <p>RDR-155 P1.0 (nexus-22man): replaces io.zonky EmbeddedPostgres throughout the
 * service test suite.  Each test class creates its own container via {@link #start()}
 * (PER_CLASS lifecycle mirrors the previous EmbeddedPostgres.builder().start() pattern).
 *
 * <p>The image is {@code pgvector/pgvector:pg17} declared compatible with {@code postgres},
 * which allows PostgreSQLContainer to perform its normal wait-strategy and connection
 * checks.  The container runs stock PostgreSQL 17 with the pgvector extension available
 * but not yet loaded — {@code CREATE EXTENSION vector} lands in a later bead (nexus-mf447).
 */
public final class PgContainerHelper {

    /** Image used for all service-module test containers. */
    public static final String IMAGE = PgContainerRecipe.IMAGE;

    /** Superuser database name (matches io.zonky default). */
    public static final String DATABASE = PgContainerRecipe.DATABASE;

    /** Superuser username (matches io.zonky default). */
    public static final String USERNAME = PgContainerRecipe.USERNAME;

    /** Superuser password. */
    public static final String PASSWORD = PgContainerRecipe.PASSWORD;

    /**
     * The image's data directory. nexus-xynce: it is mounted as tmpfs, because in
     * Docker's overlay it sat on the host disk and the engine suites wrote about
     * 50 MB/s to chas's NVMe (measured 2026-10-19).
     */
    public static final String PGDATA = PgContainerRecipe.PGDATA;

    /**
     * nexus-sz651: the server log's directory, inside the tmpfs {@link #PGDATA}. The DDL counter needs
     * every DDL statement logged ({@code log_statement=ddl} plus the event trigger's {@code RAISE LOG},
     * about 462k lines in a main run). Left on stderr, Docker stores them as json-file logs under
     * /var/lib/docker on the host's disk: 1.63 GB written against 0.48 GB without the counter (chas
     * NVMe sampler). {@code logging_collector} takes them off stderr into this directory, which is RAM.
     * Only with the counter on: with {@code -Dnexus.ddl.count=false} (nexus-3cwsw) a container has no
     * collector and no such file, and {@link #serverLog} reads Docker's log instead.
     */
    public static final String SERVER_LOG_DIR = PGDATA + "/nx-log";

    /** The one server log file (fixed name, no rotation), so the temp server of the image's init and the real one append to it. */
    public static final String SERVER_LOG_FILE = SERVER_LOG_DIR + "/postgresql.log";

    /**
     * tmpfs cap for {@link #PGDATA}. Measured live containers on chas held 19 to 171 MB
     * (most of it WAL before the max_wal_size=64MB setting below, a soft target). 1 GB was too tight: on CI the shared per-fork cluster hit ENOSPC in ChashVectorConcurrencyTest (72k concurrent vector re-upserts on top of the fork's other databases, run 39193706997), so the cap is 2 GB; a cluster that outgrows this fails
     * with "No space left on device" instead of eating host memory.
     */
    public static final String TMPFS_SIZE = PgContainerRecipe.TMPFS_SIZE;

    /**
     * Container memory limit (swap included). tmpfs pages are charged to the
     * container, so the limit is the tmpfs cap plus room for shared_buffers and
     * backends. Containers run under the Docker daemon, outside any lab-run grant:
     * this limit is what bounds each one, and the pool reserves limit times the most
     * containers a run holds.
     */
    public static final long MEMORY_LIMIT_BYTES = PgContainerRecipe.MEMORY_LIMIT_BYTES;

    static long tmpfsSizeBytes() {
        return gibibytes(TMPFS_SIZE);
    }

    /** {@code "<n>g"} in bytes; the only form these sizes are written in. */
    static long gibibytes(String size) {
        return PgContainerRecipe.gibibytes(size);
    }

    /**
     * Production service role (NOSUPERUSER NOBYPASSRLS) — the credential the app layer
     * should run under in tests so it is subject to the same RLS as production, rather
     * than the BYPASSRLS superuser (nexus-5j7pb). The role is created by each test's
     * startAll() and granted DML by the grants-nexus-svc.xml changeset.
     */
    public static final String SVC_USERNAME = "nexus_svc";
    /** Password for {@link #SVC_USERNAME}. */
    public static final String SVC_PASSWORD = "nexus_svc_pass";

    /**
     * The non-superuser schema owner of the owner-migrated template ({@link #startMigratedByOwner()}), the
     * production {@code nexus_admin} shape: NOSUPERUSER, NOBYPASSRLS, owner of every relation the walk made.
     * A cluster-wide role, created once per fork, shared by every class that takes that template.
     */
    public static final String TEMPLATE_OWNER = "nexus_admin_tmpl";
    public static final String TEMPLATE_OWNER_PASSWORD = "nexus_admin_tmpl_pass";

    private PgContainerHelper() {}

    /**
     * Create a CONFIGURED, UNSTARTED container (nexus-1hj1d).
     *
     * <p>The single hardened boot recipe — every test boot path (this
     * helper's {@link #start()} AND the network-attached raw boot in
     * {@code PgBouncerTenantIsolationTest}) must construct through here or
     * the SSL-handshake startup flake survives in the bypassing path:
     *
     * <ul>
     *   <li>{@code sslmode=disable}: the flake signature was the JDBC
     *       startup probe failing "setting up the SSL connection" for the
     *       whole 120s window against a restarting/half-up server under
     *       Docker pressure (pg JDBC defaults to sslmode=prefer; a local
     *       throwaway container needs no TLS). The wait already covers the
     *       classic initdb-restart window, so that fix is a no-op here: with
     *       the counter off the default wait reads the ready line twice, and
     *       with it on the {@link #READY_PROBE} passes only for the real
     *       server (nexus-sz651). The URL param is the load-bearing change,
     *       propagating via getJdbcUrl() to every pool and createConnection
     *       call.</li>
     *   <li>{@code withStartupAttempts(3)}: native belt — a genuinely
     *       failed startup recreates the whole container (fresh initdb)
     *       instead of flaking the class.</li>
     * </ul>
     *
     * <p>Returns a {@link FailFastPostgreSQLContainer} (nexus-soqa8, hardening (c) of
     * nexus-lgdy1): a foreign server squatting the published port — e.g. a leaked host
     * Postgres from the atexit-only-teardown class of bug — rejects the container's
     * credentials with a deterministic auth SQLSTATE, and this fails in one attempt
     * instead of burning the full ~120s connect-retry budget on a connection that will
     * never succeed. See {@link FailFastPostgreSQLContainer}'s class doc for the full
     * mechanism.
     */
    public static PostgreSQLContainer<?> newContainer() {
        return newContainer(DockerImageName.parse(IMAGE));
    }

    /**
     * {@link #newContainer()} on another image derived from {@link #IMAGE} (one with an
     * extra extension installed, say). Every test Postgres container is built here, so
     * every one gets the tmpfs data directory and the memory limit
     * ({@code PgContainerTmpfsGateTest} forbids constructing one anywhere else).
     */
    public static PostgreSQLContainer<?> newContainer(DockerImageName image) {
        return newContainer(image, TMPFS_SIZE, MEMORY_LIMIT_BYTES);
    }

    /**
     * {@link #newContainer(DockerImageName)} with a larger tmpfs and memory limit, for a
     * test that loads more than the default 2 GB holds (the opt-in recall run). The
     * limit must exceed the tmpfs size, because tmpfs pages are charged to the container.
     */
    public static PostgreSQLContainer<?> newContainer(DockerImageName image, String tmpfsSize, long memoryBytes) {
        boolean counted = DdlCounts.enabled();
        // The recipe's own base command comes first (PgContainerRecipe.configure); these follow it.
        List<String> extraServerArgs = new ArrayList<>();
        if (counted) {
            extraServerArgs.addAll(List.of(
                // nexus-sz651: count DDL per run (DdlLogParser). log_statement=ddl logs the
                // top-level statements; the event trigger the init script installs logs every
                // DDL command, including the ones a function runs through EXECUTE.
                "-c", "log_statement=ddl",
                "-c", "log_line_prefix=%m [%p] db=%d ",
                "-c", "log_timezone=UTC",
                // ... and keep all of it off Docker's log driver (SERVER_LOG_DIR). One fixed file, never
                // rotated or truncated, so an offset into it stays valid. The collector sends the
                // "ready to accept connections" line to the file too, so the wait below is a probe.
                "-c", "logging_collector=on",
                "-c", "log_destination=stderr",
                "-c", "log_directory=" + SERVER_LOG_DIR,
                "-c", "log_filename=postgresql.log",
                "-c", "log_rotation_age=0",
                "-c", "log_rotation_size=0",
                "-c", "log_truncate_on_rotation=off"));
        }
        FailFastPostgreSQLContainer container = new FailFastPostgreSQLContainer(image.asCompatibleSubstituteFor("postgres"))
            .ddlCounted(counted)
            .ddlOwner(DdlCounts.callerClass());
        // Image aside, everything about the boot is the recipe's, shared with the jOOQ codegen runner (nexus-llba1):
        // credentials, sslmode, start attempts, tmpfs PGDATA, memory limit, the base server flags, the run labels.
        // (nexus-7xfh7: the caller label ranks call sites in `docker events`.)
        PgContainerRecipe.configure(container, tmpfsSize, memoryBytes, extraServerArgs, callerClass());
        if (counted) {
            // The image's init runs a temp server on a unix socket only; pg_isready on TCP passes only for
            // the real server, once init is done. (The default wait reads Docker's log for the ready line,
            // which the collector moved into the file.) Counter off: the default wait stays.
            container
                .waitingFor(Wait.forSuccessfulCommand(READY_PROBE).withStartupTimeout(STARTUP_TIMEOUT))
                .withCopyFileToContainer(
                    MountableFile.forClasspathResource("ddl/nx-ddl-probe.sql"),
                    "/docker-entrypoint-initdb.d/00-nx-ddl-probe.sql");
        }
        return container;
    }

    /**
     * The wait the counter-on container starts with (nexus-sz651). {@code -h 127.0.0.1} is load-bearing: without
     * it {@code pg_isready} uses the unix socket and passes on the image's init-time temp server, before the
     * real server is up. That temp server listens on the socket only, so TCP passes only for the real one.
     */
    static final String READY_PROBE = "pg_isready -h 127.0.0.1 -p 5432 -q";

    /**
     * One start attempt's wait. 60 s is the library's own value for {@code PostgreSQLContainer}'s ready-line
     * wait, which this probe replaces; nexus-sz651 first wrote 120 s with no reason (nexus-t4y7b found none
     * in history). With {@code withStartupAttempts(3)} a server that never comes up costs three of these.
     */
    static final Duration STARTUP_TIMEOUT = Duration.ofSeconds(60);

    /** Reads of the server log wait this long for one {@code docker exec} before abandoning it and trying again. */
    static final Duration SERVER_LOG_READ_TIMEOUT = Duration.ofSeconds(60);

    /** How many tries {@link #serverLog} gives one read. */
    static final int SERVER_LOG_READ_ATTEMPTS = 3;

    private static final org.slf4j.Logger LOG = org.slf4j.LoggerFactory.getLogger(PgContainerHelper.class);

    /** Test seam, per thread so a parallel test is unaffected: a shorter per-try wait for {@link #serverLog}. */
    static final ThreadLocal<Duration> SERVER_LOG_READ_TIMEOUT_OVERRIDE = new ThreadLocal<>();

    private static final java.util.concurrent.ExecutorService LOG_READERS = java.util.concurrent.Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "nx-server-log-read");
        t.setDaemon(true);
        return t;
    });

    /**
     * The container's whole server log. Counter on: read from {@link #SERVER_LOG_FILE} in the container
     * (nexus-sz651), not {@code container.getLogs()}, which is Docker's log and carries only the
     * postmaster's first lines then. Counter off (the container was built with it off, nexus-3cwsw): there
     * is no file and the log is Docker's, {@code getLogs()}. The file only grows, so a length taken from one read is a valid offset for the next.
     * Throws when the file cannot be read. One {@code docker exec} that never returns (seen once, in a
     * 10-class run on colima; the cause is not established) is abandoned after
     * {@link #SERVER_LOG_READ_TIMEOUT} and the read is tried again, up to {@link #SERVER_LOG_READ_ATTEMPTS}
     * tries. That bounds the CALLER. What happens to the stuck exec depends on where it hangs, and which
     * one happened on colima is not established: if the exec is parked in docker-java's
     * {@code awaitCompletion} waiting for its stream, {@code Future.cancel(true)} wakes it and its
     * {@code finally} closes the stream; if the hang is inside the synchronous execCreate/inspect HTTP call,
     * the reader thread stays blocked until the JVM exits. Every abandon is logged at WARN
     * ({@link #retryingRead}) so the recurrence rate can be read off a run.
     */
    public static String serverLog(PostgreSQLContainer<?> c) {
        return serverLog(c, PgContainerHelper::retryingRead);
    }

    /** The retrying reader {@link #serverLog} hands its read to; a test passes one that records the arguments. */
    @FunctionalInterface
    interface LogReader {
        String read(java.util.concurrent.Callable<String> read, Duration timeout, int attempts, String owner);
    }

    /** {@link #serverLog} with its reader injected, so a test can see the timeout and attempts it is given (nexus-ccg9y). */
    static String serverLog(PostgreSQLContainer<?> c, LogReader reader) {
        if (c instanceof FailFastPostgreSQLContainer f && !f.ddlCounted()) {
            return c.getLogs(); // counter off: no collector, the server log is Docker's log again
        }
        Duration timeout = SERVER_LOG_READ_TIMEOUT_OVERRIDE.get() != null
            ? SERVER_LOG_READ_TIMEOUT_OVERRIDE.get() : SERVER_LOG_READ_TIMEOUT;
        String owner = c instanceof FailFastPostgreSQLContainer f ? f.ddlOwner() : "unknown";
        return reader.read(() -> {
            org.testcontainers.containers.Container.ExecResult r = c.execInContainer("cat", SERVER_LOG_FILE);
            if (r.getExitCode() != 0) {
                throw new IllegalStateException("nexus-sz651: reading " + SERVER_LOG_FILE + " exited "
                    + r.getExitCode() + ": " + r.getStderr());
            }
            return r.getStdout();
        }, timeout, SERVER_LOG_READ_ATTEMPTS, owner);
    }

    /** Lines of the server log's end a failed start prints. */
    static final int START_FAILURE_TAIL_LINES = 50;

    /**
     * The last {@code lines} lines of the server log file, for a start that failed its wait. Testcontainers'
     * own dump of a failed start is Docker's log, which with the collector on holds only the postmaster's
     * first lines; a FATAL or PANIC after the collector started is only in the file. Best effort, one try
     * of 15 s: a container that already exited took its tmpfs, and the file, with it. Never throws.
     */
    static String serverLogTail(PostgreSQLContainer<?> c, int lines) {
        String owner = c instanceof FailFastPostgreSQLContainer f ? f.ddlOwner() : "unknown";
        try {
            return retryingRead(() -> {
                org.testcontainers.containers.Container.ExecResult r =
                    c.execInContainer("tail", "-n", Integer.toString(lines), SERVER_LOG_FILE);
                if (r.getExitCode() != 0) {
                    throw new IllegalStateException("tail exited " + r.getExitCode() + ": " + r.getStderr());
                }
                return r.getStdout();
            }, Duration.ofSeconds(15), 1, owner);
        } catch (RuntimeException e) {
            return "(server log unreadable: " + e + ")";
        }
    }

    /**
     * Run {@code read} on a daemon thread, giving each of {@code attempts} tries {@code timeout}. A try that
     * times out is abandoned and logged at WARN with the attempt number and the time waited, and two classes:
     * {@code owner}, the class that BOOTED the container ({@link FailFastPostgreSQLContainer#ddlOwner()},
     * "unknown" for a handle), and {@code reader}, the first class outside the container plumbing on the
     * calling thread's stack, i.e. the one that asked for this read. An abandoned try's thread is interrupted
     * with {@code cancel(true)}: that wakes an exec parked in {@code awaitCompletion} (its {@code finally}
     * closes the stream), but not one blocked in the synchronous create/inspect HTTP call, which stays
     * blocked until the JVM exits. Which of the two a real hang is has not been established.
     */
    static String retryingRead(java.util.concurrent.Callable<String> read, Duration timeout, int attempts, String owner) {
        for (int attempt = 1; ; attempt++) {
            long started = System.nanoTime();
            java.util.concurrent.Future<String> f = LOG_READERS.submit(read);
            try {
                return f.get(timeout.toMillis(), java.util.concurrent.TimeUnit.MILLISECONDS);
            } catch (java.util.concurrent.TimeoutException e) {
                f.cancel(true);
                LOG.warn("event=server_log_read_abandoned owner={} reader={} attempt={} of={} elapsed_ms={} file={}",
                    owner, callerClass(), attempt, attempts, java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started),
                    SERVER_LOG_FILE);
                if (attempt >= attempts) {
                    throw new IllegalStateException("nexus-sz651: reading " + SERVER_LOG_FILE + " timed out "
                        + attempts + " times, " + timeout + " each", e);
                }
            } catch (InterruptedException e) {
                f.cancel(true);
                Thread.currentThread().interrupt();
                throw new IllegalStateException("nexus-sz651: interrupted reading " + SERVER_LOG_FILE, e);
            } catch (java.util.concurrent.ExecutionException e) {
                Throwable cause = e.getCause();
                if (cause instanceof RuntimeException re) {
                    throw re;
                }
                throw new IllegalStateException("nexus-sz651: reading " + SERVER_LOG_FILE, cause);
            }
        }
    }

    /**
     * One try of {@code call} on the same daemon-thread pool as {@link #retryingRead}, abandoned after {@code timeout}:
     * for a docker-java call on a failure path, where a wedged daemon must not hang the report. A timeout throws
     * {@code IllegalStateException("timed out after ...")} and interrupts the try, which wakes a stream wait but not a
     * call blocked in the synchronous HTTP exchange; that thread stays blocked until the JVM exits (nexus-ccg9y).
     */
    static <T> T boundedOnce(java.util.concurrent.Callable<T> call, Duration timeout, String what) {
        java.util.concurrent.Future<T> f = LOG_READERS.submit(call);
        try {
            return f.get(timeout.toMillis(), java.util.concurrent.TimeUnit.MILLISECONDS);
        } catch (java.util.concurrent.TimeoutException e) {
            f.cancel(true);
            throw new IllegalStateException("timed out after " + timeout + ": " + what, e);
        } catch (InterruptedException e) {
            f.cancel(true);
            Thread.currentThread().interrupt();
            throw new IllegalStateException("interrupted: " + what, e);
        } catch (java.util.concurrent.ExecutionException e) {
            if (e.getCause() instanceof RuntimeException re) {
                throw re;
            }
            throw new IllegalStateException(what, e.getCause());
        }
    }

    /** First stack frame outside the container plumbing: the test class that caused a boot. */
    static String callerClass() {
        Set<String> plumbing = Set.of(
            PgContainerHelper.class.getName(), SharedCluster.class.getName(),
            SharedDatabaseHandle.class.getName(), FailFastPostgreSQLContainer.class.getName());
        return StackWalker.getInstance().walk(frames -> frames
            .map(StackWalker.StackFrame::getClassName)
            .filter(n -> !plumbing.contains(n))
            .findFirst().orElse("unknown"))
            .replaceFirst("^dev\\.nexus\\.service\\.", "");
    }

    /**
     * System property opt-out for the nexus-yhmav shared-cluster reuse below
     * ({@code -Dnexus.test.pg.shared=false}). Debugging escape hatch only -- the
     * default (unset / any value other than the literal {@code "false"}) is shared
     * reuse ON, since it is a strict subset of what a fresh container already
     * guarantees (see {@link SharedCluster}'s javadoc for the safety argument).
     */
    private static final String SHARED_PROPERTY = "nexus.test.pg.shared";

    /**
     * Create and start a container.
     *
     * <p><b>nexus-yhmav (per-fork container reuse):</b> by default this returns a
     * {@link SharedDatabaseHandle} onto a fresh, already-migrated database cloned
     * from a per-fork shared cluster (one real container boot per surefire fork,
     * not one per class) -- see {@link SharedCluster} for the full design and the
     * safety argument. Set {@code -Dnexus.test.pg.shared=false} to fall back to the
     * pre-yhmav behavior (a genuinely fresh container every call), e.g. while
     * debugging a suspected cross-class interference report.
     *
     * <p>Replaces {@code EmbeddedPostgres.builder().start()}.
     */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> start() {
        if ("false".equals(System.getProperty(SHARED_PROPERTY))) {
            return startDedicated();
        }
        try {
            return SharedCluster.acquireDatabase();
        } catch (SQLException e) {
            throw new IllegalStateException("nexus-yhmav shared-cluster acquire failed", e);
        }
    }

    /**
     * Always boots a genuinely fresh, independently-managed container -- the
     * explicit opt-out from {@link #start()}'s shared-cluster reuse and from
     * {@link #startEmptyDatabase}'s empty database on it.
     *
     * <p>Since nexus-7xfh7 a migration-process class (one that walks the changelog from nothing, or
     * part of the way, and asserts on the result) takes {@link #startEmptyDatabase}: the database is
     * the isolation boundary, so it needs no container of its own. A container is still needed when the
     * test depends on CLUSTER-level state or on how the container is built. The roster, each entry
     * confirmed against the class:
     * <ul>
     *   <li>{@code GrantsPgMonitorTest} -- falsifies {@code grants-004} by running it WITHOUT the
     *       migrating role's {@code pg_monitor ... WITH ADMIN OPTION}; on a shared cluster
     *       {@code nexus_svc} already holds {@code pg_monitor} from the template migration, so the
     *       falsification would be vacuous. Role membership is cluster-wide.</li>
     *   <li>{@code TextGateProbeSingleRoleGuardIntegrationTest} -- asserts role membership
     *       edges on {@code nexus_svc} and the owner ({@code grantRoleMembership}), which would leak
     *       into every later class on a shared cluster.</li>
     *   <li>{@code Rdr192LiveCExplainEvidenceIntegrationTest}, {@code ReadPathLeafPruningIntegrationTest} --
     *       read the plans {@code auto_explain} writes to the CONTAINER's log ({@link #serverLog}); a shared
     *       cluster's log holds every class's output, and {@code ALTER ROLE nexus_svc SET auto_explain...}
     *       is cluster-wide.</li>
     *   <li>{@code TenantPartitionCreationTs1MeasurementIntegrationTest} -- a latency measurement that
     *       creates and drops ~2,500 leaves per run; it wants an idle server and a lock table of its own.</li>
     *   <li>{@code ChashProbePlanShapeTest}, {@code ChashVectorConcurrencyTest},
     *       {@code ChunkLiveOwnersRecallExtendedIntegrationTest} -- a sized container
     *       ({@link #startDedicated(String, long)}): they outgrow, or come within a few percent of, the
     *       default {@link #TMPFS_SIZE} tmpfs, and a full tmpfs on the shared cluster also blocks the teardown
     *       connection, so every later class in the fork errors at acquire.</li>
     *   <li>{@code PlpgsqlCheckGateTest} (a different image) and {@code PgBouncerTenantIsolationTest}
     *       (network-attached) build their own container through {@link #newContainer}.</li>
     * </ul>
     * Classes that create a cluster-global role under a name the migration fixes ({@code nexus_diag}, say)
     * no longer need a container: they declare it exclusive in {@link #startEmptyDatabase}.
     */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> startDedicated() {
        PostgreSQLContainer<?> c = newContainer();
        c.start();
        return c;
    }

    /**
     * An EMPTY, unmigrated database on the per-fork shared cluster (nexus-7xfh7), in place of
     * {@link #startDedicated()}'s fresh container boot. For a class that is itself the migration
     * process (it walks the changelog from nothing, or part of the way, then asserts) yet needs no
     * cluster-level state of its own. {@code stop()} drops the database.
     *
     * <p>Stays {@link #startDedicated()} for: {@code ALTER SYSTEM} or any server setting, a different
     * image, a network attachment, a sized container, per-method assertions about the cluster itself
     * (a role's absence, membership or attributes), and the provisioning whose point is a virgin
     * cluster's ownership. The cluster already holds {@code nexus_svc} and the other roles the template
     * migration made; roles a class creates need names no other class uses, or are declared in
     * {@code exclusiveRoles} (absent at acquire, dropped at stop; see
     * {@link SharedCluster#acquireEmptyDatabase}). With {@code -Dnexus.test.pg.shared=false} this boots a
     * dedicated container, as {@link #start()} does.
     */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> startEmptyDatabase(String... exclusiveRoles) {
        if ("false".equals(System.getProperty(SHARED_PROPERTY))) {
            return startDedicated();
        }
        try {
            return SharedCluster.acquireEmptyDatabase(exclusiveRoles);
        } catch (SQLException e) {
            throw new IllegalStateException("nexus-7xfh7 shared-cluster empty-database acquire failed: " + e.getMessage(), e);
        }
    }

    /**
     * A database already migrated to HEAD BY {@link #TEMPLATE_OWNER}, cloned from the per-fork owner-migrated
     * template (nexus-0ty8z), in place of {@link #startEmptyDatabase}'s bootstrap-then-{@code SchemaMigrator.migrate}.
     * For a class whose subject is not the walk but needs production's ownership shape: a SECURITY DEFINER function
     * that must run as a role subject to row-level security, a leaf only its owner may alter. A class that asserts
     * the provisioning or the walk itself (ownership from scratch, a changeset count, a part-way state) stays on
     * {@link #startEmptyDatabase}; {@code DbStartClassificationGateTest} records which is which.
     *
     * <p>The clone has the owner's relations and grants but not the owner's database-level privilege
     * ({@code CREATE DATABASE ... TEMPLATE} does not copy a database ACL), so the owner provisioning is re-run
     * here, once, for every caller: it is idempotent and costs a handful of statements, and a second
     * {@code SchemaMigrator.migrate} by the owner finds the database as a restarted engine would. Connect as
     * {@link #TEMPLATE_OWNER}/{@link #TEMPLATE_OWNER_PASSWORD}.
     *
     * <p>With {@code -Dnexus.test.pg.shared=false} this boots a dedicated container and does the bootstrap and
     * the walk itself, so the opt-out still gives the class the same database.
     */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> startMigratedByOwner() {
        if ("false".equals(System.getProperty(SHARED_PROPERTY))) {
            PostgreSQLContainer<?> c = startDedicated();
            try (Connection su = c.createConnection("")) {
                bootstrapNonSuperuserOwner(su, TEMPLATE_OWNER, TEMPLATE_OWNER_PASSWORD);
                var cfg = new HikariConfig();
                cfg.setJdbcUrl(c.getJdbcUrl());
                cfg.setUsername(TEMPLATE_OWNER);
                cfg.setPassword(TEMPLATE_OWNER_PASSWORD);
                cfg.setMaximumPoolSize(2);
                try (HikariDataSource owner = new HikariDataSource(cfg)) {
                    dev.nexus.service.db.SchemaMigrator.migrate(owner);
                    try (Connection oc = owner.getConnection()) {
                        installTestObjectsAfterProductSchema(oc);
                    }
                }
                return c;
            } catch (Exception e) {
                c.stop();
                throw new IllegalStateException("nexus-0ty8z dedicated owner-migrated database failed: " + e.getMessage(), e);
            }
        }
        try {
            PostgreSQLContainer<?> c = SharedCluster.acquireOwnedDatabase();
            try (Connection su = c.createConnection("")) {
                bootstrapNonSuperuserOwner(su, TEMPLATE_OWNER, TEMPLATE_OWNER_PASSWORD);
            } catch (Exception e) {
                c.stop();
                throw new IllegalStateException("nexus-0ty8z owner provisioning on the clone failed: " + e.getMessage(), e);
            }
            return c;
        } catch (SQLException e) {
            throw new IllegalStateException("nexus-0ty8z owner-migrated acquire failed: " + e.getMessage(), e);
        }
    }

    /** {@link #startDedicated()} with a larger tmpfs and memory limit (see the sized {@link #newContainer}). */
    @SuppressWarnings("resource")
    public static PostgreSQLContainer<?> startDedicated(String tmpfsSize, long memoryBytes) {
        PostgreSQLContainer<?> c = newContainer(DockerImageName.parse(IMAGE), tmpfsSize, memoryBytes);
        c.start();
        return c;
    }


    /**
     * Return a pooled superuser DataSource.
     *
     * <p>Replaces {@code pg.getPostgresDatabase()} when used as a {@code DataSource} argument.
     * Returns {@link HikariDataSource} (not the {@code DataSource} interface) so the caller
     * MUST close it — unlike {@code EmbeddedPostgres.getPostgresDatabase()}, this pool is not
     * owned by the container lifecycle (review nexus-22man: close it in teardown / TWR).
     */
    public static HikariDataSource superuserDataSource(PostgreSQLContainer<?> c) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(c.getJdbcUrl());
        cfg.setUsername(c.getUsername());
        cfg.setPassword(c.getPassword());
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    /**
     * Run the PRODUCT master changelog ({@code db/changelog/db.changelog-master.xml})
     * against {@code su} — the single place every test class's own hand-rolled
     * {@code new Liquibase("db/changelog/db.changelog-master.xml", ...)} call
     * used to live (nexus-cbo4a batch 1a). Creates the {@code nexus}
     * schema and every product table, and — via {@code role-001-nexus-svc.xml}, the
     * FIRST include in the master changelog — creates the {@code nexus_svc} role
     * itself if it does not already exist, so callers never need to pre-create it by
     * hand before this call (see {@link SharedCluster}'s template-bootstrap comment
     * for the same finding, made independently for the shared-cluster path).
     *
     * <p><b>Leaves {@code su} with {@code autoCommit(true)} restored</b> (review
     * finding, nexus-cbo4a batch 1a follow-up): Liquibase manages its own
     * changeset-boundary commits and disables the connection's autoCommit while
     * {@code update()} runs, but does NOT restore it afterward. A caller that
     * issues a further raw statement on this SAME connection after this method
     * returns (e.g. a hand-kept {@code GRANT}) would otherwise run it inside an
     * open transaction that is silently rolled back when {@code su} closes —
     * exactly the bug {@link #bootstrapServiceRole}'s own note below documents.
     * Restoring it here, once, means every caller gets the fix for free instead
     * of re-adding {@code su.setAutoCommit(true)} at each call site.
     *
     * <p><b>The first call on a clone from {@link #start()} is a no-op</b> (nexus-0ty8z): the clone already is the
     * migrated template, with its runAlways grants applied and the {@code nexus_test} objects installed. The
     * skip is per clone and per FIRST CALL, not "untouched": a class that changes the clone before its first call
     * and counts on this call to heal it must call twice. Any later call, an empty database
     * ({@link #startEmptyDatabase}), an owner-template clone ({@link #startMigratedByOwner}), a dedicated
     * container and {@code -Dnexus.test.pg.shared=false} run the changelog. {@code PristineCloneSkipTest} pins it.
     *
     * @param su superuser connection to run the migration under
     */
    public static void applyProductSchema(Connection su) throws Exception {
        // nexus-0ty8z: the first call on an untouched clone of the shared template has nothing to apply. The
        // template is migrated to HEAD (every runAlways grant applied) and carries the nexus_test objects, and a
        // clone copies all of that. Running the changelog anyway replayed ~150 runAlways statements per class
        // (a no-op by construction) for no assertion. Only the FIRST call per clone is skipped, and only on a
        // clone made by SharedCluster.acquireDatabase(): a second call, an empty database, an owner-template clone,
        // a dedicated container, or -Dnexus.test.pg.shared=false all run the changelog as before. The skip is
        // "first call", NOT "nothing touched yet": a class that changes the clone and then calls this once to
        // heal it is skipped. PristineCloneSkipTest pins both the premise and these edges.
        if (SharedCluster.takePristine(su.getCatalog())) {
            su.setAutoCommit(true);
            return;
        }
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        Liquibase liquibase = new Liquibase(
            "db/changelog/db.changelog-master.xml", new ClassLoaderResourceAccessor(), db);
        // nexus-cbo4a batch 9 item 0 (Sam's directive, 2026-09-05; REDESIGNED per T2
        // nexus/critique-nexus-cbo4a-batch-9-search-path): the master changelog's own
        // search-path-001-relocate-vector-extensions.xml GUARDS that vector/pg_trgm
        // are relocated into the nexus schema, attempting the ALTER EXTENSION
        // statements directly (its first-tier fallback) when they are not already
        // relocated. That direct attempt succeeds here because `su` IS a superuser
        // connection and superuser bypasses ownership checks entirely (same as the
        // jOOQ codegen plugin's own Testcontainers bootstrap, which runs this exact
        // changelog the same way) -- no separate Java-side relocation call, and no
        // SECURITY DEFINER helper function, is needed for this superuser-driven
        // walk. See that changeset's own header for the full three-tier derivation
        // (the other two tiers exist for a NOSUPERUSER migrating role, which `su`
        // here is not).
        liquibase.update(new Contexts());
        installTestObjectsAfterProductSchema(su);
    }

    /**
     * Install the {@code nexus_test.*} schema objects (nexus-cbo4a batch 12) —
     * hoisted out of {@link #applyProductSchema} so a caller that migrates the
     * PRODUCT changelog a different way (e.g. via {@code SchemaMigrator.migrate}
     * as the real non-superuser migrating role, rather than this class's own
     * superuser-driven {@code applyProductSchema}) can still install the
     * {@code drop_constraint}/{@code add_fk_not_valid}/{@code
     * add_fk_not_valid_composite3}/{@code set_force_rls}/{@code
     * grant_execute_on_function} test-lifecycle functions {@link #dropConstraint}
     * and friends need. {@code nexus_test} is entirely separate from
     * {@code nexus}/{@code public} and inert with respect to
     * product-schema migration testing — installing it changes nothing the
     * product changelog walk can observe. Never part of the product changelog;
     * the codegen-time counterpart is db.changelog-test-master.xml (see the
     * pom's generate-jooq-test-sources execution).
     *
     * <p><b>ORDERING / OWNERSHIP CONTRACT (found the hard way in batch 12):</b>
     * whichever role's Liquibase run creates {@code databasechangelog} first OWNS
     * it. A test that later migrates the product changelog as a NON-superuser
     * migrating role (SchemaMigratorIntegrationTest's aged-box walks) must call
     * this through THAT role's own connection, never the superuser's, or the
     * walk fails with "permission denied for table databasechangelog". Callers
     * that migrate via {@link #applyProductSchema} (superuser throughout) can
     * pass the same superuser connection, which is what {@code applyProductSchema}
     * itself does. Pass the connection of the role that will run the product
     * changelog on this database, whichever that is.
     *
     * @param conn connection of the role that will migrate the product changelog
     *             on this database (the superuser for applyProductSchema-driven
     *             tests; the migrating role for SchemaMigrator-driven tests)
     */
    /**
     * {@link #installTestObjects} for a database whose product schema is already migrated, refusing if a test object
     * that waits for a product type was skipped. nexus_test.cosine_order is declared on nexus.vector and its changeset
     * carries a precondition (onFail CONTINUE) so a pre-walk install can run before that type exists; once the schema
     * is in place a skip would be silent until a KNN test failed with "function does not exist", so this checks it.
     */
    public static void installTestObjectsAfterProductSchema(Connection conn) throws Exception {
        installTestObjects(conn);
        if (!PgCatalogProbes.routineExists(DSL.using(conn, SQLDialect.POSTGRES), "nexus_test", "cosine_order")) {
            throw new IllegalStateException("nexus_test.cosine_order was not installed although the product schema is"
                + " migrated: its nexus.vector precondition skipped it (nexus-cbo4a)");
        }
    }

    public static void installTestObjects(Connection conn) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
        new Liquibase("db/changelog-test/db.changelog-test-objects.xml",
            new ClassLoaderResourceAccessor(), db).update(new Contexts());
        conn.setAutoCommit(true);
    }

    /**
     * Bootstrap a test-local service role via the {@code db/changelog-test/
     * db.changelog-test-role.xml} test changelog (nexus-cbo4a batch 1a) — replaces
     * the hand-rolled DO-block {@code CREATE ROLE} and schema/table/sequence
     * {@code GRANT}s that 84 test classes used to copy by hand. Creates
     * {@code svcRole} (LOGIN, NOSUPERUSER, NOBYPASSRLS) if absent,
     * redundantly/idempotently ensures {@code nexus_svc} exists too (see
     * {@link #applyProductSchema}'s javadoc — always a no-op here in practice), and
     * grants {@code svcRole} the same {@code nexus} DML/sequence
     * access {@link #grantServiceSchemaAccess} used to hand-grant. Deliberately does
     * NOT set {@code svcRole}'s {@code search_path} (nexus-cbo4a batch 9 item 1,
     * Sam's directive nexus-zrcj7): every legitimate query already goes through
     * schema-qualified jOOQ generated Tables/Routines or a function-pinned
     * {@code SET search_path} in the function definition itself.
     *
     * <p><b>Call AFTER {@link #applyProductSchema}</b> — the {@code GRANT ... ON ALL
     * TABLES}/{@code ON ALL SEQUENCES} statements inside the test changelog require
     * the {@code nexus} schema and its tables to already exist.
     *
     * <p><b>Leaves {@code su} with {@code autoCommit(true)} restored</b> (real bug
     * found converting the six classes that also call {@link #seedServiceToken}, then
     * generalized here, nexus-cbo4a batch 1a follow-up): Liquibase's {@code update()}
     * manages its own changeset-boundary commits by disabling the connection's
     * autoCommit, and does NOT turn it back on when it returns. Every caller that then
     * runs a further raw statement on the SAME {@code su} — a {@code seedServiceToken}
     * insert, or a hand-kept {@code GRANT EXECUTE ON FUNCTION} the fixed grant set here
     * does not cover — was silently executing it inside an open transaction that got
     * rolled back the moment the try-with-resources closed {@code su}, with no
     * exception raised anywhere. The first version of this batch left the fix as a
     * copy-pasted {@code su.setAutoCommit(true)} at 11 call sites; restoring it here
     * once, at the one place every caller already goes through, means the fix cannot
     * be forgotten by a future caller and cannot rot into 11 independently-maintained
     * copies.
     *
     * @param su      superuser connection (the role owner / grantor)
     * @param svcRole the test-local service role name to create and grant
     * @param svcPass the password for {@code svcRole}
     */
    public static void bootstrapServiceRole(Connection su, String svcRole, String svcPass) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        Liquibase liquibase = new Liquibase(
            "db/changelog-test/db.changelog-test-role.xml", new ClassLoaderResourceAccessor(), db);
        Map<String, Object> params = new HashMap<>();
        params.put("svcRole", svcRole);
        params.put("svcPass", svcPass);
        for (var entry : params.entrySet()) {
            liquibase.setChangeLogParameter(entry.getKey(), entry.getValue());
        }
        liquibase.update(new Contexts());
        su.setAutoCommit(true);
    }

    /**
     * Bootstrap a test-local "admin" role that OWNS {@code nexus.topic_assignments}
     * (nexus-4a8pn case (f)) -- see {@code db.changelog-test-admin-role.xml}'s own
     * header for the full rationale. Unlike {@link #bootstrapServiceRole}'s
     * {@code svcRole} (DML-only, never an owner), this role can itself run
     * {@code ALTER TABLE nexus.topic_assignments ... [NO] FORCE ROW LEVEL SECURITY}
     * (a DDL statement PostgreSQL restricts to the table owner or a superuser),
     * mirroring production's non-superuser {@code nexus_admin} migration identity.
     *
     * <p><b>Call this only against a database no other class reads</b> ({@link #start()}'s clone,
     * {@link #startEmptyDatabase}, or a dedicated container). Table ownership is database-local, so a
     * per-class database isolates it; the role itself is cluster-wide, so {@code adminRole} must be a name
     * no other class uses.
     *
     * <p><b>Call AFTER {@link #applyProductSchema}</b>, same ordering contract as
     * {@link #bootstrapServiceRole}.
     *
     * @param su        superuser connection (the grantor / current table owner)
     * @param adminRole the test-local admin role name to create
     * @param adminPass the password for {@code adminRole}
     */
    public static void bootstrapAdminRole(Connection su, String adminRole, String adminPass) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        Liquibase liquibase = new Liquibase(
            "db/changelog-test/db.changelog-test-admin-role.xml", new ClassLoaderResourceAccessor(), db);
        Map<String, Object> params = new HashMap<>();
        params.put("adminRole", adminRole);
        params.put("adminPass", adminPass);
        for (var entry : params.entrySet()) {
            liquibase.setChangeLogParameter(entry.getKey(), entry.getValue());
        }
        liquibase.update(new Contexts());
        su.setAutoCommit(true);
    }

    /**
     * ServiceIntegrationTest's skeleton harness (nexus-cbo4a): the {@code svc_test} role, the
     * FORCE-RLS {@code nexus_test.rls_probe} table and a minimal {@code nexus.service_tokens}
     * seeded with {@code tokenHash}, from {@code db.changelog-test-rls-probe-harness.xml}. Runs
     * WITHOUT the product master changelog; see that file's header. Leaves {@code su} with
     * {@code autoCommit(true)} restored, as {@link #bootstrapServiceRole} does.
     *
     * <p>The Liquibase journal goes to {@code nexus_test}, not {@code public}: the harness pins
     * that {@code /version} reports a {@code schema_error} when there is no
     * {@code public.databasechangelog}, and a journal in {@code public} would turn that missing-table
     * error into a permission error and the test would pass for the wrong reason.
     *
     * @param su        superuser connection on a database no other class reads ({@link #startEmptyDatabase},
     *                  as {@code ServiceIntegrationTest} uses, or a dedicated container); the harness's
     *                  schema, role and tables are per database, the {@code svc_test} role is cluster-wide
     * @param tokenHash sha256 hex of the harness bearer token
     */
    public static void installRlsProbeHarness(Connection su, String tokenHash) throws Exception {
        su.setAutoCommit(true);
        DSL.using(su, SQLDialect.POSTGRES).createSchemaIfNotExists("nexus_test").execute();
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        db.setLiquibaseSchemaName("nexus_test");
        Liquibase liquibase = new Liquibase(
            "db/changelog-test/db.changelog-test-rls-probe-harness.xml", new ClassLoaderResourceAccessor(), db);
        liquibase.setChangeLogParameter("tokenHash", tokenHash);
        liquibase.update(new Contexts());
        su.setAutoCommit(true);
    }

    /**
     * The DBA-side provisioning a NON-SUPERUSER schema owner needs before the product changelog can be
     * walked as that owner (nexus-wbfpw.48): the owner role, {@code nexus_svc}, the database/public
     * grants, both extensions, the {@code nexus} schema owned by {@code adminRole} and the extension
     * relocation helpers. See {@code db.changelog-test-nonsuper-owner.xml}'s header for why a test that
     * wants production's ownership (and so production's row-level-security behaviour for a SECURITY
     * DEFINER function) cannot migrate as the superuser. After this, migrate with
     * {@code SchemaMigrator.migrate} over a pool connected as {@code adminRole}.
     *
     * <p><b>The precondition is a virgin database ({@link #startEmptyDatabase}, or a dedicated container),
     * before any product migration.</b> Role creation is idempotent ({@code IF NOT EXISTS}), so a role that already
     * exists keeps its attributes: this call does not change them, and nothing in it detects the mismatch.
     * A rerun after a migration with the same parameters is safe (the relocation helpers are reinstalled);
     * {@code SchemaRollbackRoundTripIntegrationTest} does exactly that after a rollback drops the
     * {@code nexus} schema. Runs through its own Liquibase bookkeeping tables, so the owner's later product
     * walk does not find a superuser-owned {@code databasechangelog}. The same provisioning as the five-argument
     * overload with both flags {@code true}, minus its refusal of a contradicted flag: these defaults make no claim about a
     * {@code nexus_svc} that already exists.
     *
     * @param su        superuser connection
     * @param adminRole the owner role to create (the production nexus_admin equivalent)
     * @param adminPass its password
     */
    public static void bootstrapNonSuperuserOwner(Connection su, String adminRole, String adminPass) throws Exception {
        // Not the checked five-argument form: the defaults are not a request about nexus_svc, which a shared
        // cluster already holds (NOINHERIT) and the fixtures that call this do not depend on.
        provisionNonSuperuserOwner(su, adminRole, adminPass, true, true);
    }

    /**
     * {@link #bootstrapNonSuperuserOwner(Connection, String, String)} for the fixture that deliberately differs
     * from production's provisioning (nexus-cbo4a, pre-walk bootstrap fold): {@code monitorAdminOption=false}
     * omits {@code GRANT pg_monitor TO adminRole WITH ADMIN OPTION} (the falsification case of
     * {@code GrantsPgMonitorTest}); {@code svcInherits=false} creates {@code nexus_svc} NOINHERIT, as
     * production does.
     *
     * <p>A pre-existing role's attributes are NOT changed, so this overload THROWS rather than ignore a flag the
     * cluster contradicts: where {@code nexus_svc} already exists (every shared cluster: the template migration
     * made it NOINHERIT and a {@code pg_monitor} member) a different {@code svcInherits}, or
     * {@code monitorAdminOption=false}, is refused. Such a fixture stays on {@link #startDedicated()}. A rerun
     * after a migration with the
     * same parameters is safe (every statement is idempotent), as
     * {@code SchemaRollbackRoundTripIntegrationTest} does after a rollback dropped the {@code nexus} schema.
     */
    public static void bootstrapNonSuperuserOwner(Connection su, String adminRole, String adminPass,
                                                  boolean monitorAdminOption, boolean svcInherits) throws Exception {
        requireFlagsTakeEffect(DSL.using(su, SQLDialect.POSTGRES), monitorAdminOption, svcInherits);
        provisionNonSuperuserOwner(su, adminRole, adminPass, monitorAdminOption, svcInherits);
    }

    private static void provisionNonSuperuserOwner(Connection su, String adminRole, String adminPass,
                                                   boolean monitorAdminOption, boolean svcInherits) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-nonsuper-owner.xml",
            "databasechangelog_test_nonsuper_owner", Map.of(
                "adminRole", adminRole, "adminPass", adminPass, "svcPass", SVC_PASSWORD,
                "monitorAdminOption", Boolean.toString(monitorAdminOption),
                "svcInherit", svcInherits ? "INHERIT" : "NOINHERIT"));
    }

    /**
     * The five-argument bootstrap exists for a fixture whose point is a flag. A role that already exists keeps
     * its attributes ({@code CREATE ROLE ... IF NOT EXISTS}), so on a cluster where {@code nexus_svc} was made
     * by the template migration (every shared cluster) a flag it contradicts would be dropped without a word,
     * and a falsification built on it would pass vacuously. Refuse instead.
     */
    private static void requireFlagsTakeEffect(DSLContext ctx, boolean monitorAdminOption, boolean svcInherits) {
        Field<Boolean> inherit = DSL.field(DSL.name("rolinherit"), Boolean.class);
        Boolean existingInherit = ctx.select(inherit).from(DSL.table(DSL.name("pg_roles")))
            .where(DSL.field(DSL.name("rolname"), String.class).eq(SVC_USERNAME)).fetchOne(inherit);
        if (existingInherit == null) {
            return; // a virgin cluster: the provisioning creates the role with the requested flags
        }
        if (existingInherit != svcInherits) {
            throw new IllegalStateException(SVC_USERNAME + " already exists as "
                + (existingInherit ? "INHERIT" : "NOINHERIT") + " but " + (svcInherits ? "INHERIT" : "NOINHERIT")
                + " was requested; the bootstrap cannot change an existing role. Run this fixture on a dedicated "
                + "container (PgContainerHelper.startDedicated()).");
        }
        if (!monitorAdminOption && PgCatalogProbes.hasRole(ctx, SVC_USERNAME, "pg_monitor")) {
            throw new IllegalStateException(SVC_USERNAME + " already holds pg_monitor, so omitting pg_monitor ... "
                + "WITH ADMIN OPTION from the owner would not exercise the missing grant (a vacuous falsification). "
                + "Run this fixture on a dedicated container (PgContainerHelper.startDedicated()).");
        }
    }

    /**
     * Make {@link #SVC_USERNAME}'s NEW sessions log every statement's plan, statements nested in a
     * function included, to the server log (see {@code db.changelog-test-auto-explain.xml}). Open a fresh
     * pool after this; existing pooled connections keep their old settings. Read the plans back with
     * {@link #serverLog}: with the counter on the server log is a file in the container (nexus-sz651), not
     * Docker's log; with it off it is Docker's log.
     */
    public static void enableAutoExplainForService(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-auto-explain.xml",
            "databasechangelog_test_auto_explain", Map.of("svcRole", SVC_USERNAME));
    }

    /** Password every role {@link #grantRoleMembership} creates gets. */
    public static final String MEMBER_PASSWORD = "nexus_member_pass";

    /**
     * {@code GRANT grantedRole TO memberRole WITH INHERIT <inherit>}, creating {@code memberRole} as a LOGIN
     * role (password {@link #MEMBER_PASSWORD}) when it does not exist yet. {@code bypassRls} applies only to a
     * role created here. See {@code db.changelog-test-role-membership.xml}. Role names are fixed test
     * literals, never input.
     */
    public static void grantRoleMembership(Connection su, String grantedRole, String memberRole,
                                           boolean inherit, boolean bypassRls) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-role-membership.xml",
            "databasechangelog_test_role_membership", Map.of(
                "grantedRole", grantedRole, "memberRole", memberRole, "memberPass", MEMBER_PASSWORD,
                "inheritOption", inherit ? "TRUE" : "FALSE",
                "memberAttrs", bypassRls ? "BYPASSRLS" : "NOBYPASSRLS"));
    }

    /**
     * Gives {@code role} what the scratch tests need on the t1 schema (nexus-cbo4a): USAGE on the schema,
     * through {@code db.changelog-test-t1-schema-usage.xml} (jOOQ has no GRANT ON SCHEMA), then SELECT,
     * INSERT, UPDATE and DELETE on {@code t1.scratch} through jOOQ. {@link #bootstrapServiceRole} covers
     * only nexus and nexus_test, so this is the one place t1 is granted. Leaves {@code su} autoCommit.
     */
    public static void grantT1ScratchTo(Connection su, String role) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-t1-schema-usage.xml",
            "databasechangelog_test_t1_schema_usage", Map.of("role", role));
        DSL.using(su, SQLDialect.POSTGRES)
            .grant(DSL.privilege("SELECT"), DSL.privilege("INSERT"),
                   DSL.privilege("UPDATE"), DSL.privilege("DELETE"))
            .on(dev.nexus.service.jooq.t1.Tables.SCRATCH)
            .to(DSL.role(role))
            .execute();
    }

    /**
     * {@code CREATE ROLE role NOLOGIN} (nexus-cbo4a batch 7), through {@code db.changelog-test-nologin-role.xml}:
     * jOOQ has no CREATE ROLE. Role names are fixed test literals, never input. Leaves {@code su} autoCommit.
     */
    public static void createNologinRole(Connection su, String role) throws Exception {
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-nologin-role.xml",
            "databasechangelog_test_nologin_role", Map.of("role", role));
    }

    /** What a {@code Table<?>} may render to before it is substituted into a changelog: one or two quoted plain names. */
    static final java.util.regex.Pattern RENDERED_TABLE = java.util.regex.Pattern.compile(
        "^(\"[a-z_][a-z0-9_]*\"\\.)?\"[a-z_][a-z0-9_]*\"$");

    /**
     * Render {@code table} for a changelog parameter and refuse anything that is not a quoted, optionally
     * schema-qualified plain name. A {@code Table} built by {@code DSL.table(String)} is plain SQL and renders as
     * whatever text it was given, so the type alone does not make the parameter safe; the rendered form is checked.
     * Package-private so a unit pin can call it with a context that has no connection.
     */
    static String renderTableParam(DSLContext ctx, Table<?> table) {
        String rendered = ctx.render(table);
        if (!RENDERED_TABLE.matcher(rendered).matches()) {
            throw new IllegalArgumentException("table does not render as a quoted plain name: " + rendered);
        }
        return rendered;
    }

    /** The batch 7 helpers substitute names into changelog SQL unquoted: accept only a plain lowercase identifier. */
    static void requireTestIdentifier(String name) {
        if (name == null || !name.matches("[a-z_][a-z0-9_]*") || name.equals("public")) {
            throw new IllegalArgumentException("not a plain test identifier: " + name);
        }
    }

    /**
     * The password is substituted into a single-quoted SQL literal: accept only letters, digits and underscore, so no
     * quote, dollar, brace, backslash or whitespace can end the literal or reach the changelog's property syntax.
     * The message omits the value: a rejected password must not be echoed into a log.
     */
    private static void requireTestPasswordLiteral(String password) {
        if (password == null || !password.matches("[A-Za-z0-9_]+")) {
            throw new IllegalArgumentException("not a safe test password literal (letters, digits and underscore only)");
        }
    }

    /**
     * {@code GRANT USAGE ON SCHEMA schema TO role} (nexus-cbo4a batch 7), through
     * {@code db.changelog-test-schema-usage.xml}: jOOQ's grant resolves its target to a Table, so it cannot name
     * a schema. Leaves {@code su} autoCommit.
     */
    public static void grantSchemaUsage(Connection su, String schema, String role) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-schema-usage.xml",
            "databasechangelog_test_schema_usage", Map.of("schema", schema, "role", role));
    }

    /**
     * {@code ALTER DEFAULT PRIVILEGES IN SCHEMA schema GRANT SELECT ON TABLES TO role} (nexus-cbo4a batch 7),
     * through {@code db.changelog-test-schema-default-select-acl.xml}: jOOQ has no ALTER DEFAULT PRIVILEGES.
     * Leaves {@code su} autoCommit.
     */
    public static void grantSchemaDefaultSelectAcl(Connection su, String schema, String role) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(role);
        if (schema.equals("nexus") || schema.equals("public")) {
            throw new IllegalArgumentException("a default SELECT ACL on " + schema + " would reach every later table");
        }
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-schema-default-select-acl.xml",
            "databasechangelog_test_schema_default_acl", Map.of("schema", schema, "role", role));
    }

    /**
     * {@code ALTER TABLE table OWNER TO owner} (nexus-cbo4a batch 7), through
     * {@code db.changelog-test-table-owner.xml}: jOOQ has no OWNER TO. {@code table} is a jOOQ {@link Table},
     * rendered quoted and schema-qualified. Leaves {@code su} autoCommit.
     */
    public static void alterTableOwner(Connection su, Table<?> table, String owner) throws Exception {
        requireTestIdentifier(owner);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-table-owner.xml",
            "databasechangelog_test_table_owner",
            Map.of("table", renderTableParam(DSL.using(su, SQLDialect.POSTGRES), table), "owner", owner));
    }

    /**
     * {@code CREATE ROLE role LOGIN PASSWORD password NOSUPERUSER NOCREATEDB NOCREATEROLE [NO]BYPASSRLS}
     * (nexus-cbo4a batch 8), through {@code db.changelog-test-login-role.xml}: jOOQ has no CREATE ROLE. A role that
     * already exists fails, as the bare statement did. Role name and password are fixed test literals; the role must
     * be a plain lowercase identifier and the password a quote-free word ({@link #requireTestPasswordLiteral}).
     * Leaves {@code su} autoCommit.
     */
    public static void createLoginRole(Connection su, String role, String password, boolean bypassRls) throws Exception {
        requireTestIdentifier(role);
        requireTestPasswordLiteral(password);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-login-role.xml",
            "databasechangelog_test_login_role", Map.of(
                "role", role, "pass", password, "rlsAttr", bypassRls ? "BYPASSRLS" : "NOBYPASSRLS"));
    }

    /**
     * {@code GRANT grantedRole TO memberRole WITH ADMIN OPTION} (nexus-cbo4a batch 8), through
     * {@code db.changelog-test-role-admin-option.xml}: jOOQ's grant is an object-privilege grant, not a role
     * membership. {@link #grantRoleMembership} is the INHERIT form. Leaves {@code su} autoCommit.
     */
    public static void grantRoleWithAdminOption(Connection su, String grantedRole, String memberRole) throws Exception {
        requireTestIdentifier(grantedRole);
        requireTestIdentifier(memberRole);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-role-admin-option.xml",
            "databasechangelog_test_role_admin_option", Map.of("grantedRole", grantedRole, "memberRole", memberRole));
    }

    /**
     * Production-shaped ownership of a migrated product schema (nexus-cbo4a batch 8), through
     * {@code db.changelog-test-product-schema-owner.xml}: {@code role} gets USAGE and CREATE on nexus, t1 and public
     * and becomes the owner of every nexus/t1 table and sequence. Call after the superuser's walk. Leaves
     * {@code su} autoCommit.
     */
    public static void handProductSchemaTo(Connection su, String role) throws Exception {
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-product-schema-owner.xml",
            "databasechangelog_test_product_schema_owner", Map.of("role", role));
    }

    /**
     * {@code ALTER TABLE table SET SCHEMA schema} (nexus-cbo4a batch 8), through
     * {@code db.changelog-test-table-schema.xml}: jOOQ has no SET SCHEMA for a table. Run it on a connection of the
     * table's owner; {@code table} is a jOOQ {@link Table}, rendered quoted and schema-qualified, and {@code schema}
     * a plain test identifier other than {@code public}. The bookkeeping tables land in the connection's current
     * schema under a name that is not {@code databasechangelog}. Leaves {@code conn} autoCommit.
     */
    public static void setTableSchema(Connection conn, Table<?> table, String schema) throws Exception {
        requireTestIdentifier(schema);
        runSuperuserTestChangelog(conn, "db/changelog-test/db.changelog-test-table-schema.xml",
            "databasechangelog_test_table_schema",
            Map.of("table", renderTableParam(DSL.using(conn, SQLDialect.POSTGRES), table), "schema", schema));
    }

    /**
     * Revoke every role membership {@code role} granted, then {@code DROP ROLE IF EXISTS role} (nexus-cbo4a batch 8),
     * through {@code db.changelog-test-drop-exclusive-role.xml}: jOOQ has neither DROP ROLE nor REVOKE ... GRANTED BY.
     * {@code SharedDatabaseHandle}'s teardown of an exclusive role; {@code admin} is the control connection, and the
     * role's database must already be gone. Leaves {@code admin} autoCommit.
     */
    public static void dropRoleRevokingGrants(Connection admin, String role) throws Exception {
        requireTestIdentifier(role);
        runSuperuserTestChangelog(admin, "db/changelog-test/db.changelog-test-drop-exclusive-role.xml",
            "databasechangelog_test_drop_role", Map.of("role", role));
    }

    // ── nexus-cbo4a batch 11: the typed replacements for the old runSuperuserDdl helper ──────────
    //
    // That helper took a DDL string from the caller and ran it through a changelog parameter, which is exactly
    // a SQL string in Java that RawSqlGateTest could not see. It is deleted. What replaced each of its 64 callers is
    // one of: typed jOOQ (createTable/dropTable/truncate/grant/revoke/dropSchema), an existing helper, or one of the
    // methods below. Every method below fixes the statement's SHAPE in a test changelog and lets the caller choose
    // only validated names or closed-list values; none takes SQL text.

    /** Plain lowercase identifier or {@code PUBLIC}, for a policy's TO clause. */
    private static void requireRoleOrPublic(String role) {
        if (!"PUBLIC".equals(role)) {
            requireTestIdentifier(role);
        }
    }

    /** The embedding columns of nexus.chunks, the only columns an index fixture may name. */
    private static final Set<String> EMBEDDING_COLUMNS = Set.of("embedding_384", "embedding_768", "embedding_1024");

    /** What {@link #requireTestLiteral} admits; package-private so the unit pin names the same expression. */
    static final java.util.regex.Pattern TEST_LITERAL = java.util.regex.Pattern.compile("[A-Za-z0-9_.'\\\\-]+");

    /**
     * A value that goes inside a single-quoted literal of a test changelog: letters, digits, underscore, dot, hyphen,
     * apostrophe and backslash only (no dollar, brace or whitespace, so nothing reaches the changelog's property
     * syntax), with each apostrophe doubled. The apostrophe is admitted because PciCatalogIntegrationTest round-trips
     * a collection name that has one. The backslash is admitted because the same class round-trips one that has a
     * backslash, and it is a literal character there: the engine runs with {@code standard_conforming_strings = on}
     * (that test asserts it), so a backslash in {@code '...'} is not an escape and apostrophe doubling is the only
     * escaping a literal needs. Package-private so a unit pin can call it with no database.
     */
    static String requireTestLiteral(String value) {
        if (value == null || !TEST_LITERAL.matcher(value).matches()) {
            throw new IllegalArgumentException("not a safe test literal: " + value);
        }
        return value.replace("'", "''");
    }

    /**
     * {@code ALTER ROLE role SET application_name TO 'empty_db_poison'} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-role-application-name-poison.xml}: jOOQ has no ALTER ROLE. The value is fixed in the
     * changelog; a cluster-wide setting on {@code role} that SharedCluster's reset-at-acquire must clear.
     */
    public static void setRoleApplicationNamePoison(Connection su, String role) throws Exception {
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-role-application-name-poison.xml",
            "databasechangelog_test_role_app_name_poison", Map.of("role", role));
    }

    /**
     * {@code GRANT grantedRole TO memberRole GRANTED BY grantor} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-role-granted-by.xml}: a role-membership grant with a named grantor has no jOOQ form.
     */
    public static void grantRoleGrantedBy(Connection su, String grantedRole, String memberRole, String grantor)
            throws Exception {
        requireTestIdentifier(grantedRole);
        requireTestIdentifier(memberRole);
        requireTestIdentifier(grantor);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-role-granted-by.xml",
            "databasechangelog_test_role_granted_by",
            Map.of("grantedRole", grantedRole, "memberRole", memberRole, "grantor", grantor));
    }

    /**
     * {@code CREATE SCHEMA schema AUTHORIZATION owner} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-schema-authorization.xml}: jOOQ's createSchema has no AUTHORIZATION clause.
     */
    public static void createSchemaAuthorization(Connection su, String schema, String owner) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(owner);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-schema-authorization.xml",
            "databasechangelog_test_schema_authorization", Map.of("schema", schema, "owner", owner));
    }

    /**
     * The table {@code nx_sz651_nested_probe}, created by a {@code CREATE TABLE} issued through {@code EXECUTE}
     * inside a {@code DO} block (nexus-cbo4a batch 11), through {@code db.changelog-test-nested-ddl-probe.xml}:
     * SharedClusterEmptyDatabaseTest needs DDL the server sees only as a NESTED command.
     */
    public static void createProbeTableThroughDoBlock(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-nested-ddl-probe.xml",
            "databasechangelog_test_nested_ddl_probe", Map.of());
    }

    /**
     * {@code nexus.collection_vector_stats_v019}, the vectors-019-5 view definition under a probe name, readable
     * by PUBLIC (nexus-cbo4a batch 11). The view is {@code db.changelog-test-collection-vector-stats-v019.xml}
     * (jOOQ's createView has no {@code WITH (security_invoker = true)}); the GRANT is typed jOOQ.
     */
    public static void createCollectionVectorStatsV019View(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-collection-vector-stats-v019.xml",
            "databasechangelog_test_cvs_v019", Map.of());
        DSL.using(su, SQLDialect.POSTGRES)
            .grant(DSL.privilege("SELECT"))
            .on(DSL.table(DSL.name("nexus", "collection_vector_stats_v019")))
            .toPublic()
            .execute();
    }

    /**
     * {@code GRANT MAINTAIN ON nexus.chunks TO role}, typed jOOQ (nexus-cbo4a batch 11): the grant production's
     * nexus_svc has (grants-005-chunks-unify-maintain), given to a test's own service role.
     */
    public static void grantMaintainOnChunks(Connection su, String role) {
        DSL.using(su, SQLDialect.POSTGRES)
            .grant(DSL.privilege("MAINTAIN"))
            .on(CHUNKS)
            .to(DSL.role(role))
            .execute();
    }

    /**
     * The undo of {@link #enableAutoExplainForService}'s module load (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-auto-explain-reset.xml}: {@code ALTER ROLE nexus_svc RESET session_preload_libraries}.
     * Sessions opened afterwards run without auto_explain.
     */
    public static void disableAutoExplainForService(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-auto-explain-reset.xml",
            "databasechangelog_test_auto_explain_reset", Map.of("svcRole", SVC_USERNAME));
    }

    /** The storage parameters {@link #setTableStorageParams} and {@link #resetTableStorageParams} accept. */
    private static final Set<String> STORAGE_PARAM_ALLOWLIST = Set.of(
        "autovacuum_enabled", "autovacuum_vacuum_scale_factor", "autovacuum_vacuum_threshold",
        "autovacuum_vacuum_insert_scale_factor", "autovacuum_analyze_scale_factor", "fillfactor");

    private static void requireStorageParamName(String name) {
        if (!STORAGE_PARAM_ALLOWLIST.contains(name)) {
            throw new IllegalArgumentException(
                "storage parameter not permitted: " + name + " (allowed: " + STORAGE_PARAM_ALLOWLIST + ")");
        }
    }

    /**
     * {@code ALTER TABLE table SET (name = value, ...)} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-table-storage-set.xml}: jOOQ has no storage parameters. The names come from
     * {@link #STORAGE_PARAM_ALLOWLIST}; a value is a plain number or {@code true}/{@code false}. The options keep the
     * map's iteration order, so pass a {@link java.util.LinkedHashMap} where the order matters.
     */
    public static void setTableStorageParams(Connection conn, Table<?> table, Map<String, String> options)
            throws Exception {
        if (options.isEmpty()) {
            throw new IllegalArgumentException("no storage parameters to set");
        }
        List<String> parts = new ArrayList<>();
        for (var option : options.entrySet()) {
            requireStorageParamName(option.getKey());
            if (option.getValue() == null || !option.getValue().matches("[0-9]+(\\.[0-9]+)?|true|false")) {
                throw new IllegalArgumentException("not a plain storage parameter value: " + option.getValue());
            }
            parts.add(option.getKey() + " = " + option.getValue());
        }
        runSuperuserTestChangelog(conn, "db/changelog-test/db.changelog-test-table-storage-set.xml",
            "databasechangelog_test_table_storage_set", Map.of(
                "table", renderTableParam(DSL.using(conn, SQLDialect.POSTGRES), table), "options", String.join(", ", parts)));
    }

    /**
     * {@code ALTER TABLE table RESET (name, ...)} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-table-storage-reset.xml}; the names come from {@link #STORAGE_PARAM_ALLOWLIST}.
     */
    public static void resetTableStorageParams(Connection conn, Table<?> table, String... names) throws Exception {
        if (names.length == 0) {
            throw new IllegalArgumentException("no storage parameters to reset");
        }
        for (String name : names) {
            requireStorageParamName(name);
        }
        runSuperuserTestChangelog(conn, "db/changelog-test/db.changelog-test-table-storage-reset.xml",
            "databasechangelog_test_table_storage_reset", Map.of(
                "table", renderTableParam(DSL.using(conn, SQLDialect.POSTGRES), table), "options", String.join(", ", names)));
    }

    /**
     * The per-collection partial HNSW index the PCI builder writes, as a fixture (nexus-cbo4a batch 11):
     * {@code CREATE INDEX index ON schema.table USING hnsw (column nexus.vector_cosine_ops) WHERE collection =
     * 'collection'}, through {@code db.changelog-test-hnsw-index-collection.xml}. Not CONCURRENTLY; a build that must
     * be observed mid-flight is {@link #createLeafHnswIndexConcurrently}'s.
     */
    public static void createLeafHnswIndex(Connection su, String schema, String table, String index, String column,
                                           String collection) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        requireTestIdentifier(index);
        requireEmbeddingColumn(column);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-hnsw-index-collection.xml",
            "databasechangelog_test_hnsw_index_collection", Map.of("schema", schema, "table", table, "index", index,
                "column", column, "collection", requireTestLiteral(collection)));
    }

    /**
     * An HNSW index whose predicate is the compound {@code collection = 'c' AND tenant_id = 't'} (nexus-cbo4a batch
     * 11), through {@code db.changelog-test-hnsw-index-collection-tenant.xml}: a shape the PCI catalog reader must
     * count as unparsed.
     */
    public static void createLeafHnswIndexCollectionAndTenant(Connection su, String schema, String table, String index,
                                                              String column, String collection, String tenant)
            throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        requireTestIdentifier(index);
        requireEmbeddingColumn(column);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-hnsw-index-collection-tenant.xml",
            "databasechangelog_test_hnsw_index_coll_tenant", Map.of("schema", schema, "table", table, "index", index,
                "column", column, "collection", requireTestLiteral(collection), "tenant", requireTestLiteral(tenant)));
    }

    /**
     * An HNSW index with no predicate (nexus-cbo4a batch 11), through the existing
     * {@code db.changelog-test-hnsw-index.xml}: {@code CREATE INDEX index ON schema.table USING hnsw (column
     * nexus.vector_cosine_ops)}.
     */
    public static void createHnswIndex(Connection su, String schema, String table, String index, String column)
            throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        requireTestIdentifier(index);
        requireEmbeddingColumn(column);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-hnsw-index.xml",
            "databasechangelog_test_hnsw_index", Map.of(
                "indexName", index, "tableName", schema + "." + table, "column", column));
    }

    /**
     * {@code CREATE INDEX index ON schema.table (column) WHERE column = 'value'} (nexus-cbo4a batch 11), typed
     * jOOQ {@code createIndex(..).on(..).where(..)}: an index the PCI catalog reader must not report. Names are
     * quoted by jOOQ and the value is rendered as an escaped literal, so no changelog and no value validator is
     * involved; the names still go through {@link #requireTestIdentifier} like every sibling helper.
     */
    public static void createBtreeIndexWhereEquals(Connection su, String schema, String table, String index,
                                                   String column, String value) {
        btreeIndexWhereEqualsQuery(DSL.using(su, SQLDialect.POSTGRES), schema, table, index, column, value).execute();
    }

    /** The statement {@link #createBtreeIndexWhereEquals} runs, unexecuted so a unit pin can render it. */
    static Query btreeIndexWhereEqualsQuery(DSLContext ctx, String schema, String table, String index,
                                            String column, String value) {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        requireTestIdentifier(index);
        requireTestIdentifier(column);
        Field<String> col = DSL.field(DSL.name(column), String.class);
        return ctx.createIndex(DSL.name(index)).on(DSL.table(DSL.name(schema, table)), col).where(col.eq(value));
    }

    private static void requireEmbeddingColumn(String column) {
        if (!EMBEDDING_COLUMNS.contains(column)) {
            throw new IllegalArgumentException("not an embedding column: " + column + " (allowed: " + EMBEDDING_COLUMNS + ")");
        }
    }

    /**
     * {@code UPDATE pg_catalog.pg_index SET indisvalid = false} for one index (nexus-cbo4a batch 11), typed jOOQ over
     * the catalog: what an interrupted {@code REINDEX INDEX CONCURRENTLY} leaves behind. The index is found by schema
     * and name; an UPDATE that touches no row throws, so it cannot pass vacuously. Superuser only.
     */
    public static void markIndexInvalid(Connection su, String schema, String index) {
        setIndexValid(su, schema, index, false);
    }

    /** The inverse of {@link #markIndexInvalid}: an index that became valid after a snapshot judged it a leftover. */
    public static void markIndexValid(Connection su, String schema, String index) {
        setIndexValid(su, schema, index, true);
    }

    private static void setIndexValid(Connection su, String schema, String index, boolean valid) {
        Table<?> pgClass = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        Table<?> pgNamespace = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<Object> classOid = DSL.field(DSL.name("c", "oid"));
        int updated = DSL.using(su, SQLDialect.POSTGRES)
            .update(DSL.table(DSL.name("pg_catalog", "pg_index")))
            .set(DSL.field(DSL.name("indisvalid"), Boolean.class), valid)
            .where(DSL.field(DSL.name("indexrelid")).eq(
                DSL.select(classOid).from(pgClass).join(pgNamespace)
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                    .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
                    .and(DSL.field(DSL.name("c", "relname"), String.class).eq(index))))
            .execute();
        if (updated != 1) {
            throw new IllegalStateException("expected to mark exactly one index " + (valid ? "valid" : "invalid")
                + ", found " + updated + ": " + schema + "." + index);
        }
    }

    /**
     * {@code DROP INDEX IF EXISTS <qualified>} (nexus-cbo4a batch 11): jOOQ's {@code dropIndexIfExists} renders the
     * name unqualified on POSTGRES and would silently find nothing, so the existence check is
     * {@code to_regclass} over the rendered qualified name and the drop is {@link #dropIndex}.
     */
    public static void dropIndexIfExists(Connection conn, Name qualifiedIndex) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        String found = ctx.select(DSL.function("to_regclass", String.class, DSL.val(ctx.render(qualifiedIndex))))
            .fetchOne(0, String.class);
        if (found != null) {
            dropIndex(conn, qualifiedIndex);
        }
    }

    /**
     * {@code ALTER ROLE role CONNECTION LIMIT limit} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-role-connection-limit.xml}: jOOQ has no ALTER ROLE. {@code 0} refuses every new
     * connection, {@code -1} lifts the limit.
     */
    public static void setRoleConnectionLimit(Connection su, String role, int limit) throws Exception {
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-role-connection-limit.xml",
            "databasechangelog_test_role_conn_limit", Map.of("role", role, "limit", Integer.toString(limit)));
    }

    /** {@code GRANT CREATE ON SCHEMA schema TO role} (nexus-cbo4a batch 11), through {@code db.changelog-test-schema-create-grant.xml}. */
    public static void grantSchemaCreate(Connection su, String schema, String role) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-schema-create-grant.xml",
            "databasechangelog_test_schema_create_grant", Map.of("schema", schema, "role", role));
    }

    /** {@code REVOKE CREATE ON SCHEMA schema FROM role} (nexus-cbo4a batch 11), through {@code db.changelog-test-schema-create-revoke.xml}. */
    public static void revokeSchemaCreate(Connection su, String schema, String role) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(role);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-schema-create-revoke.xml",
            "databasechangelog_test_schema_create_revoke", Map.of("schema", schema, "role", role));
    }

    /**
     * {@code CREATE ROLE role PASSWORD 'password' NOLOGIN} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-nologin-role-password.xml}. The password goes through {@link #requireTestPasswordLiteral}.
     */
    public static void createNologinRoleWithPassword(Connection su, String role, String password) throws Exception {
        requireTestIdentifier(role);
        requireTestPasswordLiteral(password);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-nologin-role-password.xml",
            "databasechangelog_test_nologin_role_password", Map.of("role", role, "pass", password));
    }

    /**
     * {@code CREATE ROLE role LOGIN PASSWORD 'password' SUPERUSER NOBYPASSRLS} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-superuser-login-role.xml}. The password goes through {@link #requireTestPasswordLiteral}.
     */
    public static void createSuperuserLoginRole(Connection su, String role, String password) throws Exception {
        requireTestIdentifier(role);
        requireTestPasswordLiteral(password);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-superuser-login-role.xml",
            "databasechangelog_test_superuser_login_role", Map.of("role", role, "pass", password));
    }

    /** PERMISSIVE or RESTRICTIVE, for {@link #createPolicyUsingTrue}. */
    public enum PolicyKind { PERMISSIVE, RESTRICTIVE }

    /**
     * {@code CREATE POLICY policy ON table AS kind FOR command TO role USING (true)} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-create-policy.xml}: jOOQ has no CREATE POLICY. {@code command} is one of ALL, SELECT,
     * INSERT, UPDATE, DELETE; {@code role} a plain identifier or {@code PUBLIC}.
     */
    public static void createPolicyUsingTrue(Connection su, Table<?> table, String policy, PolicyKind kind,
                                             String command, String role) throws Exception {
        requireTestIdentifier(policy);
        requireRoleOrPublic(role);
        if (!Set.of("ALL", "SELECT", "INSERT", "UPDATE", "DELETE").contains(command)) {
            throw new IllegalArgumentException("not a policy command: " + command);
        }
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-create-policy.xml",
            "databasechangelog_test_create_policy", Map.of(
                "table", renderTableParam(DSL.using(su, SQLDialect.POSTGRES), table), "policy", policy,
                "kind", kind.name(), "command", command, "role", role));
    }

    /**
     * {@code DROP POLICY policy ON table} (nexus-cbo4a batch 11), through {@code db.changelog-test-drop-policy.xml}.
     * Not {@code nexus_test.drop_policy}: it also runs in a database the product changelog alone walked.
     */
    public static void dropPolicy(Connection conn, Table<?> table, String policy) throws Exception {
        requireTestIdentifier(policy);
        runSuperuserTestChangelog(conn, "db/changelog-test/db.changelog-test-drop-policy.xml",
            "databasechangelog_test_drop_policy",
            Map.of("table", renderTableParam(DSL.using(conn, SQLDialect.POSTGRES), table), "policy", policy));
    }

    /** The four row-level-security switches of {@code ALTER TABLE}, for {@link #setRowSecurity}. */
    public enum RowSecurityMode {
        ENABLE("ENABLE"), DISABLE("DISABLE"), FORCE("FORCE"), NO_FORCE("NO FORCE");

        private final String keyword;

        RowSecurityMode(String keyword) {
            this.keyword = keyword;
        }
    }

    /**
     * {@code ALTER TABLE table [ENABLE|DISABLE|FORCE|NO FORCE] ROW LEVEL SECURITY} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-table-row-security.xml}. Not {@code nexus_test.set_row_security}/{@link #setForceRls}:
     * it also runs in a database the product changelog alone walked, which has no nexus_test schema.
     */
    public static void setRowSecurity(Connection conn, Table<?> table, RowSecurityMode mode) throws Exception {
        runSuperuserTestChangelog(conn, "db/changelog-test/db.changelog-test-table-row-security.xml",
            "databasechangelog_test_table_row_security", Map.of(
                "table", renderTableParam(DSL.using(conn, SQLDialect.POSTGRES), table), "mode", mode.keyword));
    }

    /** {@code CREATE EXTENSION plpgsql_check} (nexus-cbo4a batch 11), through {@code db.changelog-test-extension-plpgsql-check.xml}. */
    public static void createPlpgsqlCheckExtension(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-extension-plpgsql-check.xml",
            "databasechangelog_test_ext_plpgsql_check", Map.of());
    }

    /** The bookkeeping table the concurrent-index helpers share, so one lock clear covers them all. */
    private static final String CONCURRENT_INDEX_BOOKKEEPING = "databasechangelog_test_concurrent_index";

    /**
     * {@code CREATE INDEX CONCURRENTLY index ON schema.table USING hnsw (column nexus.vector_cosine_ops) WHERE
     * collection = 'collection'} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-hnsw-index-collection-concurrently.xml}, a changeset with runInTransaction="false"
     * (PostgreSQL refuses CONCURRENTLY inside a transaction block). The statement blocks until it ends, so a
     * caller that wants to observe it mid-flight runs this on a thread of its own with its own connection.
     *
     * <p><b>A run whose backend is terminated leaves the Liquibase lock held.</b> Liquibase cannot release its
     * lock row over the dead connection, so the next call blocks on it (for minutes) instead of running. A test
     * that terminates the backend of a build started here must call {@link #clearConcurrentIndexBuildLock}
     * before it calls this method or {@link #createBuilderHnswIndexConcurrently} again.
     */
    public static void createLeafHnswIndexConcurrently(Connection su, String schema, String table, String index,
                                                       String column, String collection) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        requireTestIdentifier(index);
        requireEmbeddingColumn(column);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-hnsw-index-collection-concurrently.xml",
            CONCURRENT_INDEX_BOOKKEEPING, Map.of("schema", schema, "table", table, "index", index,
                "column", column, "collection", requireTestLiteral(collection)));
    }

    /**
     * The statement {@code PciBuilderSession} sends, as a fixture (nexus-cbo4a batch 11): {@link
     * #createLeafHnswIndexConcurrently} plus {@code WITH (m = m, ef_construction = efConstruction)}, through
     * {@code db.changelog-test-hnsw-index-builder-concurrently.xml}. Both numbers must be positive. The lock caveat
     * of {@link #createLeafHnswIndexConcurrently} applies.
     */
    public static void createBuilderHnswIndexConcurrently(Connection su, String schema, String table, String index,
                                                          String column, String collection, int m, int efConstruction)
            throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        requireTestIdentifier(index);
        requireEmbeddingColumn(column);
        if (m <= 0 || efConstruction <= 0) {
            throw new IllegalArgumentException("m and ef_construction must be positive: " + m + ", " + efConstruction);
        }
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-hnsw-index-builder-concurrently.xml",
            CONCURRENT_INDEX_BOOKKEEPING, Map.of("schema", schema, "table", table, "index", index,
                "column", column, "collection", requireTestLiteral(collection),
                "m", Integer.toString(m), "ef_construction", Integer.toString(efConstruction)));
    }

    /**
     * Release the Liquibase lock {@link #createLeafHnswIndexConcurrently} or {@link #createBuilderHnswIndexConcurrently}
     * may have left held when its backend was terminated mid-statement: {@code locked = false} on its lock row, as the
     * superuser that owns the table.
     */
    public static void clearConcurrentIndexBuildLock(Connection su) {
        Table<?> lock = DSL.table(DSL.name(CONCURRENT_INDEX_BOOKKEEPING + "_lock"));
        DSL.using(su, SQLDialect.POSTGRES)
            .update(lock)
            .set(DSL.field(DSL.name("locked"), Boolean.class), false)
            .execute();
    }

    /**
     * {@code REINDEX INDEX CONCURRENTLY schema.index} (nexus-cbo4a batch 11), through
     * {@code db.changelog-test-reindex-index-concurrently.xml}: jOOQ has no REINDEX. Like {@link #reindexIndex} it
     * runs on {@code su} itself with no enclosing transaction, so session settings made on {@code su} first
     * ({@code statement_timeout}, {@code application_name}) apply to it. It blocks until it ends or is cancelled.
     */
    public static void reindexIndexConcurrently(Connection su, String schema, String index) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(index);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-reindex-index-concurrently.xml",
            "databasechangelog_test_reindex_concurrently", Map.of("schema", schema, "index", index));
    }

    /**
     * {@code VACUUM schema.table} (nexus-cbo4a batch 11), through {@code db.changelog-test-vacuum-table.xml}: jOOQ
     * has no VACUUM, and PostgreSQL refuses it inside a transaction block. Both names go through
     * {@link #requireTestIdentifier}.
     */
    public static void vacuumTable(Connection su, String schema, String table) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(table);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-vacuum-table.xml",
            "databasechangelog_test_vacuum_table", Map.of("schema", schema, "table", table));
    }

    /**
     * {@code CREATE DATABASE name} on a throwaway database (nexus-cbo4a batch 11), jOOQ {@code createDatabase}.
     * Autocommit is switched on first: PostgreSQL refuses the statement inside a transaction block.
     */
    public static void createDatabase(Connection su, String name) throws SQLException {
        requireTestIdentifier(name);
        su.setAutoCommit(true);
        DSL.using(su, SQLDialect.POSTGRES).createDatabase(DSL.name(name)).execute();
    }

    /** {@code DROP DATABASE IF EXISTS name} (nexus-cbo4a batch 11), jOOQ {@code dropDatabaseIfExists}; autocommit as in {@link #createDatabase}. */
    public static void dropDatabaseIfExists(Connection su, String name) throws SQLException {
        requireTestIdentifier(name);
        su.setAutoCommit(true);
        DSL.using(su, SQLDialect.POSTGRES).dropDatabaseIfExists(DSL.name(name)).execute();
    }

    /**
     * Run a test changelog on {@code su} through its own bookkeeping table. Despite the name the
     * connection may be any role the changelog's statements are allowed for: the RDR-225 scratch
     * fixture runs as the schema owner so its scratch tables are owned by the role that owns the
     * functions under test.
     */
    public static void runSuperuserTestChangelog(Connection su, String changelog, String bookkeepingTable,
                                                 Map<String, String> params) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        db.setDatabaseChangeLogTableName(bookkeepingTable);
        db.setDatabaseChangeLogLockTableName(bookkeepingTable + "_lock");
        Liquibase liquibase = new Liquibase(changelog, new ClassLoaderResourceAccessor(), db);
        for (var entry : params.entrySet()) {
            liquibase.setChangeLogParameter(entry.getKey(), entry.getValue());
        }
        liquibase.update(new Contexts());
        su.setAutoCommit(true);
    }

    /**
     * Seed one {@code nexus.service_tokens} row via generated jOOQ DSL (nexus-cbo4a
     * batch 1a) — replaces the hand-rolled {@code INSERT INTO nexus.service_tokens
     * (token_hash, tenant_id, label) VALUES (...) ON CONFLICT (token_hash) DO
     * NOTHING} six test classes used to build by string concatenation. The raw
     * token is hashed via {@link TokenHashing#sha256Hex}, matching production's own
     * issuance path exactly.
     *
     * @param dsl    a {@link DSLContext} over the same connection/role the schema
     *               was migrated under (e.g. {@code DSL.using(su, SQLDialect.POSTGRES)})
     * @param token  the raw bearer token to hash and store
     * @param tenant the tenant id to bind the token to
     * @param label  the token's {@code service_tokens.label} value
     */
    public static void seedServiceToken(DSLContext dsl, String token, String tenant, String label) {
        dsl.insertInto(SERVICE_TOKENS)
            .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL)
            .values(TokenHashing.sha256Hex(token), tenant, label)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Seed one {@code nexus.service_tokens} row with {@code scope}/{@code
     * expires_at}/{@code revoked_at} set (nexus-cbo4a batch 9 item 1) — the overload
     * {@link #seedServiceToken(DSLContext, String, String, String)} does not cover,
     * for the sites that previously hand-rolled a wider column list via {@code
     * prepareStatement}/{@code createStatement().execute}. {@code scope}/{@code
     * expiresAt}/{@code revokedAt} are OMITTED from the insert (never set to a SQL
     * NULL) when the argument is {@code null} — {@code scope} carries a {@code NOT
     * NULL DEFAULT 'tenant'} CHECK constraint (service-tokens-003-scope-column.xml)
     * that a literal null would violate, and the two timestamp columns are simply
     * nullable-and-unset in that case, matching how a caller who never mentioned
     * them in a hand-rolled column list behaved.
     *
     * @param dsl       a {@link DSLContext} over the same connection/role the schema
     *                  was migrated under
     * @param token     the raw bearer token to hash and store
     * @param tenant    the tenant id to bind the token to
     * @param label     the token's {@code service_tokens.label} value
     * @param scope     {@code service_tokens.scope} ({@code root}/{@code tenant}/
     *                  {@code mint}/{@code data}), or {@code null} to take the
     *                  column default ({@code tenant})
     * @param expiresAt {@code service_tokens.expires_at}, or {@code null} to leave
     *                  it unset
     * @param revokedAt {@code service_tokens.revoked_at}, or {@code null} to leave
     *                  it unset
     */
    public static void seedServiceToken(DSLContext dsl, String token, String tenant, String label,
                                         String scope, java.time.OffsetDateTime expiresAt,
                                         java.time.OffsetDateTime revokedAt) {
        var step = dsl.insertInto(SERVICE_TOKENS)
            .set(SERVICE_TOKENS.TOKEN_HASH, TokenHashing.sha256Hex(token))
            .set(SERVICE_TOKENS.TENANT_ID, tenant)
            .set(SERVICE_TOKENS.LABEL, label);
        if (scope != null) {
            step = step.set(SERVICE_TOKENS.SCOPE, scope);
        }
        if (expiresAt != null) {
            step = step.set(SERVICE_TOKENS.EXPIRES_AT, expiresAt);
        }
        if (revokedAt != null) {
            step = step.set(SERVICE_TOKENS.REVOKED_AT, revokedAt);
        }
        step.onConflictDoNothing().execute();
    }

    /** The RDR-103 4-segment conformant name shape, {@code <ct>__<owner>__<model>__v<n>} —
     *  used only to derive constraint-satisfying attributes for {@link #insertCollection},
     *  never a general-purpose parser.
     *
     *  <p>RDR-204 Phase 2 (bead nexus-ft04v.16 fix): the owner group now allows {@code _}
     *  in addition to {@code [a-zA-Z0-9-]} — WIDER than {@code
     *  hygiene-002-collection-attributes-walk.xml}'s branch-A regex this used to match
     *  byte-for-byte. The production walk's stricter pattern governs REAL data and is out
     *  of this bead's scope; this helper only INFERS attributes for test-fixture
     *  convenience, and dozens of fixtures across the tree (this test's own {@code
     *  "code__afc_own1__voyage-code-3__v1"}-style dedicated names, deliberately
     *  underscored for readability/uniqueness — see e.g. {@code
     *  TaxonomyAssignFromChashesRepositoryTest}'s per-test "dedicated collection"
     *  comments) use an owner segment containing {@code _}. Under the pre-nexus-ft04v.16
     *  name-SEGMENT dispatch authority this mismatch was invisible ({@code
     *  collection.split("__")} tolerates an internal {@code _} fine, since it only
     *  splits on the literal {@code "__"} delimiter) — it surfaced only once dispatch
     *  moved to reading THIS ROW's {@code embedding_model}/{@code dimension}: a
     *  non-matching owner segment silently routed every such fixture into branch D
     *  (content_type {@code "unknown"}, embedding_model the {@code bge-base-en-v15-768}
     *  fallback), which resolved the WRONG dimension for every non-768 fixture and
     *  produced a silent "0 rows assigned/matched" everywhere the dispatched dim
     *  disagreed with the collection's actual stored dimension — never an exception,
     *  since the row still existed and still resolved to SOME real, FK-valid model.
     *
     *  <p>RDR-204 engine, bead nexus-ztafa (fix round, review finding 2c): the owner
     *  group is {@code [a-zA-Z0-9-]+(?:_[a-zA-Z0-9-]+)*}, NOT the simpler
     *  {@code [a-zA-Z0-9_-]+} — a single underscore remains legal only BETWEEN two
     *  alnum/hyphen tokens, and a bare {@code __} can never appear inside the owner
     *  itself, since that stays reserved, unconditionally, for the segment
     *  separator. The simpler grammar made a name like {@code
     *  "code__foo__bar__v9"} ambiguous between this 4-segment pattern (owner
     *  {@code "foo"}) and {@link #CONFORMANT_COLLECTION_NAME}'s 2-segment sibling
     *  below with owner {@code "foo__bar__v9"} — this tightened grammar removes
     *  that ambiguity structurally, matching {@code
     *  hygiene-004-owner-grammar-underscore.xml}'s own widened grammar exactly. */
    private static final Pattern CONFORMANT_COLLECTION_NAME = Pattern.compile(
        "^(code|docs|rdr|knowledge)__([a-zA-Z0-9-]+(?:_[a-zA-Z0-9-]+)*)__([a-z][a-z0-9-]*)__v[0-9]+$");

    /** The {@code quarantine-} prefixed sibling of {@link #CONFORMANT_COLLECTION_NAME}
     *  (hygiene-002-collection-attributes-walk.xml's branch B) — RDR-204 Phase 2 fix
     *  (bead nexus-ft04v.16): this branch did not exist before, so every
     *  quarantine-prefixed fixture name (e.g. {@code
     *  "quarantine-code__gcq-case4__voyage-code-3__v1"}) fell through to {@link
     *  #CONFORMANT_COLLECTION_NAME} (which never matches — the name does not START
     *  with {@code code|docs|rdr|knowledge}, {@code quarantine-} is prepended) and
     *  landed in the generic fallback (embedding_model {@code bge-base-en-v15-768},
     *  768-dim) regardless of what model the name's own suffix names. Invisible under
     *  the pre-nexus-ft04v.16 name-segment dispatch authority; surfaced as a silent
     *  wrong-dim dispatch (a 1024-dim embedder writing to a collection whose
     *  registered row says 768) once dispatch started reading this row.
     *
     *  <p>RDR-204 engine, bead nexus-ztafa (fix round): owner group tightened to
     *  {@code [a-zA-Z0-9-]+(?:_[a-zA-Z0-9-]+)*}, same rationale as {@link
     *  #CONFORMANT_COLLECTION_NAME}'s own javadoc above. */
    private static final Pattern CONFORMANT_QUARANTINE_COLLECTION_NAME = Pattern.compile(
        "^quarantine-(code|docs|rdr|knowledge)__([a-zA-Z0-9-]+(?:_[a-zA-Z0-9-]+)*)__([a-z][a-z0-9-]*)__v[0-9]+$");

    /**
     * Seed a minimal {@code nexus.catalog_collections} row via generated jOOQ DSL
     * (nexus-cbo4a batch 10) — replaces the identical hand-rolled {@code INSERT INTO
     * nexus.catalog_collections (tenant_id, name) VALUES (...) ON CONFLICT (tenant_id,
     * name) DO NOTHING} that {@code CollectionRegistryFkTest} and
     * {@code CollectionRegistryFkExtraTest} each built by string concatenation
     * (identical shape, duplicated across files — the same class of duplication
     * {@link #seedServiceToken(DSLContext, String, String, String)} closed for
     * {@code service_tokens}).
     *
     * <p><b>nexus-ft04v.4/.5 fix (critic T2 critique-nexus-ft04v-4-walk-changeset-b42549f03
     * [24941] Critical):</b> hygiene-002-collection-attributes-walk.xml adds a non-empty
     * CHECK on {@code content_type}/{@code owner_id}/{@code embedding_model}, an
     * {@code embedding_model} FK to {@code nexus.embedding_models}, and a NOT NULL +
     * enum CHECK on {@code lifecycle_state} -- the bare two-column insert this method
     * used to do (still {@code ''}/{@code ''}/{@code ''}/{@code NULL}) violates all four
     * on the shared, fully-migrated test cluster, and this is the ONE place ~39 call
     * sites across the tree share, so it is fixed here rather than at each site. When
     * {@code name} matches the RDR-103 conformant shape
     * ({@code <ct>__<owner>__<model>__v<n>}, {@code ct} one of code/docs/rdr/knowledge),
     * {@code content_type}/{@code owner_id} come from the name and {@code embedding_model}
     * is the name's token when it is a real row in {@code embedding_models}, else the
     * seeded local fallback {@code bge-base-en-v15-768} (same rule
     * hygiene-002-1's own walk uses) -- {@code lifecycle_state} is {@code 'live'}.
     * Otherwise: {@code content_type} {@code 'unknown'}, {@code owner_id} the tenant,
     * {@code embedding_model} the fallback, and {@code lifecycle_state} {@code
     * 'quarantine'} when {@code name} carries the {@code quarantine-} prefix, else
     * {@code 'live'}. {@code model_version}/{@code display_name} stay at their {@code ''}
     * default -- neither is constrained and no caller of this method has ever needed
     * them set.
     *
     * <p><b>Column-existence guard (found while landing the fix above):</b> this
     * method is also called against databases migrated only PART WAY through the
     * changelog, well before catalog-036-4 even adds {@code dimension}/{@code
     * lifecycle_state} (e.g. {@code SchemaMigratorIntegrationTest}'s aged-fleet
     * scenarios, which stop at an early changeset like {@code catalog-013-0} on
     * purpose to reproduce a pre-constraint production timeline). Referencing
     * {@code CATALOG_COLLECTIONS.LIFECYCLE_STATE} unconditionally breaks those --
     * jOOQ's generated field exists at COMPILE time regardless of what the TARGET
     * database has actually walked to, so the INSERT fails with "column
     * lifecycle_state does not exist" there. {@link PgCatalogProbes#columnExists}
     * checks live, on the SAME connection, whether {@code lifecycle_state} exists
     * yet; when it does not, this method falls back to the original bare
     * {@code (tenant_id, name)} insert, preserving every early-migration caller's
     * prior behavior exactly.
     *
     * @param dsl      a {@link DSLContext} over the same connection/role the schema
     *                 was migrated under (e.g. {@code DSL.using(su, SQLDialect.POSTGRES)})
     * @param tenantId the tenant id to register the collection under
     * @param name     the collection name ({@code catalog_collections.name})
     */
    public static void insertCollection(DSLContext dsl, String tenantId, String name) {
        // nexus-cbo4a fix (ManifestCollectionStampTest's "null collection is rejected
        // the same way" case): catalog_collections.name is NOT NULL, so there is no
        // sensible row to seed for a null name -- a no-op here. Before this guard, a
        // null name reached the startsWith("quarantine-") check below and NPE'd,
        // masking the writer-under-test's own IllegalArgumentException contract
        // (requireNonBlank) behind an unrelated seeding-helper crash. Blank ("") is
        // deliberately NOT included here: an empty NAME is a valid, if unusual,
        // value that existing callers (e.g. this file's own blank-collection sibling
        // test) rely on actually registering a row under, same as any other name.
        if (name == null) {
            return;
        }
        if (!PgCatalogProbes.columnExists(dsl, "nexus", "catalog_collections", "lifecycle_state")) {
            dsl.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                .values(tenantId, name)
                .onConflictDoNothing()
                .execute();
            return;
        }
        // RDR-225: a collection is registered so that chunks can be written under it, and a chunk of a
        // tenant with no partition leaf is refused by tuple routing. Production creates the leaves with
        // the tenant's first service token; a test that invents a tenant id never issues one, so the
        // registration it already has to do stands in for it.
        ensureTenantPartitions(dsl, tenantId);

        String contentType = "unknown";
        String ownerId = tenantId;
        String embeddingModel = "bge-base-en-v15-768";
        String lifecycleState = name.startsWith("quarantine-") ? "quarantine" : "live";

        Matcher mq = CONFORMANT_QUARANTINE_COLLECTION_NAME.matcher(name);
        Matcher m = CONFORMANT_COLLECTION_NAME.matcher(name);
        if (mq.matches()) {
            contentType = mq.group(1);
            ownerId = mq.group(2);
            String token = mq.group(3);
            boolean modelKnown = dsl.fetchExists(dsl.selectOne().from(EMBEDDING_MODELS)
                .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(token)));
            embeddingModel = modelKnown ? token : "bge-base-en-v15-768";
            lifecycleState = "quarantine";
        } else if (m.matches()) {
            contentType = m.group(1);
            ownerId = m.group(2);
            String token = m.group(3);
            boolean modelKnown = dsl.fetchExists(dsl.selectOne().from(EMBEDDING_MODELS)
                .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(token)));
            embeddingModel = modelKnown ? token : "bge-base-en-v15-768";
            lifecycleState = "live";
        }

        dsl.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(tenantId, name, contentType, ownerId, embeddingModel, lifecycleState)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * Create {@code tenant}'s partition leaves under every model partition of {@code nexus.chunks} and
     * {@code nexus.taxonomy_centroids} (RDR-225, nexus-3wh8d.13), as the {@code service_tokens} trigger
     * does for a tenant's first token. Idempotent (the function looks leaves up by bound), and a no-op on
     * a schema that has not been walked to the partitioned layout yet, so the pre-walk dedicated
     * containers can call the seeds that call this. Runs as whatever role {@code ctx} carries, which must
     * be allowed to EXECUTE the function (the superuser the schema was migrated under).
     */
    public static void ensureTenantPartitions(DSLContext ctx, String tenant) {
        if (!PgCatalogProbes.columnExists(ctx, "nexus", "chunks", "embedding_model")
                || !PgCatalogProbes.routineExists(ctx, "nexus", "create_tenant_partitions")) {
            return;
        }
        PartitionScratch.createTenantPartitions(ctx, "chunks", tenant, true);
        PartitionScratch.createTenantPartitions(ctx, "taxonomy_centroids", tenant, true);
    }

    /**
     * True when {@code nexus.chunks} already has {@code embedding_model} (the schema has been walked past
     * {@code vectors-030-1}). The chunk and manifest seeds below serve two kinds of caller: tests on a fully
     * migrated schema, where every row must carry its collection's model, and the pre-walk containers
     * (the Hygiene walks, the upgrade rehearsal, the migration-walk test), which seed the OLD layout before
     * the walk and cannot name a column that does not exist yet.
     */
    public static boolean isPartitionedLayout(DSLContext ctx) {
        return PgCatalogProbes.columnExists(ctx, "nexus", "chunks", "embedding_model");
    }

    /**
     * {@link #insertCollection(DSLContext, String, String)} registered under an explicit embedding
     * model (RDR-225, nexus-3wh8d.13). A chunk carries its collection's model and a vector's width must
     * be that model's dimension, so a seed that writes 384- or 1024-wide vectors into a collection whose
     * name does not carry the right model token registers it here. The model is applied to the freshly
     * inserted row, which no chunk references yet, so the composite foreign key does not object; call
     * it BEFORE the first chunk of the collection.
     */
    public static void insertCollection(DSLContext dsl, String tenantId, String name, String embeddingModel) {
        insertCollection(dsl, tenantId, name);
        dsl.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, embeddingModel)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenantId))
            .and(CATALOG_COLLECTIONS.NAME.eq(name))
            .execute();
    }

    /**
     * Seed a minimal {@code nexus.catalog_documents} row via generated jOOQ DSL
     * (nexus-cbo4a batch 10) — the {@code catalog_documents} counterpart to {@link
     * #insertCollection}, replacing the identical hand-rolled {@code INSERT INTO
     * nexus.catalog_documents (tenant_id, tumbler, title) VALUES (...) ON CONFLICT
     * (tenant_id, tumbler) DO NOTHING} duplicated the same way. {@code
     * catalog_documents} PK is {@code (tenant_id, tumbler)}; {@code title} is
     * required NOT NULL and is synthesized as {@code "Test Doc " + tumbler}, matching
     * every hand-rolled call site's own literal exactly.
     *
     * @param dsl      a {@link DSLContext} over the same connection/role the schema
     *                 was migrated under
     * @param tenantId the tenant id to register the document under
     * @param tumbler  the document's {@code catalog_documents.tumbler}
     */
    public static void insertCatalogDocument(DSLContext dsl, String tenantId, String tumbler) {
        dsl.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE)
            .values(tenantId, tumbler, "Test Doc " + tumbler)
            .onConflictDoNothing()
            .execute();
    }

    /**
     * The embedding model {@code collection} is registered under, for the seeds below (RDR-225,
     * nexus-3wh8d.13): {@code nexus.chunks} and every table that references it carry
     * {@code embedding_model NOT NULL}, taken from the collection's registry row, and a chunk's
     * vector width must be that model's dimension (the model partition's CHECK). A seed that names a
     * collection with no row fails here, naming the missing registration, instead of inserting a chunk
     * the composite foreign key would refuse with a less direct message.
     *
     * @throws IllegalStateException when {@code (tenant, collection)} has no {@code catalog_collections} row
     */
    public static String collectionModel(DSLContext ctx, String tenant, String collection) {
        String model = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL);
        if (model == null) {
            throw new IllegalStateException("collection '" + collection + "' is not registered for tenant '"
                + tenant + "'; call PgContainerHelper.insertCollection first (a chunk carries its collection's model)");
        }
        return model;
    }

    /**
     * Give {@code chashHex} a live owner in {@code collection} (RDR-192 Step 5,
     * bead nexus-wbfpw.10): since vectors-019 every read-visibility predicate
     * requires {@code EXISTS (SELECT 1 FROM nexus.chunk_live_owners(tenant, collection,
     * chash))}, a chunk with no manifest row at all is no longer visible to search,
     * hybrid search, get/getWhere/getEmbeddings/getAllMetadata/list, or counted by
     * {@code nexus.live_chunks}/{@code nexus.collection_vector_stats} — a test fixture
     * that writes a chunk and expects it to be readable back must also give it an
     * owner. Inserts one LIVE {@code catalog_documents} row (deterministic tumbler
     * {@code "own-" + collection}, idempotent) and one {@code catalog_document_chunks}
     * manifest row per {@code chashHex} entry, appended after whatever positions that
     * document already owns (a repeat call against the same collection APPENDS rather
     * than colliding on the {@code (tenant_id, doc_id, position)} PK — callers that
     * write chunks to the same collection across several calls can call this again
     * each time). The chunk rows named by {@code chashHex} must already exist
     * ({@code fk_catalog_chunks_chunk}).
     *
     * @param ctx        a {@link DSLContext} over the same connection/role the schema
     *                   was migrated under (superuser or any role with INSERT on the
     *                   two catalog tables)
     * @param tenantId   the tenant id
     * @param collection the collection the chunks live in; also the owning document's
     *                   {@code physical_collection}
     * @param chashHex   the 64-lowercase-hex chashes to grant a live owner, in the
     *                   order they should occupy successive manifest positions
     */
    public static void ownChunks(DSLContext ctx, String tenantId, String collection, String... chashHex) {
        String docId = "own-" + collection;
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .values(tenantId, docId, "Owner " + docId, collection)
            .onConflictDoNothing()
            .execute();

        Integer maxPosition = ctx.select(org.jooq.impl.DSL.max(CATALOG_DOCUMENT_CHUNKS.POSITION))
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenantId))
            .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
            .fetchOne(0, Integer.class);
        int nextPosition = (maxPosition == null) ? 0 : maxPosition + 1;
        if (chashHex.length == 0) {
            return;
        }

        // Single multi-row INSERT (one round trip regardless of chashHex.length) —
        // a fixture owning thousands of chashes (e.g. a dense-gate-scale fixture)
        // must not pay one round trip per chash.
        if (!isPartitionedLayout(ctx)) {
            var old = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                    CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                    CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .values(tenantId, docId, nextPosition,
                        dev.nexus.service.db.Chash.fromHex(chashHex[0]).toBytes(), collection);
            for (int i = 1; i < chashHex.length; i++) {
                old = old.values(tenantId, docId, nextPosition + i,
                        dev.nexus.service.db.Chash.fromHex(chashHex[i]).toBytes(), collection);
            }
            old.onConflictDoNothing().execute();
            return;
        }
        String model = collectionModel(ctx, tenantId, collection);
        var step = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
            .values(tenantId, docId, nextPosition,
                    dev.nexus.service.db.Chash.fromHex(chashHex[0]).toBytes(), collection, model);
        for (int i = 1; i < chashHex.length; i++) {
            step = step.values(tenantId, docId, nextPosition + i,
                    dev.nexus.service.db.Chash.fromHex(chashHex[i]).toBytes(), collection, model);
        }
        step.onConflictDoNothing().execute();
    }

    /**
     * Seed one {@code nexus.chunks} row at dim 384 via generated jOOQ DSL (nexus-cbo4a
     * batch 10) — hoisted from the byte-for-byte identical {@code insertChunk384}
     * duplicated in {@code CatalogDeleteCollectionCascadeTest} and {@code
     * CatalogRenameCollectionTest} (batch-4's own critique named this exact
     * duplication class for the seed-insert shape). {@code chashBytes} is the raw
     * {@code chash} column value (callers control the 32-byte-vs-64-hex-decoded
     * distinction; see {@code chashBytes}/{@code hexChashBytes} helper pairs in the
     * callers), {@code chunk_text} is fixed at {@code "text"} — every existing call
     * site's own literal — since no caller has ever needed a different value.
     *
     * @param ctx        a {@link DSLContext} over the same connection/role the schema
     *                   was migrated under
     * @param tenant     the tenant id
     * @param collection the collection name
     * @param chashBytes the raw {@code chash} bytea value
     * @param v          the 384-dim embedding vector
     */
    public static void insertChunk384(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        if (!isPartitionedLayout(ctx)) {
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                           CHUNKS.EMBEDDING_384)
               .values(tenant, collection, chashBytes, "text", v)
               .execute();
            return;
        }
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                       CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
           .values(tenant, collection, chashBytes, collectionModel(ctx, tenant, collection), "text", v)
           .execute();
    }

    /** {@code nexus.chunks} seed at dim 768 — see {@link #insertChunk384} for the full contract. */
    public static void insertChunk768(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        if (!isPartitionedLayout(ctx)) {
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                           CHUNKS.EMBEDDING_768)
               .values(tenant, collection, chashBytes, "text", v)
               .execute();
            return;
        }
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                       CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_768)
           .values(tenant, collection, chashBytes, collectionModel(ctx, tenant, collection), "text", v)
           .execute();
    }

    /** {@code nexus.chunks} seed at dim 1024 — see {@link #insertChunk384} for the full contract. */
    public static void insertChunk1024(DSLContext ctx, String tenant, String collection, byte[] chashBytes, Vector v) {
        if (!isPartitionedLayout(ctx)) {
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                           CHUNKS.EMBEDDING_1024)
               .values(tenant, collection, chashBytes, "text", v)
               .execute();
            return;
        }
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                       CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_1024)
           .values(tenant, collection, chashBytes, collectionModel(ctx, tenant, collection), "text", v)
           .execute();
    }

    private static final com.fasterxml.jackson.databind.ObjectMapper CHUNK_METADATA_MAPPER =
        new com.fasterxml.jackson.databind.ObjectMapper();

    /**
     * Seed {@code nexus.chunks} rows with NO manifest row, through generated jOOQ DSL
     * rather than the write routes (RDR-223 P3.1, nexus-z0o2p.23). From Phase 3 the
     * engine refuses an ownerless write on {@code /v1/vectors/upsert-chunks}
     * (and the retired {@code /store-put} and {@code /upsert-reference-only} routes are gone), so a test that needs a
     * chunk before its owner exists (the manifest FK wants the chunk first) or an
     * orphan on purpose builds it here. The Python twin is {@code tests/_chunk_seed.py}.
     *
     * <p>Unlike {@link #insertChunk384} this carries the text, the metadata and the
     * caller's real vectors, so it can stand in for an {@code upsert-chunks} POST for a
     * FIRST write. It is deliberately looser than the Python twin
     * ({@code tests/_chunk_seed.py}), not the same contract:
     * <ul>
     *   <li>it runs as whatever role {@code ctx} carries (the superuser in the existing
     *       callers, so RLS does not apply) and sets no tenant GUC;</li>
     *   <li>a repeat of an existing {@code (tenant, collection, chash)} is left alone
     *       ({@code ON CONFLICT DO NOTHING}): no text or vector replace, no metadata merge,
     *       no {@code retention} reset, no {@code last_written_at} restamp, where the route
     *       and the Python twin all do those;</li>
     *   <li>the embedding column follows the vector's width (384, 768 or 1024), and the row carries
     *       the collection's registered model (RDR-225), so a width that is not that model's
     *       dimension is refused by the partition's CHECK, as the engine's writers refuse it.</li>
     * </ul>
     * The collection must already be registered ({@link #insertCollection}); the caller
     * pairs this with {@link #ownChunks} when the test wants the chunks live.
     *
     * @param ctx        a {@link DSLContext} over a role that may INSERT into {@code nexus.chunks}
     *                   for {@code tenant} (the superuser used by the other seeds here, or the
     *                   service role inside a {@code TenantScope.withTenant})
     * @param chashHex   64-lowercase-hex chashes, one per chunk
     * @param texts      chunk texts, aligned with {@code chashHex}
     * @param embeddings vectors, aligned with {@code chashHex}, all one width
     * @param metadatas  per-chunk metadata maps, aligned with {@code chashHex}
     */
    public static void insertChunks(DSLContext ctx, String tenant, String collection,
                                    java.util.List<String> chashHex, java.util.List<String> texts,
                                    java.util.List<float[]> embeddings,
                                    java.util.List<Map<String, Object>> metadatas) {
        int n = chashHex.size();
        if (texts.size() != n || embeddings.size() != n || metadatas.size() != n) {
            throw new IllegalArgumentException("insertChunks: ids/texts/embeddings/metadatas must align ("
                + n + "/" + texts.size() + "/" + embeddings.size() + "/" + metadatas.size() + ")");
        }
        // RDR-225: every row of the call carries the collection's model; a vector whose width is not
        // that model's dimension is refused by the model partition's CHECK, as the engine's writers are.
        final String model = isPartitionedLayout(ctx) ? collectionModel(ctx, tenant, collection) : null;
        for (int i = 0; i < n; i++) {
            byte[] chash = dev.nexus.service.db.Chash.fromHex(chashHex.get(i)).toBytes();
            float[] v = embeddings.get(i);
            org.jooq.JSONB meta;
            try {
                meta = org.jooq.JSONB.jsonb(CHUNK_METADATA_MAPPER.writeValueAsString(metadatas.get(i)));
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                throw new IllegalArgumentException("insertChunks: metadata is not JSON-serialisable", e);
            }
            var col = switch (v.length) {
                case 384 -> CHUNKS.EMBEDDING_384;
                case 768 -> CHUNKS.EMBEDDING_768;
                case 1024 -> CHUNKS.EMBEDDING_1024;
                default -> throw new IllegalArgumentException("insertChunks: no embedding column of width " + v.length);
            };
            if (model == null) {
                ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                               col, CHUNKS.METADATA)
                   .values(tenant, collection, chash, texts.get(i), Vector.of(v), meta)
                   .onConflictDoNothing()
                   .execute();
                continue;
            }
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                           CHUNKS.CHUNK_TEXT, col, CHUNKS.METADATA)
               .values(tenant, collection, chash, model, texts.get(i), Vector.of(v), meta)
               .onConflictDoNothing()
               .execute();
        }
    }

    /**
     * {@link #insertChunks} with the vectors taken from {@code embedder}, the way the
     * {@code upsert-chunks} route computes them server-side.
     */
    public static void insertChunks(DSLContext ctx, String tenant, String collection,
                                    java.util.List<String> chashHex, java.util.List<String> texts,
                                    java.util.List<Map<String, Object>> metadatas,
                                    dev.nexus.service.vectors.Embedder embedder) {
        insertChunks(ctx, tenant, collection, chashHex, texts, embedder.embed(texts), metadatas);
    }

    /**
     * Seed OWNED chunks for a test that POSTs to {@code /v1/vectors/upsert-chunks} to exercise
     * something other than an ownerless first write (RDR-223 P3.1, nexus-z0o2p.23). From Phase 3
     * the engine refuses that route for a chash with no live manifest row, so such a test
     * pre-seeds the chash with an owner and POSTs with {@code force_re_embed} (an owned chash
     * with a stored vector skips the embedder otherwise, and the embedder is what these tests
     * reach for).
     *
     * <p>Each chunk gets a zero vector of {@code dim} (the width the collection's registered
     * model routes to) and the text {@code "seed"}; {@link #insertChunks} and {@link #ownChunks}
     * do the rest, so it runs as whatever role {@code ctx} carries (the superuser, in the callers)
     * and the collection must already be registered ({@link #insertCollection}).
     */
    public static void insertOwnedChunks(DSLContext ctx, String tenant, String collection, int dim,
                                         String... chashHex) {
        var hex = java.util.List.of(chashHex);
        insertChunks(ctx, tenant, collection, hex,
            hex.stream().map(h -> "seed").toList(),
            hex.stream().map(h -> new float[dim]).toList(),
            hex.stream().map(h -> Map.<String, Object>of()).toList());
        ownChunks(ctx, tenant, collection, chashHex);
    }

    /**
     * Seed one REFERENCE-ONLY {@code nexus.chunks} row ({@code chunk_text} NULL,
     * {@code retention='reference-only'}, RDR-169 G4) with NO manifest row. This is the only
     * writer of such a row in the tree: the engine's own writer
     * ({@code PgVectorRepository#upsertReferenceOnlyChunk}, and the route over it) is gone
     * (RDR-223 Phase 3, nexus-z0o2p.36), so a test that needs a reference-only row builds it
     * here, the way {@link #insertChunks} builds a content row.
     *
     * <p>A repeat of the same {@code (tenant, collection, chash)} refreshes the embedding and
     * REPLACES the metadata, and never touches {@code chunk_text} (so a row that was full stays
     * full; the guard against a full to reference-only change belonged to the removed writer, not
     * to the schema). It runs as whatever role {@code ctx} carries (the superuser, in the
     * callers), sets no tenant GUC, and the collection must already be registered
     * ({@link #insertCollection}).
     *
     * @param chashHex  the 64-lowercase-hex chash
     * @param embedding the vector; its width (384, 768 or 1024) picks the embedding column
     * @param metadata  chunk metadata, may be empty
     */
    public static void insertReferenceOnlyChunk(DSLContext ctx, String tenant, String collection,
                                                String chashHex, float[] embedding,
                                                Map<String, Object> metadata) {
        org.jooq.JSONB meta;
        try {
            meta = org.jooq.JSONB.jsonb(CHUNK_METADATA_MAPPER.writeValueAsString(metadata));
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalArgumentException("insertReferenceOnlyChunk: metadata is not JSON-serialisable", e);
        }
        var col = switch (embedding.length) {
            case 384 -> CHUNKS.EMBEDDING_384;
            case 768 -> CHUNKS.EMBEDDING_768;
            case 1024 -> CHUNKS.EMBEDDING_1024;
            default -> throw new IllegalArgumentException(
                "insertReferenceOnlyChunk: no embedding column of width " + embedding.length);
        };
        byte[] chash = dev.nexus.service.db.Chash.fromHex(chashHex).toBytes();
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                       CHUNKS.CHUNK_TEXT, col, CHUNKS.METADATA, CHUNKS.RETENTION)
           .values(tenant, collection, chash, collectionModel(ctx, tenant, collection), null,
                   Vector.of(embedding), meta, "reference-only")
           .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL)
           .doUpdate()
           .set(col, org.jooq.impl.DSL.excluded(col))
           .set(CHUNKS.METADATA, org.jooq.impl.DSL.excluded(CHUNKS.METADATA))
           .execute();
    }

    /**
     * Allowlist of GUC names {@link #setTenant} may stamp — the same two names {@link
     * TenantScope#PERMITTED_GUCS} enforces (that field is package-private inside {@code
     * dev.nexus.service.db}, unreachable from this package, so this is a second copy of
     * the same guard rather than a shared reference; drift between the two would be a
     * silent RLS-context miss either way, and both are derived from {@link
     * TenantScope#DEFAULT_TENANT_GUC}/{@link TenantScope#T1_TENANT_GUC} so a future third
     * GUC needs a coordinated edit in both places, not a lone one here).
     */
    private static final Set<String> TENANT_GUC_ALLOWLIST =
        Set.of(TenantScope.DEFAULT_TENANT_GUC, TenantScope.T1_TENANT_GUC);

    /**
     * Stamp {@code gucName} on an existing, test-owned {@link Connection} via jOOQ's typed
     * {@code set_config(...)} function call (nexus-cbo4a batch 2) — the test-tree counterpart
     * to {@link TenantScope#withTenant}, for call sites that hold a raw {@link Connection}
     * they already own (bootstrap superuser connections, multi-connection cross-tenant
     * isolation probes, {@code RESET}-then-reassert sequences) and need to stamp or clear a
     * tenant GUC on it directly, with no lambda-scoped connection lifecycle. {@link
     * TenantScope#withTenant} does not fit that shape at all — it BORROWS its own connection
     * from a {@link javax.sql.DataSource} and commits/closes it before returning, whereas every
     * caller here already has the connection open and keeps driving it afterward.
     *
     * <p>Replaces the raw {@code SET nexus.tenant = '...'} / {@code SET LOCAL nexus.tenant =
     * '...'} / {@code SELECT set_config('nexus.tenant', ..., ...)} / {@code RESET nexus.tenant}
     * string literals these call sites used to build by hand (Sam's no-raw-SQL-strings-in-Java
     * directive, nexus-zrcj7).
     *
     * @param conn    the connection to stamp; its transaction/autocommit state is left exactly
     *                as the caller set it — this method neither opens nor commits a transaction
     * @param gucName the GUC name, restricted to {@link #TENANT_GUC_ALLOWLIST} — the same
     *                defense-in-depth guard {@link TenantScope#withTenant} itself enforces, so
     *                this helper cannot become a second, unguarded path to an arbitrary session
     *                GUC
     * @param tenant  the tenant value to set, or {@code null} to RESET the GUC to its default
     *                (Postgres: {@code set_config(name, NULL, is_local)} performs exactly the
     *                {@code RESET name} operation — see the {@code set_config} documentation)
     * @param isLocal {@code true} for {@code SET LOCAL} (transaction-scoped — {@code conn} must
     *                have an open, not-yet-committed transaction, i.e. {@code autoCommit=false});
     *                {@code false} for session-scoped {@code SET}/{@code RESET}
     */
    public static void setTenant(Connection conn, String gucName, String tenant, boolean isLocal) {
        if (!TENANT_GUC_ALLOWLIST.contains(gucName)) {
            throw new IllegalArgumentException(
                "gucName not permitted: " + gucName + " (allowed: " + TENANT_GUC_ALLOWLIST + ")");
        }
        DSL.using(conn, SQLDialect.POSTGRES)
            .select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(gucName), DSL.val(tenant, SQLDataType.VARCHAR), DSL.val(isLocal)))
            .fetch();
    }

    /**
     * The planner settings plan-shape tests pin, the only names {@link #setPlannerGuc} and
     * {@link #setRolePlannerGuc} accept.
     */
    private static final Set<String> PLANNER_GUC_ALLOWLIST = Set.of(
        "enable_seqscan", "enable_indexscan", "enable_bitmapscan", "enable_sort",
        "enable_hashjoin", "enable_mergejoin", "enable_nestloop", "hnsw.ef_search");

    /**
     * {@code SET [LOCAL] guc = value} for a planner setting, through {@code set_config} with bound
     * arguments instead of a statement string (nexus-cbo4a). {@code isLocal=false} is the
     * session-level {@code SET}; {@code isLocal=true} is {@code SET LOCAL} and ends with the
     * transaction.
     *
     * @param conn    connection whose session the setting applies to
     * @param guc     a name from {@link #PLANNER_GUC_ALLOWLIST}
     * @param value   the setting's text value, e.g. {@code "off"} or {@code "40"}
     * @param isLocal whether the setting is transaction-scoped
     */
    public static void setPlannerGuc(Connection conn, String guc, String value, boolean isLocal) {
        if (!PLANNER_GUC_ALLOWLIST.contains(guc)) {
            throw new IllegalArgumentException(
                "planner guc not permitted: " + guc + " (allowed: " + PLANNER_GUC_ALLOWLIST + ")");
        }
        DSL.using(conn, SQLDialect.POSTGRES)
            .select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(guc), DSL.val(value, SQLDataType.VARCHAR), DSL.val(isLocal)))
            .fetch();
    }

    /**
     * {@code ALTER ROLE role SET guc = value} for a planner setting (nexus-cbo4a batch 9), through
     * {@code db.changelog-test-role-planner-guc.xml}: jOOQ has no ALTER ROLE. The role goes through
     * {@link #requireTestIdentifier}, the setting through {@link #PLANNER_GUC_ALLOWLIST} and the value must be
     * {@code on}, {@code off} or digits, so nothing else reaches the changelog's text. A role-level setting reaches
     * only connections opened after this call. Never a {@code search_path} (not on the allowlist).
     */
    public static void setRolePlannerGuc(Connection su, String role, String guc, String value) throws Exception {
        requireTestIdentifier(role);
        if (!PLANNER_GUC_ALLOWLIST.contains(guc)) {
            throw new IllegalArgumentException(
                "planner guc not permitted: " + guc + " (allowed: " + PLANNER_GUC_ALLOWLIST + ")");
        }
        if (value == null || !value.matches("on|off|[0-9]+")) {
            throw new IllegalArgumentException("not a plain planner setting value: " + value);
        }
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-role-planner-guc.xml",
            "databasechangelog_test_role_planner_guc", Map.of("role", role, "guc", guc, "value", value));
    }

    /**
     * {@code REINDEX INDEX schema.index} (nexus-cbo4a batch 9), through {@code db.changelog-test-reindex-index.xml}:
     * jOOQ has no REINDEX. Both name parts go through {@link #requireTestIdentifier}. The changeset runs with no
     * enclosing transaction (REINDEX of a partitioned index refuses one) on {@code su} itself, so session settings
     * made on {@code su} first (a {@code maintenance_work_mem} or {@code max_parallel_maintenance_workers} pin
     * through {@code ctx.set}) still apply to it. Leaves {@code su} autoCommit.
     */
    public static void reindexIndex(Connection su, String schema, String index) throws Exception {
        requireTestIdentifier(schema);
        requireTestIdentifier(index);
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-reindex-index.xml",
            "databasechangelog_test_reindex_index", Map.of("schema", schema, "index", index));
    }

    /** Which text of a product changeset {@link #replayPart} runs. */
    private enum ChangesetPart { FORWARD, ROLLBACK }

    /**
     * Runs one product changeset's own forward SQL, read from the changelog XML on the classpath, on {@code conn}
     * as that connection's current role, and returns the driver's {@code SQLWarning} messages (the
     * {@code RAISE NOTICE} chain) in order (nexus-cbo4a batch 10). See {@link #replayPart} for the contract.
     */
    public static List<String> replayChangeset(Connection conn, String changelog, String changesetId)
            throws Exception {
        return replayPart(conn, changelog, changesetId, ChangesetPart.FORWARD, java.util.function.UnaryOperator.identity());
    }

    /**
     * {@link #replayChangeset} with every occurrence of {@code from} in the forward text replaced by {@code to}
     * first, for a test that replays an already-shipped changeset against a later schema (a column whose type has
     * changed since it ran). {@code from} must occur in the text or this throws {@link IllegalStateException}, so
     * an edit of the shipped text fails the test instead of replaying it unsubstituted.
     */
    public static List<String> replayChangesetSubstituting(Connection conn, String changelog, String changesetId,
            String from, String to) throws Exception {
        return replayPart(conn, changelog, changesetId, ChangesetPart.FORWARD, text -> {
            if (from.isEmpty() || !text.contains(from)) {
                throw new IllegalStateException("changeset " + changesetId + " in " + changelog
                    + " no longer contains the substituted text: " + from);
            }
            return text.replace(from, to);
        });
    }

    /** The forward text {@link #replayChangeset} would run, for a test that pins which statement it replays. */
    public static String changesetForwardText(String changelog, String changesetId) throws Exception {
        return changesetText(changelog, changesetId, ChangesetPart.FORWARD);
    }

    /** The {@code <rollback>} text {@link #replayChangesetRollback} would run. */
    public static String changesetRollbackText(String changelog, String changesetId) throws Exception {
        return changesetText(changelog, changesetId, ChangesetPart.ROLLBACK);
    }

    /** {@link #replayChangeset} for the changeset's {@code <rollback>} text instead of its forward SQL. */
    public static List<String> replayChangesetRollback(Connection conn, String changelog, String changesetId)
            throws Exception {
        return replayPart(conn, changelog, changesetId, ChangesetPart.ROLLBACK, java.util.function.UnaryOperator.identity());
    }

    /**
     * The one sanctioned raw execute for the changeset-replay idiom (RawSqlGateTest's SANCTIONED_STATEMENTS): there
     * is no SQL string in Java, the statement IS the product text. A Liquibase run of the file cannot stand in: it
     * applies the changeset's siblings too, runs under the migration role, and its executor drops the notices. An
     * error from the server surfaces unchanged ({@code SQLException}, SQLSTATE and message as the driver raised
     * them); the connection's autoCommit, transaction and role are left as the caller set them.
     *
     * <p>FORWARD is the concatenation, in document order, of the changeset's DIRECT {@code <sql>} children;
     * ROLLBACK is the direct {@code <sql>} children of its {@code <rollback>}, or that element's own text when it
     * has none. Nested descendants are not followed, so a forward body never picks up the rollback's {@code <sql>}.
     * A changeset that is not found, or whose selected part is empty (an empty {@code <rollback/>}), throws
     * {@link IllegalStateException}: a replay of nothing would pass vacuously. XML entities are decoded, as
     * Liquibase decodes them.
     *
     * <p>The replay is NOT a Liquibase run: it does not honour {@code splitStatements}, {@code stripComments},
     * {@code preConditions} or {@code runAlways}. The text goes to the server as ONE simple-query string, and a
     * test that replays a changeset with preconditions stages the state they check itself. A changeset or
     * {@code <sql>} carrying {@code dbms} or {@code endDelimiter} is refused, since either changes what Liquibase
     * would send. See {@link #changesetText} for the other refusals (non-product changelogs, mixed or
     * non-{@code <sql>} rollback content). {@code edit} rewrites the text before it is sent (identity except for
     * {@link #replayChangesetSubstituting}).
     */
    private static List<String> replayPart(Connection conn, String changelog, String changesetId,
            ChangesetPart part, java.util.function.UnaryOperator<String> edit) throws Exception {
        String sql = edit.apply(changesetText(changelog, changesetId, part));
        List<String> notices = new ArrayList<>();
        try (java.sql.Statement st = conn.createStatement()) {
            // SANCTIONED RAW (nexus-cbo4a): product changeset text, run verbatim for its NOTICE chain, role and isolation from siblings
            st.execute(sql);
            for (java.sql.SQLWarning w = st.getWarnings(); w != null; w = w.getNextWarning()) {
                notices.add(w.getMessage());
            }
        }
        return notices;
    }

    private static final String LIQUIBASE_NS = "http://www.liquibase.org/xml/ns/dbchangelog";

    private static final String PRODUCT_CHANGELOG_PREFIX = "db/changelog/";

    /**
     * The text {@link #replayPart} runs, with its refusals. {@code changelog} must start with
     * {@code db/changelog/} (no {@code ..} segment) and its resolved resource must NOT come from the test classes
     * output: a test resource cannot shadow a product file, and a test changelog (db/changelog-test) never runs
     * through this helper. ROLLBACK throws {@link IllegalStateException} for a {@code <rollback>} that has element
     * children other than {@code <sql>}, or has both {@code <sql>} children and non-blank loose text; several
     * {@code <rollback>} elements are joined with a newline.
     */
    private static String changesetText(String changelog, String changesetId, ChangesetPart part) throws Exception {
        if (!changelog.startsWith(PRODUCT_CHANGELOG_PREFIX) || changelog.contains("..")) {
            throw new IllegalStateException("changeset replay is for PRODUCT changelogs under "
                + PRODUCT_CHANGELOG_PREFIX + ", got: " + changelog);
        }
        java.net.URL url = PgContainerHelper.class.getClassLoader().getResource(changelog);
        if (url == null) {
            throw new IllegalStateException("changelog not found on classpath: " + changelog);
        }
        if (url.toString().contains("/test-classes/")) {
            throw new IllegalStateException("changelog " + changelog + " resolves into the test classes output ("
                + url + "); a test resource must not shadow a product changelog");
        }
        org.w3c.dom.Document doc;
        try (var in = url.openStream()) {
            var factory = javax.xml.parsers.DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            doc = factory.newDocumentBuilder().parse(in);
        }
        org.w3c.dom.NodeList changeSets = doc.getElementsByTagNameNS(LIQUIBASE_NS, "changeSet");
        for (int i = 0; i < changeSets.getLength(); i++) {
            var cs = (org.w3c.dom.Element) changeSets.item(i);
            if (!changesetId.equals(cs.getAttribute("id"))) {
                continue;
            }
            refuseUnreplayable(cs, changelog);
            String text = switch (part) {
                case FORWARD -> directSql(cs);
                case ROLLBACK -> {
                    StringBuilder sb = new StringBuilder();
                    for (var n = cs.getFirstChild(); n != null; n = n.getNextSibling()) {
                        if (n instanceof org.w3c.dom.Element el && "rollback".equals(el.getLocalName())) {
                            if (sb.length() > 0) {
                                sb.append('\n');
                            }
                            sb.append(rollbackText(el, changesetId, changelog));
                        }
                    }
                    yield sb.toString();
                }
            };
            if (text.isBlank()) {
                throw new IllegalStateException("changeset " + changesetId + " in " + changelog + " has no "
                    + part + " SQL to replay");
            }
            return text;
        }
        throw new IllegalStateException("changeset not found: " + changesetId + " in " + changelog);
    }

    /** One {@code <rollback>}: its direct {@code <sql>} children, or its own text; never a mix, never other tags. */
    private static String rollbackText(org.w3c.dom.Element rollback, String changesetId, String changelog) {
        boolean hasSql = false;
        StringBuilder loose = new StringBuilder();
        for (var n = rollback.getFirstChild(); n != null; n = n.getNextSibling()) {
            if (n instanceof org.w3c.dom.Element el) {
                if (!"sql".equals(el.getLocalName())) {
                    throw new IllegalStateException("changeset " + changesetId + " in " + changelog
                        + ": <rollback> has a <" + el.getLocalName() + "> child; only <sql> children can be replayed");
                }
                hasSql = true;
            } else if (n.getNodeType() == org.w3c.dom.Node.TEXT_NODE
                    || n.getNodeType() == org.w3c.dom.Node.CDATA_SECTION_NODE) {
                loose.append(n.getNodeValue());
            }
        }
        if (hasSql && !loose.toString().isBlank()) {
            throw new IllegalStateException("changeset " + changesetId + " in " + changelog
                + ": <rollback> mixes <sql> children with loose text; the replay would pick one and drop the other");
        }
        return hasSql ? directSql(rollback) : loose.toString();
    }

    /**
     * Throws {@link IllegalStateException} when changeset {@code cs}, or any {@code <sql>} in it, carries
     * {@code dbms} or {@code endDelimiter}: either changes what Liquibase would send, and a one-string replay
     * honours neither. No product changeset carries them today; PgContainerHelperChangesetTextTest pins the check.
     */
    static void refuseUnreplayable(org.w3c.dom.Element cs, String changelog) {
        boolean scoped = !cs.getAttribute("dbms").isEmpty();
        org.w3c.dom.NodeList sqls = cs.getElementsByTagNameNS(LIQUIBASE_NS, "sql");
        for (int j = 0; j < sqls.getLength(); j++) {
            var el = (org.w3c.dom.Element) sqls.item(j);
            scoped |= !el.getAttribute("dbms").isEmpty() || !el.getAttribute("endDelimiter").isEmpty();
        }
        if (scoped) {
            throw new IllegalStateException("changeset " + cs.getAttribute("id") + " in " + changelog
                + " carries dbms or endDelimiter, which a one-string replay cannot honour");
        }
    }

    private static String directSql(org.w3c.dom.Element parent) {
        StringBuilder sb = new StringBuilder();
        for (var n = parent.getFirstChild(); n != null; n = n.getNextSibling()) {
            if (n instanceof org.w3c.dom.Element el && "sql".equals(el.getLocalName())) {
                sb.append(el.getTextContent()).append('\n');
            }
        }
        return sb.toString();
    }

    /**
     * The plan text of {@code EXPLAIN query} (nexus-cbo4a batch 9), the query bound as it would run: the server's own
     * lines, every row, joined with newlines. Runs on {@code ctx}'s connection and transaction, so a tenant GUC or SET
     * LOCAL pin applies. It does not return jOOQ's {@code Explain.plan()}, a {@code Result.format()} table that sizes
     * its column from the first 50 rows and cuts a wider later row with "..." (nexus-ou2za: a cut row could hide the
     * text a {@code doesNotContain} looks for); an execute listener keeps the rows {@code ctx.explain} fetched.
     */
    public static String explainPlan(DSLContext ctx, Query query) {
        return rawExplain(ctx.configuration(), query);
    }

    /** Runs jOOQ's EXPLAIN of {@code query} on {@code configuration} and returns the fetched plan rows, uncut. */
    private static String rawExplain(Configuration configuration, Query query) {
        List<Result<?>> fetched = new ArrayList<>();
        configuration.deriveAppending(new ExecuteListener() {
            @Override
            public void fetchEnd(ExecuteContext e) {
                if (e.result() != null) {
                    fetched.add(e.result());
                }
            }
        }).dsl().explain(query);
        if (fetched.size() != 1) {
            throw new IllegalStateException("EXPLAIN fetched " + fetched.size() + " results, expected one (nexus-ou2za)");
        }
        return fetched.get(0).stream().map(r -> String.valueOf(r.get(0))).collect(Collectors.joining("\n"));
    }

    /**
     * {@link #explainPlan} with every bind value rendered inline as a literal (jOOQ's static statement): the CUSTOM
     * plan the planner makes for a statement carrying constants. It proves what the planner can do for those
     * literals, not what production runs: production binds parameters, and a prepared statement can switch to a
     * GENERIC plan (plan_cache_mode) whose row estimates ignore the bound values. Read a pass here as the best-case
     * plan; a generic-plan test is a separate proof.
     */
    public static String explainPlanInlined(DSLContext ctx, Query query) {
        return rawExplain(ctx.configuration().deriveSettings(s -> s.withStatementType(StatementType.STATIC_STATEMENT)),
            query);
    }

    /**
     * The four {@code EXPLAIN} options {@code nexus_test.explain_plan} takes (nexus-cbo4a batch C). jOOQ's
     * {@code ctx.explain} runs a plain {@code EXPLAIN}; a test that reads {@code loops=} counts, {@code Output:} lines or
     * buffer counts needs ANALYZE, VERBOSE or BUFFERS, and one that must not see cost numbers needs COSTS OFF. The
     * options are booleans, never text: the function assembles the option list server side.
     */
    public record ExplainOptions(boolean analyze, boolean verbose, boolean buffers, boolean costs) {
        /** {@code EXPLAIN (ANALYZE, VERBOSE, COSTS OFF)}. */
        public static final ExplainOptions ANALYZE_VERBOSE_COSTS_OFF = new ExplainOptions(true, true, false, false);
        /** {@code EXPLAIN (ANALYZE, BUFFERS)}. */
        public static final ExplainOptions ANALYZE_BUFFERS = new ExplainOptions(true, false, true, true);
    }

    /**
     * The plan text of {@code EXPLAIN (options) query}, every row, through {@code nexus_test.explain_plan}
     * (db.changelog-test-objects.xml, nexus-cbo4a batch C). It takes a jOOQ {@link Query} and nothing else, rendered
     * with every bind value inlined ({@code ctx.renderInlined}): EXPLAIN cannot take bind parameters through the
     * function's EXECUTE, so this is the CUSTOM plan for those literal values, the plan the planner makes for the
     * first execution of a prepared statement whose parameters are known. A caller needing the GENERIC plan
     * (plan_cache_mode) proves it separately. It takes no String, but a jOOQ Query can itself be plain SQL
     * ({@code ctx.resultQuery(text)}), so the type alone does not keep Java SQL text out. Two callers pass one, and
     * neither text is written in Java: ManifestLessCensusNotesGuardIndexPlanShapeTest explains the product constant
     * PgVectorRepository.MANIFEST_LESS_CENSUS_SQL (SqlStringLiteralGateTest's PRODUCT_SQL map counts it), and
     * TaxonomyAssignCrossLateralHnswTest explains assign_from_chashes_1024's deployed body read from pg_proc.
     *
     * <p>The function runs on {@code ctx}'s connection and in its open transaction, as its role, so a tenant GUC and any
     * SET LOCAL planner pin apply to the plan and ANALYZE executes the query under the caller's row-level security.
     * The role needs USAGE on schema nexus_test, which {@link #bootstrapServiceRole} grants.
     *
     * @param ctx     the context to run on; bind it to the connection or transaction the query is meant to run in
     * @param query   the statement to explain (a select, or an insert, update or delete)
     * @param options the EXPLAIN options
     */
    public static String explainPlanWith(DSLContext ctx, Query query, ExplainOptions options) {
        return String.join("\n", ctx
            .select(DSL.function(DSL.name("nexus_test", "explain_plan"), SQLDataType.CLOB,
                DSL.val(ctx.renderInlined(query)), DSL.val(options.analyze()), DSL.val(options.verbose()),
                DSL.val(options.buffers()), DSL.val(options.costs())))
            .fetch(0, String.class));
    }

    /**
     * {@code ANALYZE table} on an existing, test-owned {@link Connection} (nexus-cbo4a
     * batch 3/4) -- the test-tree counterpart to {@link TenantScope#vacuumAnalyze}.
     *
     * <p>Batch 4 (nexus-zrcj7): retired the raw {@code stmt.execute("ANALYZE " + ...)}
     * onto {@code nexus_test.analyze_table(regclass)}, a TEST-SCHEMA function
     * (db/changelog-test/db.changelog-test-objects.xml, applied by
     * {@link #applyProductSchema} after the product master; jOOQ codegen for it runs
     * as a second plugin execution at generate-test-sources into
     * target/generated-test-sources/jooq, so nothing test-only ever enters the
     * product schema or the product changelog -- batch 5, Sam's ruling 2026-09-05)
     * -- ANALYZE is still PostgreSQL maintenance syntax with no typed jOOQ DSL form (same
     * category {@link TenantScope#vacuumAnalyze}'s own SANCTIONED RAW comment documents
     * for VACUUM), but that raw statement now lives server-side, inside a plpgsql wrapper
     * function, rather than being assembled client-side. Table identity comes from a
     * generated jOOQ {@link Table}, rendered through {@code ctx.render(table)} (properly
     * quoted/qualified for the dialect) into the function's {@code regclass} argument --
     * never a hand-typed schema-qualified string literal, so unlike {@code vacuumAnalyze}'s
     * allowlist (needed there because VACUUM's callers pass a bare string) there is no
     * caller-controlled name to validate: only a compile-time-checked generated
     * {@code Table} reaches this method. {@code Routines.analyzeTable}'s generated
     * {@code target} parameter is typed {@code Object} ({@code @Deprecated}, "Unknown data
     * type") because {@code regclass} has no jOOQ-recognized Java mapping -- the same
     * shape {@code CatalogRepository#searchDocuments}'s {@code Routines.catalogFtsMatch}
     * call already carries for its {@code tsvector} parameter; jOOQ still renders an
     * explicit {@code CAST(? AS "pg_catalog"."regclass")} around the bind value, so the
     * rendered table-name string is parsed by PostgreSQL's own regclass input function,
     * never string-concatenated into the query text.
     *
     * @param conn  the connection to run ANALYZE on; unlike {@code vacuumAnalyze}, ANALYZE
     *              has no autocommit/transaction-block restriction, so this method neither
     *              inspects nor changes the connection's autocommit state
     * @param table the generated jOOQ table to analyze (e.g. {@code Tables.CHUNKS})
     */
    public static void analyzeTable(Connection conn, Table<?> table) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.analyzeTable(ctx.configuration(), ctx.render(table));
    }

    /**
     * {@code ALTER TABLE .. ADD CONSTRAINT .. FOREIGN KEY (tenant_id, column) REFERENCES
     * ref (tenant_id, refColumn) extraClause NOT VALID} (nexus-cbo4a batch 10 review
     * fold-in) -- the test-tree counterpart to {@link #analyzeTable}, same idiom: the
     * raw statement moves server-side into {@code nexus_test.add_fk_not_valid}
     * (db/changelog-test/db.changelog-test-objects.xml), a Postgres-only {@code ALTER
     * TABLE} extension ({@code NOT VALID}) with no jOOQ typed-DSL form (confirmed
     * against jOOQ 3.20.11/3.21's manual: {@code alterConstraint().enforced()/
     * notEnforced()} renders MySQL-style {@code [NOT] ENFORCED}, not Postgres's
     * {@code NOT VALID}). {@code table}/{@code refTable} are generated jOOQ
     * {@link Table}s, rendered through {@code ctx.render(...)} into the function's
     * {@code regclass} arguments -- never a hand-typed schema-qualified string. Every
     * caller's composite FK is {@code (tenant_id, X) -> (tenant_id, Y)}, so
     * {@code tenant_id} is hardcoded as the first column on both sides inside the
     * function body rather than parameterizing a shape no call site actually varies.
     *
     * @param conn           the connection to run the ALTER TABLE on (superuser/table owner)
     * @param table          the table gaining the FK (e.g. {@code Tables.CHUNKS})
     * @param constraintName the FK constraint name
     * @param column         the second FK column (after {@code tenant_id})
     * @param refTable       the referenced table (e.g. {@code Tables.CATALOG_COLLECTIONS})
     * @param refColumn      the second referenced column (after {@code tenant_id})
     * @param extraClause    the {@code ON UPDATE}/{@code ON DELETE} clause fragment
     *                       (e.g. {@code "ON DELETE RESTRICT"}), or {@code ""} for none
     */
    public static void addFkNotValid(Connection conn, Table<?> table, String constraintName, String column,
                                      Table<?> refTable, String refColumn, String extraClause) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.addFkNotValid(ctx.configuration(), ctx.render(table), constraintName, column,
            ctx.render(refTable), refColumn, extraClause);
    }

    /**
     * {@code ALTER TABLE .. VALIDATE CONSTRAINT} (nexus-cbo4a batch 10 review fold-in)
     * -- see {@link #addFkNotValid} for the full contract this shares. {@code
     * VALIDATE CONSTRAINT} has no jOOQ typed-DSL form.
     *
     * @param conn           the connection to run the ALTER TABLE on
     * @param table          the table whose constraint is being validated
     * @param constraintName the constraint name
     */
    public static void validateConstraint(Connection conn, Table<?> table, String constraintName) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.validateConstraint(ctx.configuration(), ctx.render(table), constraintName);
    }

    /**
     * {@code ALTER TABLE .. [NO] FORCE ROW LEVEL SECURITY} (nexus-cbo4a batch 10
     * review fold-in) -- see {@link #addFkNotValid} for the full contract this
     * shares. {@code [NO] FORCE ROW LEVEL SECURITY} is a Postgres-only RLS DDL
     * extension with no jOOQ typed-DSL form.
     *
     * @param conn  the connection to run the ALTER TABLE on (table owner)
     * @param table the table to toggle {@code FORCE ROW LEVEL SECURITY} on
     * @param force {@code true} for {@code FORCE ROW LEVEL SECURITY}, {@code false}
     *              for {@code NO FORCE ROW LEVEL SECURITY}
     */
    public static void setForceRls(Connection conn, Table<?> table, boolean force) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.setForceRls(ctx.configuration(), ctx.render(table), force);
    }

    /**
     * {@code LOCK TABLE table IN mode MODE} (nexus-cbo4a batch 6) through
     * {@code nexus_test.lock_table(text, text)} (db.changelog-test-objects.xml), the same server-side wrapper idiom
     * as {@link #setForceRls}: jOOQ has no LOCK clause. The lock is taken in the connection's open transaction and
     * held until it ends, so {@code conn} must have {@code autoCommit=false} for the lock to outlive the call.
     * {@code table} is a generated jOOQ {@link Table}, rendered through {@code ctx.render(...)} and cast to
     * {@code regclass} inside the function; {@code mode} is checked there against the eight PostgreSQL lock modes.
     * Called as a plain function ({@code DSL.function}), not through the generated {@code Routines}, so a stale
     * generated-test-sources tree does not stop this from compiling.
     *
     * @param conn  the connection whose transaction takes the lock
     * @param table the generated jOOQ table to lock (e.g. {@code Tables.SERVICE_TOKENS})
     * @param mode  the PostgreSQL table lock mode
     */
    public static void lockTable(Connection conn, Table<?> table, LockMode mode) {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.select(DSL.function(DSL.name("nexus_test", "lock_table"), SQLDataType.BOOLEAN,
                DSL.val(ctx.render(table)), DSL.val(mode.name().replace('_', ' '))))
            .fetch();
    }

    /**
     * The eight PostgreSQL table lock modes, for {@link #lockTable}; each constant's name with underscores as
     * spaces is the mode's keyword form, which {@code nexus_test.lock_table} checks again server-side.
     */
    public enum LockMode {
        ACCESS_SHARE, ROW_SHARE, ROW_EXCLUSIVE, SHARE_UPDATE_EXCLUSIVE,
        SHARE, SHARE_ROW_EXCLUSIVE, EXCLUSIVE, ACCESS_EXCLUSIVE
    }

    /**
     * {@code ALTER TABLE .. DROP CONSTRAINT IF EXISTS ..} (nexus-cbo4a batch 11) --
     * a genuinely dangling manifest row can only be seeded by momentarily dropping its
     * FK, inserting the dangling row, then re-adding the constraint NOT VALID (see
     * {@link #addFkNotValidComposite3}) -- the insert happens BETWEEN this call and
     * that one, so the two are separate methods rather than one combined drop-then-add.
     * {@code table} is a generated jOOQ {@link Table}, rendered through
     * {@code ctx.render(...)} into the function's {@code regclass} argument -- never a
     * hand-typed schema-qualified string.
     *
     * @param conn           the connection to run the ALTER TABLE on (superuser/table owner)
     * @param table          the table whose constraint is being dropped
     * @param constraintName the constraint name
     */
    public static void dropConstraint(Connection conn, Table<?> table, String constraintName) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.dropConstraint(ctx.configuration(), ctx.render(table), constraintName);
    }

    /**
     * First half of the dangling-manifest-row idiom (nexus-cbo4a): {@link #dropConstraint} of
     * {@code fk_catalog_chunks_chunk}, so the caller can seed a {@code catalog_document_chunks} row with no
     * matching {@code nexus.chunks} row. Pair it with {@link #readdCatalogChunksFkNotValid} once the row is in.
     */
    public static void dropCatalogChunksFk(Connection conn) throws SQLException {
        dropConstraint(conn, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk");
    }

    /**
     * Re-adds {@code chunks_chash_octet_check} and {@code catalog_document_chunks_chash_octet_check}
     * NOT VALID, each only if its table lacks it (nexus-cbo4a), after a test dropped one or both with
     * jOOQ's {@code dropConstraint} to seed a legacy 16-byte chash. Runs
     * {@code db.changelog-test-chash-octet-checks-readd.xml}; see that file's header for why this is not
     * jOOQ. Needs only the two tables, not the {@code nexus_test} objects. Leaves {@code su} autoCommit.
     */
    public static void readdChashOctetChecksNotValid(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-chash-octet-checks-readd.xml",
            "databasechangelog_test_octet_checks", Map.of());
        // The changelog's preconditions skip a constraint that is present; check the post-state outright so a
        // skipped or failed re-add cannot leave a caller's seed window open unnoticed.
        // constraintValidated reads the declaring table's own row, not the copies each chunks partition inherits.
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        for (String conname : new String[]{"chunks_chash_octet_check", "catalog_document_chunks_chash_octet_check"}) {
            Boolean validated = PgCatalogProbes.constraintValidated(ctx, conname);
            if (!Boolean.FALSE.equals(validated)) {
                throw new IllegalStateException(conname + " after re-add: expected present and NOT VALID, convalidated="
                    + validated);
            }
        }
    }

    /**
     * Reverts {@code nexus.topic_assignments.doc_id} to TEXT (hex) through
     * {@code db.changelog-test-topic-assignments-text-doc-id.xml} (nexus-cbo4a), for a test that replays a
     * changeset written for the TEXT-era column. The caller drops what depends on the column's type first.
     * Leaves {@code su} autoCommit.
     */
    public static void revertTopicAssignmentsDocIdToText(Connection su) throws Exception {
        runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-topic-assignments-text-doc-id.xml",
            "databasechangelog_test_text_doc_id", Map.of());
    }

    /**
     * Second half of the dangling-manifest-row idiom (nexus-cbo4a): re-adds {@code fk_catalog_chunks_chunk}
     * NOT VALID in catalog-029-0's shape through {@link #addFkNotValidComposite3}, so it is live (unvalidated)
     * again for every later statement. The function appends {@code embedding_model} to both column lists when
     * both tables carry it, which gives the four-column key (tenant_id, collection, chash, embedding_model).
     * The ON UPDATE / DEFERRABLE clause is held here once rather than at every call site.
     */
    public static void readdCatalogChunksFkNotValid(Connection conn) throws SQLException {
        addFkNotValidComposite3(conn, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk",
            "collection", "chash", CHUNKS, "collection", "chash",
            "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE");
    }

    /**
     * {@code DROP INDEX <schema>.<index>} (nexus-cbo4a batch 13) via
     * {@code nexus_test.drop_index(regclass)} (db.changelog-test-objects.xml).
     * jOOQ's own {@code dropIndex} never schema-qualifies the index name on
     * POSTGRES, so it would resolve through the session search_path; the
     * qualified {@link Name} is rendered into the function's regclass argument
     * instead, so PostgreSQL resolves the identity itself, and the function
     * renders the DROP from pg_class by oid as an explicit schema.name pair.
     */
    public static void dropIndex(Connection conn, Name qualifiedIndex) throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.dropIndex(ctx.configuration(), ctx.render(qualifiedIndex));
    }

    /**
     * {@code ALTER TABLE .. ADD CONSTRAINT .. FOREIGN KEY (tenant_id, col2, col3)
     * REFERENCES .. (tenant_id, refCol2, refCol3) extraClause NOT VALID} (nexus-cbo4a
     * batch 11) -- the THREE-column composite-FK sibling of {@link #addFkNotValid},
     * which only covers a {@code (tenant_id, X) -> (tenant_id, Y)} shape. A
     * dangling-manifest-row test needs to re-add {@code fk_catalog_chunks_chunk}'s real
     * {@code (tenant_id, collection, chash)} composite after {@link #dropConstraint} and
     * an intervening dangling-row insert -- moved server-side into {@code
     * nexus_test.add_fk_not_valid_composite3} (db.changelog-test-objects.xml).
     * {@code table}/{@code refTable} are generated jOOQ {@link Table}s, rendered
     * through {@code ctx.render(...)} into the function's {@code regclass} arguments --
     * never a hand-typed schema-qualified string.
     *
     * @param conn        the connection to run the ALTER TABLE on (superuser/table owner)
     * @param table       the table gaining the FK (e.g. {@code Tables.CATALOG_DOCUMENT_CHUNKS})
     * @param constraintName the FK constraint name
     * @param column2     the second FK column (after {@code tenant_id})
     * @param column3     the third FK column
     * @param refTable    the referenced table (e.g. {@code Tables.CHUNKS})
     * @param refColumn2  the second referenced column (after {@code tenant_id})
     * @param refColumn3  the third referenced column
     * @param extraClause the {@code ON UPDATE}/{@code ON DELETE} clause fragment
     *                    (e.g. {@code "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE"}),
     *                    or {@code ""} for none
     */
    public static void addFkNotValidComposite3(Connection conn, Table<?> table, String constraintName,
                                                String column2, String column3, Table<?> refTable,
                                                String refColumn2, String refColumn3, String extraClause)
            throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.addFkNotValidComposite3(ctx.configuration(), ctx.render(table), constraintName, column2, column3,
            ctx.render(refTable), refColumn2, refColumn3, extraClause);
    }

    /**
     * {@code GRANT EXECUTE ON FUNCTION functionSignature TO role} (nexus-cbo4a
     * batch 11) -- jOOQ's typed GRANT DSL ({@link DSLContext#grant}/{@code
     * GrantOnStep#on}) targets tables, not a function's parenthesized
     * argument-type signature, which Postgres's {@code GRANT ... ON FUNCTION}
     * syntax requires -- moved server-side into {@code
     * nexus_test.grant_execute_on_function} (db.changelog-test-objects.xml).
     *
     * @param conn              the connection to run the GRANT on (superuser)
     * @param functionSignature the function name plus its argument-type list
     *                          (e.g. {@code "nexus.gc_quarantine_orphans(int, text,
     *                          text, text, text, int)"}) -- a fixed Java literal,
     *                          never end-user input
     * @param role              the role to grant EXECUTE to
     */
    public static void grantExecuteOnFunction(Connection conn, String functionSignature, String role)
            throws SQLException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        Routines.grantExecuteOnFunction(ctx.configuration(), functionSignature, role);
    }
}
