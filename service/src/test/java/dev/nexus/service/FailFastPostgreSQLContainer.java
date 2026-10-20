// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.Connection;
import java.sql.Driver;
import java.sql.SQLException;
import java.time.Duration;
import java.util.Properties;
import java.util.concurrent.TimeUnit;

/**
 * {@link PostgreSQLContainer} that fails FAST on a deterministic authentication
 * rejection instead of exhausting {@code JdbcDatabaseContainer}'s ~120s connect-retry
 * budget (nexus-soqa8, hardening (c) of nexus-lgdy1).
 *
 * <p>MECHANISM (nexus-lgdy1, T2 {@code nexus/cascade-test-container-failure-diagnosis-
 * 2026-07-31}, [21315]): a leaked host-side Postgres (or any other host process) can
 * squat the exact ephemeral port Docker publishes this container's 5432 onto. macOS/BSD
 * routes an inbound connection to the MOST SPECIFIC bound socket, so a client dialling
 * the published port reaches the squatter, not the container — even though the container
 * itself is healthy and its own wait strategy already reported it started. The squatter
 * has no {@code postgres} role (or different credentials), so the JDBC driver gets a
 * clean, immediate {@code FATAL} at the auth handshake: SQLSTATE {@code 28000}
 * (invalid_authorization_specification — "role ... does not exist") or {@code 28P01}
 * (invalid_password). {@link org.testcontainers.containers.JdbcDatabaseContainer#createConnection}
 * cannot tell that apart from a container that is still starting up (connection refused):
 * it catches every {@link SQLException} identically inside its own retry loop and only
 * ever surfaces the last one, after the full connect-timeout budget (120s default) — so
 * the SAME deterministic auth failure replays roughly 1200 times before a caller ever
 * sees it, and a 3.97s container start presents as a 124s "broken container".
 *
 * <p>This override intercepts each individual connect attempt — the finest grain
 * {@code JdbcDatabaseContainer} exposes; there is no narrower hook — and fails
 * immediately, with a message naming the nexus-lgdy1 class and the published port,
 * whenever the driver's exception carries one of those two auth SQLSTATEs. Every other
 * {@link SQLException} (connection refused, "the database system is starting up", SSL
 * negotiation flakes — see {@link PgContainerHelper#newContainer()}'s
 * {@code sslmode=disable} note) still retries exactly as before: this narrows the retry
 * loop's failure CLASSIFICATION only, it does not shrink the timeout budget for
 * genuinely-transient conditions.
 *
 * <p>This is a deliberate, minimal duplication of {@code JdbcDatabaseContainer}'s
 * {@code createConnection} loop, not a fork of container bring-up in general. The loop
 * shape has to be reimplemented because per-attempt classification is not overridable any
 * other way: {@code createConnection} swallows every {@link SQLException} inside its own
 * retry loop and never surfaces an individual attempt's failure to a caller before its
 * timeout elapses.
 *
 * <p><b>LOAD-BEARING INVARIANT</b> (nexus-soqa8 substantive-critic critique, T2
 * {@code nexus/nexus-soqa8-critique-2026-08-06} [21531]): failing fast on the FIRST
 * classified attempt is safe only because {@link PostgreSQLContainer#waitUntilContainerStarted()}
 * OVERRIDES {@code JdbcDatabaseContainer}'s connect-loop wait entirely, gating on the wait
 * strategy {@link PgContainerHelper#newContainer} sets instead. With the DDL counter on (the default;
 * nexus-sz651) that is a {@code pg_isready} probe on TCP, which the init's unix-socket-only temp server
 * never passes: the server log is a file then and no longer carries the "ready to accept connections"
 * line on Docker's log, where the earlier {@code LogMessageWaitStrategy} read it. With the counter off
 * ({@code -Dnexus.ddl.count=false}, nexus-3cwsw) there is no collector, the ready line is on Docker's log
 * again, and the library's default {@code LogMessageWaitStrategy} stays. Postgres's {@code initdb} finalizes the superuser role and
 * credentials synchronously, before either ready line — so by the time {@code start()}
 * returns and ANY application code makes its first {@code createConnection} call, the
 * server side of the auth handshake is already fixed; a classified failure at attempt 1
 * cannot be racing the container's own startup. {@code PgContainerHelper} configures no
 * {@code withInitScripts}, so testcontainers itself never calls {@code createConnection}
 * during {@code start()} either — every call in this test tree is application code, after
 * {@code start()} has returned. This invariant would break, silently and without any
 * existing test catching the regression, if either: (a) a future testcontainers upgrade
 * changes {@code PostgreSQLContainer} to stop overriding {@code waitUntilContainerStarted()}
 * (reverting to the base class's own createConnection-based wait, which this class does not
 * intercept), or (b) {@link PgContainerHelper#newContainer()} is ever changed to configure
 * {@code withInitScript(...)} or per-container credential overrides, which would move a
 * {@code createConnection} call earlier into the internal start-up path. Re-verify this
 * javadoc against the actual call ordering before either change lands.
 *
 * <p><b>VERSION-DRIFT GUARD</b> (code-review suggestion, T2
 * {@code nexus/nexus-soqa8-code-review-2026-08-06} [21530]): the loop below is a structural
 * copy of {@code JdbcDatabaseContainer#createConnection} as of testcontainers
 * {@code 1.21.4} (see {@code service/pom.xml}'s {@code testcontainers.version}). Bumping
 * that version should re-diff this method against the upstream source (the sources jar in
 * {@code ~/.m2/repository/org/testcontainers/jdbc/<version>/jdbc-<version>-sources.jar})
 * to confirm the retry shape (condition, sleep interval, final wrapped exception) has not
 * changed underneath this override.
 */
