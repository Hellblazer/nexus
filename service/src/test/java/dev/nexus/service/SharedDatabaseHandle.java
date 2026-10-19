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
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicBoolean;

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
 * classes in this fork still need it. It DROPs this handle's own per-class
 * database instead ({@code WITH (FORCE)}, which also ends any lingering backends).
 * A drop failure is logged, not thrown: a leaked per-class database is a footprint
 * residual for the remainder of this fork's run, not a suite-correctness problem,
 * so it must not fail an otherwise-passing test's teardown. Since nexus-xynce that
 * footprint is tmpfs RAM counted against the cluster's 2 GB cap (a clone of the
 * template is about 14 MB to start), so a class should still stop its handle.
 *
 * <p>The exception is the cluster-wide roles a holder declared exclusive: a leaked
 * one fails a DIFFERENT class's acquire later, so a failed role cleanup is thrown
 * from {@code stop()} (and logged at ERROR with the holder's name) to put the red on
 * the class that leaked. {@code stop()} runs once; a second call does nothing, so it
 * cannot drop a later holder's role of the same name.
 */
final class SharedDatabaseHandle extends PostgreSQLContainer<SharedDatabaseHandle> {

    private static final Logger LOG = LoggerFactory.getLogger(SharedDatabaseHandle.class);

    private final PostgreSQLContainer<?> delegate;
    private final String dbName;
    /** Cluster-wide roles this holder declared exclusive (nexus-7xfh7); dropped after the database. */
    private final List<String> exclusiveRoles;
    /** The test class that acquired this handle; named when its exclusive-role cleanup fails. */
    private final String holder;
    private final AtomicBoolean stopped = new AtomicBoolean();

    SharedDatabaseHandle(PostgreSQLContainer<?> delegate, String dbName, List<String> exclusiveRoles, String holder) {
        super(DockerImageName.parse(PgContainerHelper.IMAGE).asCompatibleSubstituteFor("postgres"));
        this.delegate = delegate;
        this.dbName = dbName;
        this.exclusiveRoles = List.copyOf(exclusiveRoles);
        this.holder = holder;
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
        // Once per handle: a second stop() after a later holder acquired the same exclusive role would
        // otherwise drop that holder's role out from under it.
        if (!stopped.compareAndSet(false, true)) {
            return;
        }
        // WITH (FORCE) (PostgreSQL 13+) terminates the remaining backends and drops in one statement, so
        // there is no terminate-then-drop window for a new connection to slip into.
        SharedCluster.forgetPristine(dbName);
        try (Connection admin = SharedCluster.controlConnection(delegate)) {
            SharedCluster.dropDatabase(admin, dbName);
            // nexus-sz651: the cluster's WAL position after this class's last write, for its bytes.
            DdlCounts.noteRelease(dbName, DdlCounts.walBytes(admin));
        } catch (SQLException e) {
            LOG.warn("best-effort DROP DATABASE \"{}\" failed (nexus-yhmav shared-cluster handle) "
                + "-- leaking a per-class database for the remainder of this fork's run; if this class declared "
                + "exclusive roles the role cleanup below will fail too and say so", dbName, e);
        }
        dropExclusiveRoles();
    }

    /**
     * Drop the roles this holder declared exclusive. NOT best-effort: a role left on the cluster fails the
     * next class that declares it (at {@link SharedCluster#acquireEmptyDatabase}), which is not the class
     * that leaked it. So a failure here is logged at ERROR with the class and the roles and thrown, which
     * puts the red on the leaking class's own teardown.
     */
    private void dropExclusiveRoles() {
        if (exclusiveRoles.isEmpty()) {
            return;
        }
        // After the database: a role cannot be dropped while it holds privileges in a live one.
        // Nor while it is the GRANTOR of a role membership (PostgreSQL 16+ records the grantor as a
        // dependency): a migrating role that ran grants-004's GRANT pg_monitor TO nexus_svc is one.
        // Revoke what it granted, then drop it (db.changelog-test-drop-exclusive-role.xml, nexus-cbo4a batch 8).
        // Role names are validated plain identifiers (SharedCluster.createDatabase). The changelog run can fail with
        // a Liquibase exception as well as an SQLException, so each catch takes both. A role is released from the
        // holder table only if its own drop succeeded, and the error names only the roles that did not drop: a
        // role that is gone must not be reported as leaked, and one that stays must not lose its holder.
        List<String> dropped = new ArrayList<>();
        List<String> failed = new ArrayList<>();
        Exception first = null;
        try (Connection admin = SharedCluster.controlConnection(delegate)) {
            admin.setAutoCommit(true);
            for (String role : exclusiveRoles) {
                try {
                    PgContainerHelper.dropRoleRevokingGrants(admin, role);
                    dropped.add(role);
                } catch (Exception e) {
                    failed.add(role);
                    if (first == null) {
                        first = e;
                    } else {
                        first.addSuppressed(e);
                    }
                }
            }
        } catch (Exception e) {
            // no control connection (or its close failed): the roles not yet dropped are still on the cluster
            for (String role : exclusiveRoles) {
                if (!dropped.contains(role) && !failed.contains(role)) {
                    failed.add(role);
                }
            }
            if (first == null) {
                first = e;
            } else {
                first.addSuppressed(e);
            }
        }
        SharedCluster.releaseExclusiveRoles(dropped, holder);
        if (!failed.isEmpty()) {
            LOG.error("nexus-7xfh7: {} could not drop its exclusive role(s) {} on the shared cluster; every later "
                + "class that declares one of them fails at acquire until this is fixed", holder, failed, first);
            throw new IllegalStateException("nexus-7xfh7: " + holder + " leaked exclusive role(s) " + failed
                + " on the shared cluster (DROP ROLE failed: " + first.getMessage() + "). Later classes that declare "
                + "them will fail at acquire and name this class.", first);
        }
        if (first != null) {
            LOG.warn("nexus-7xfh7: {} dropped all its exclusive roles, but closing the control connection failed", holder, first);
        }
    }
}
