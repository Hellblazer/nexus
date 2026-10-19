// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.List;
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
 * thrown: a leaked per-class database is a footprint residual for the remainder
 * of this fork's run, not a suite-correctness problem, so it must not fail an
 * otherwise-passing test's teardown. Since nexus-xynce that footprint is tmpfs
 * RAM counted against the cluster's 2 GB cap (a clone of the template is about
 * 14 MB to start), so a class should still stop its handle.
 */
final class SharedDatabaseHandle extends PostgreSQLContainer<SharedDatabaseHandle> {

    private static final Logger LOG = LoggerFactory.getLogger(SharedDatabaseHandle.class);

    private final PostgreSQLContainer<?> delegate;
    private final String dbName;
    /** Cluster-wide roles this holder declared exclusive (nexus-7xfh7); dropped after the database. */
    private final List<String> exclusiveRoles;

    SharedDatabaseHandle(PostgreSQLContainer<?> delegate, String dbName) {
        this(delegate, dbName, List.of());
    }

    SharedDatabaseHandle(PostgreSQLContainer<?> delegate, String dbName, List<String> exclusiveRoles) {
        super(DockerImageName.parse(PgContainerHelper.IMAGE).asCompatibleSubstituteFor("postgres"));
        this.delegate = delegate;
        this.dbName = dbName;
        this.exclusiveRoles = List.copyOf(exclusiveRoles);
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
            + "PgContainerHelper.startEmptyDatabase() (migration-PROCESS tests that assert on a pristine "
            + "un-migrated database) or PgContainerHelper.startDedicated() (cluster-level state) instead.");
    }

    @Override
    public void stop() {
        try (Connection admin = SharedCluster.controlConnection(delegate)) {
            admin.setAutoCommit(true);
            PgCatalogProbes.terminateOtherBackends(DSL.using(admin, SQLDialect.POSTGRES), dbName);
            try (var st = admin.createStatement()) {
                st.execute("DROP DATABASE IF EXISTS \"" + dbName + "\"");
                // After the database: a role cannot be dropped while it holds privileges in a live one.
                // Nor while it is the GRANTOR of a role membership (PostgreSQL 16+ records the grantor as a
                // dependency): a migrating role that ran grants-004's GRANT pg_monitor TO nexus_svc is one.
                // Revoke what it granted, then drop it.
                if (!exclusiveRoles.isEmpty()) {
                    // Role names are validated plain identifiers (SharedCluster.createDatabase).
                    String names = exclusiveRoles.stream().map(r -> "'" + r + "'").collect(java.util.stream.Collectors.joining(","));
                    st.execute("DO $$ DECLARE r text; m record; BEGIN "
                        + "  FOREACH r IN ARRAY ARRAY[" + names + "] LOOP "
                        + "    FOR m IN SELECT g.rolname AS granted, u.rolname AS member FROM pg_auth_members a "
                        + "             JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles u ON u.oid = a.member "
                        + "             JOIN pg_roles o ON o.oid = a.grantor WHERE o.rolname = r LOOP "
                        + "      EXECUTE format('REVOKE %I FROM %I GRANTED BY %I', m.granted, m.member, r); "
                        + "    END LOOP; "
                        + "    EXECUTE format('DROP ROLE IF EXISTS %I', r); "
                        + "  END LOOP; "
                        + "END $$");
                }
            }
        } catch (SQLException e) {
            LOG.warn("best-effort DROP DATABASE \"{}\" failed (nexus-yhmav shared-cluster handle) "
                + "-- leaking a per-class database for the remainder of this fork's run, harmless "
                + "except for disk footprint", dbName, e);
        }
    }
}