class FailFastPostgreSQLContainer extends PostgreSQLContainer<FailFastPostgreSQLContainer> {

    /** SQLSTATE 28000 — invalid_authorization_specification (e.g. "role ... does not exist"). */
    static final String SQLSTATE_INVALID_AUTHORIZATION_SPECIFICATION = "28000";

    /** SQLSTATE 28P01 — invalid_password. */
    static final String SQLSTATE_INVALID_PASSWORD = "28P01";

    FailFastPostgreSQLContainer(DockerImageName dockerImageName) {
        super(dockerImageName);
    }

    // nexus-sz651: DDL-per-run counting. The log is read once, at stop() or JVM exit.
    private volatile boolean ddlShared;
    private volatile String ddlOwner = "unknown";
    // False when the counter was switched off (-Dnexus.ddl.count=false) as this container was built:
    // it then carries none of the counter's settings, so its server log is Docker's log (PgContainerHelper.serverLog).
    // Whether it is registered is DdlCounts.register's own check of the switch.
    private volatile boolean ddlCounted = true;

    /** Whether this container was built with the counter's settings (see {@link DdlCounts#enabled()}). */
    FailFastPostgreSQLContainer ddlCounted(boolean counted) {
        this.ddlCounted = counted;
        return this;
    }

    boolean ddlCounted() {
        return ddlCounted;
    }

    /** The wait strategy this container will start with (a test reads it; the field is protected). */
    org.testcontainers.containers.wait.strategy.WaitStrategy ddlWaitStrategy() {
        return getWaitStrategy();
    }

    /** This container is the per-fork shared cluster (its logs split at the template's ready mark). */
    FailFastPostgreSQLContainer ddlSharedCluster() {
        this.ddlShared = true;
        return this;
    }

    FailFastPostgreSQLContainer ddlOwner(String owner) {
        this.ddlOwner = owner;
        return this;
    }

    boolean ddlShared() {
        return ddlShared;
    }

    String ddlOwner() {
        return ddlOwner;
    }

    private static final org.slf4j.Logger LOG = org.slf4j.LoggerFactory.getLogger(FailFastPostgreSQLContainer.class);

    private volatile String startFailureLogTail;

    /** The server log's tail captured when the last start's wait failed, or null when none failed (a test reads it). */
    String startFailureLogTail() {
        return startFailureLogTail;
    }

    /** One try, as long as the server log tail's read: a wedged daemon must not hang the failure report (nexus-ccg9y). */
    static final Duration INSPECT_TIMEOUT = Duration.ofSeconds(15);

    /** Test seam, per thread: a shorter wait for {@link #containerStateForLog}'s inspect. */
    static final ThreadLocal<Duration> INSPECT_TIMEOUT_OVERRIDE = new ThreadLocal<>();

    /** What the daemon says about a container that failed its wait (nexus-ccg9y). */
    record ContainerExit(String status, Long exitCode, Boolean oomKilled) { }

    /**
     * A FRESH inspect, not {@code getContainerInfo()}: that is the response cached when the container was created,
     * so it still says "running" after the postmaster died and the container exited. A seam for the stub test.
     */
    ContainerExit inspectExit() {
        var state = getDockerClient().inspectContainerCmd(getContainerId()).exec().getState();
        return state == null ? null : new ContainerExit(state.getStatus(), state.getExitCodeLong(), state.getOOMKilled());
    }

