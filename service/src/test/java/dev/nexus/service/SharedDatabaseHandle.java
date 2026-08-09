// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Properties;

/**
 * A {@link PostgreSQLContainer}-shaped HANDLE onto one database inside an
 * already-running, per-fork SHARED cluster (nexus-yhmav) -- not a real,
 * independently-managed container.
 *
 * <p>This is the transparency seam that lets {@link PgContainerHelper#start()}
 * amortize container BOOT (the dominant per-class cost measured by nexus-13eb0:
 * host CPU idle while Testcontainers boot-churn serializes through the Docker
 * daemon) without touching the ~150 test classes that call {@code pg.createConnection},
 * {@code pg.getJdbcUrl}, {@code pg.getUsername/getPassword}, and {@code pg.stop} --
 * the entire accessor surface those classes use (verified by a repo-wide grep before
 * this class was written; nothing calls {@code getMappedPort}, {@code execInContainer},
 * or other lifecycle-dependent {@code GenericContainer} methods on the {@code pg}
 * variable).
 *
 * <p><b>Never call {@link #start()} on an instance of this class</b> -- it is not
 * a container Testcontainers manages; {@code delegate} already is. A caller that
 * needs an independently-started/managed container (e.g. attached to a custom
 * {@code Network}, as {@code PgBouncerTenantIsolationTest} does) must use
 * {@link PgContainerHelper#newContainer()} directly, which is untouched by this class.
 *
 * <p><b>{@link #stop()} never stops the shared {@code delegate}</b> -- other test
 * classes in this fork still need it. It best-effort DROPs this handle's own
 * per-class database instead (after terminating any lingering backends -- DROP
 * DATABASE fails while connections remain open). A drop failure is logged, not
 * thrown: a leaked per-class database is a harmless disk-footprint residual for
 * the remainder of this fork's run, not a suite-correctness problem, so it must
 * not fail an otherwise-passing test's teardown.
 */
final class SharedDatabaseHandle extends PostgreSQLContainer<SharedDatabaseHandle> {

    private static final Logger LOG = LoggerFactory.getLogger(SharedDatabaseHandle.class);

    private final PostgreSQLContainer<?> delegate;
    private final String dbName;

    SharedDatabaseHandle(PostgreSQLContainer<?> delegate, String dbName) {
        super(DockerImageName.parse(PgContainerHelper.IMAGE).asCompatibleSubstituteFor("postgres"));
        this.delegate = delegate;
        this.dbName = dbName;
    }

    @Override
    public String getDatabaseName() {
        return dbName;
    }

    @Override
    public String getUsername() {
        return delegate.getUsername();
    }

    @Override
    public String getPassword() {
        return delegate.getPassword();
    }

    @Override
    public String getJdbcUrl() {
        return "jdbc:postgresql://" + delegate.getHost() + ":" + delegate.getMappedPort(POSTGRESQL_PORT)
            + "/" + dbName + "?sslmode=disable";
    }

    /**
     * Reimplements {@code JdbcDatabaseContainer#createConnection} without its
     * {@code isRunning()}-gated connect-retry loop: {@code isRunning()} on THIS
     * instance is always false (it was never {@code start()}ed -- {@code delegate}
     * is the container that is actually running), so the inherited loop would
     * return immediately with no connection attempt at all. The shared cluster is
     * already confirmed up by the time any handle is created, so a bare connect
     * (no retry) is correct and sufficient -- unlike {@link FailFastPostgreSQLContainer},
     * there is no boot-race to classify here.
     */
    @Override
    public Connection createConnection(String queryString, Properties info) throws SQLException {
        Properties properties = new Properties(info);
        properties.put("user", getUsername());
        properties.put("password", getPassword());
        String url = constructUrlForConnection(queryString);
        return DriverManager.getConnection(url, properties);
    }

    @Override
    public void start() {
        throw new IllegalStateException(
            "SharedDatabaseHandle targets an already-running shared cluster -- it is not a real, "
            + "independently-startable container. A caller reaching this means it expects a "
            + "genuinely fresh/dedicated container: use PgContainerHelper.newContainer() + start() "
            + "(network-attached / multi-container cases, e.g. PgBouncerTenantIsolationTest) or "
            + "PgContainerHelper.startDedicated() (migration-PROCESS tests that assert on a pristine "
            + "un-migrated cluster, e.g. SchemaMigratorIntegrationTest) instead.");
    }

    @Override
    public void stop() {
        try (Connection admin = SharedCluster.controlConnection(delegate)) {
            admin.setAutoCommit(true);
            try (var st = admin.createStatement()) {
                st.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    + "WHERE datname = '" + dbName + "' AND pid <> pg_backend_pid()");
                st.execute("DROP DATABASE IF EXISTS \"" + dbName + "\"");
            }
        } catch (SQLException e) {
            LOG.warn("best-effort DROP DATABASE \"{}\" failed (nexus-yhmav shared-cluster handle) "
                + "-- leaking a per-class database for the remainder of this fork's run, harmless "
                + "except for disk footprint", dbName, e);
        }
    }
}
