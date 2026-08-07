// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.Connection;
import java.sql.Driver;
import java.sql.SQLException;
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
 * OVERRIDES {@code JdbcDatabaseContainer}'s connect-loop wait entirely, gating on a
 * {@code LogMessageWaitStrategy} ("database system is ready to accept connections",
 * required twice) instead. Postgres's {@code initdb} finalizes the superuser role and
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
final class FailFastPostgreSQLContainer extends PostgreSQLContainer<FailFastPostgreSQLContainer> {

    /** SQLSTATE 28000 — invalid_authorization_specification (e.g. "role ... does not exist"). */
    static final String SQLSTATE_INVALID_AUTHORIZATION_SPECIFICATION = "28000";

    /** SQLSTATE 28P01 — invalid_password. */
    static final String SQLSTATE_INVALID_PASSWORD = "28P01";

    FailFastPostgreSQLContainer(DockerImageName dockerImageName) {
        super(dockerImageName);
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