    /**
     * The container's state and exit code for the failure line, or why they could not be read. When the postmaster
     * dies the container exits and the tmpfs server log goes with it, so this is then the only diagnosis the line
     * carries (nexus-ccg9y). Never throws and waits at most {@link #INSPECT_TIMEOUT}: a daemon that cannot be asked, or
     * answers too slowly, must not mask or delay the original wait failure.
     */
    String containerStateForLog() {
        try {
            Duration timeout = INSPECT_TIMEOUT_OVERRIDE.get() != null ? INSPECT_TIMEOUT_OVERRIDE.get() : INSPECT_TIMEOUT;
            ContainerExit exit = PgContainerHelper.boundedOnce(this::inspectExit, timeout, "docker inspect");
            if (exit == null) {
                return "container_state=unknown (inspect returned no state)";
            }
            return "container_state=" + exit.status() + " exit_code=" + exit.exitCode() + " oom_killed=" + exit.oomKilled();
        } catch (RuntimeException e) {
            return "container_state=unreadable (" + e + ")";
        }
    }

    /**
     * nexus-t4y7b: when the wait fails, print the end of the server log FILE. Testcontainers' failed-start
     * dump is Docker's log, which with the counter on no longer carries the server's lines after the
     * collector started. The library retries the start (and calls this again) up to the startup attempts.
     */
    @Override
    protected void waitUntilContainerStarted() {
        try {
            super.waitUntilContainerStarted();
        } catch (RuntimeException e) {
            reportStartWaitFailure(e);
            throw e;
        }
    }

    /** Logs {@code event=start_wait_failed} with the container state and the server log tail; never throws. */
    void reportStartWaitFailure(RuntimeException e) {
        if (!ddlCounted) {
            return;
        }
        String state = containerStateForLog();
        String tail = PgContainerHelper.serverLogTail(this, PgContainerHelper.START_FAILURE_TAIL_LINES);
        startFailureLogTail = tail;
        LOG.error("event=start_wait_failed owner={} error={} {} server_log_tail=\n{}", ddlOwner, e.toString(), state, tail);
    }

    @Override
    public void start() {
        super.start();
        DdlCounts.register(this);
    }

    @Override
    public void stop() {
        try {
            DdlCounts.record(this);
        } catch (RuntimeException e) {
            // Counting is a diagnostic: whatever it hits, the container still has to stop.
            System.err.println("nexus-sz651: recording a container's DDL failed: " + e);
        } finally {
            super.stop();
        }
    }

    /**
     * Classify a connect-attempt failure as deterministic (will never succeed against
     * this server, so retrying is pointless) or presumed-transient (container may still
     * be starting up; retry is appropriate).
     *
     * <p>Reads {@link SQLException#getSQLState()} on the top-level exception only, not the
     * cause chain — the pgjdbc driver (this suite's only driver, confirmed against
     * {@code pom.xml}) throws {@code PSQLException} directly with the correct SQLSTATE, so
     * this has not been observed to matter in practice. If a future driver or dependency
     * change starts wrapping the auth failure in another {@link SQLException}, this
     * classifier would need a cause-chain walk to keep catching it.
     *
     * @return a short human-readable label for the deterministic failure class {@code e}
     *     belongs to, or {@code null} if {@code e} is not a known deterministic-auth-
     *     rejection SQLSTATE.
     */
    static String deterministicFailureReason(SQLException e) {
        String sqlState = e.getSQLState();
        if (SQLSTATE_INVALID_AUTHORIZATION_SPECIFICATION.equals(sqlState)) {
            return "invalid_authorization_specification (28000, e.g. role does not exist)";
        }
        if (SQLSTATE_INVALID_PASSWORD.equals(sqlState)) {
            return "invalid_password (28P01)";
        }
        return null;
    }

    /** Deterministic-wording diagnosis naming the nexus-lgdy1 class, port interpolated. */
    static String diagnosisMessage(String reason, int port) {
        return "connected server rejected the container's credentials (" + reason + ") -- likely a "
            + "host process squatting the published port (see nexus-lgdy1); check: "
            + "lsof -iTCP:" + port + " -sTCP:LISTEN";
    }

    @Override
    @SuppressWarnings("deprecation") // getConnectTimeoutSeconds(): no non-deprecated accessor exists (1.21.4)
    public Connection createConnection(String queryString, Properties info) throws SQLException, NoDriverFoundException {
        Properties properties = new Properties(info);
        properties.put("user", getUsername());
        properties.put("password", getPassword());
        String url = constructUrlForConnection(queryString);
        Driver jdbcDriverInstance = getJdbcDriverInstance();

        SQLException lastException = null;
        try {
            long start = System.nanoTime();
            while ((System.nanoTime() - start) < TimeUnit.SECONDS.toNanos(getConnectTimeoutSeconds()) && isRunning()) {
                try {
                    return jdbcDriverInstance.connect(url, properties);
                } catch (SQLException e) {
                    String deterministic = deterministicFailureReason(e);
                    if (deterministic != null) {
                        int port = getMappedPort(POSTGRESQL_PORT);
                        throw new IllegalStateException(diagnosisMessage(deterministic, port), e);
                    }
                    lastException = e;
                    Thread.sleep(100L);
                }
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        throw new SQLException("Could not create new connection", lastException);
    }
}
