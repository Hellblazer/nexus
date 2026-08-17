// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Per-fork-JVM shared PostgreSQL cluster (nexus-yhmav): ONE {@link PostgreSQLContainer}
 * boot, reused across every test class in this surefire fork, with a fresh
 * {@code CREATE DATABASE ... TEMPLATE} clone handed out per class instead of a fresh
 * container boot per class.
 *
 * <p><b>Why this is safe (investigation findings, recorded here so the next reader
 * does not have to re-derive them):</b>
 * <ul>
 *   <li><b>Roles are cluster-wide, but every ordinary test class's role bootstrap is
 *       already idempotent</b> ({@code DO $$ BEGIN IF NOT EXISTS (...) THEN CREATE ROLE
 *       ... END IF; END $$}) -- a repo-wide sweep (2026-08-09) found every bare,
 *       non-guarded {@code CREATE ROLE} confined to exactly the classes that opt out via
 *       {@link PgContainerHelper#startDedicated()} (plus one single-use role name in
 *       {@code Catalog013RlsReplayTest} that is created at most once per fork lifetime
 *       and therefore never collides). Re-running an idempotent role bootstrap against
 *       an already-bootstrapped cluster is a harmless no-op. A SEPARATE sweep (also
 *       2026-08-09, added after a full-suite run caught {@code
 *       ServiceTokenScopeBackfillTest} colliding on a bare {@code CREATE TABLE}) covers
 *       the same hazard expressed via schema DDL instead of roles: any class hand-
 *       building a table that also exists in the master changelog, or applying a NAMED
 *       changelog file instead of {@code db.changelog-master.xml}, needs the same
 *       opt-out -- see {@link PgContainerHelper#startDedicated()}'s roster for the
 *       full, current list and each entry's specific reason.</li>
 *   <li><b>Liquibase's own {@code update()} is idempotent</b> against an
 *       already-migrated database (its {@code DATABASECHANGELOG} table -- itself part
 *       of the template clone -- makes every changeset a skip). A class's own
 *       {@code startAll()} re-running the master changelog against a freshly cloned,
 *       already-migrated database costs a changelog-table scan, not real DDL.</li>
 *   <li><b>Advisory locks and {@code ALTER DATABASE ... SET} are PostgreSQL
 *       database-scoped</b>, not cluster-scoped ({@code pg_locks}/{@code pg_advisory_*}
 *       key on the connected database's OID) -- sweep-gate tests
 *       ({@code RekeyOpsIntegrationTest}, {@code IndexRunFenceTest}) and
 *       {@code DenseGateScanBudgetIntegrationTest}'s {@code ALTER DATABASE "<dbname>"
 *       SET hnsw.max_scan_tuples} are automatically isolated per class by the database
 *       boundary this design already gives every class. <b>{@code ALTER ROLE ... SET}
 *       (no {@code IN DATABASE} clause) is NOT in that category</b> (nexus-tyiht,
 *       substantive-critic 2026-08-09): it writes a cluster-wide
 *       {@code pg_db_role_setting} row ({@code setdatabase=0}) that template cloning
 *       neither copies nor resets, and ~90 test files issue exactly that statement for
 *       their service role's {@code search_path}. The database boundary does NOT
 *       isolate it -- so {@link #acquireDatabase()} enforces isolation itself: before
 *       handing out each clone it RESETS every cluster-wide role-level setting,
 *       restoring the exact fresh-cluster baseline (the master changelog never issues
 *       {@code ALTER ROLE ... SET}; production sets {@code search_path} via
 *       {@code connectionInitSql} -- see {@code grants-nexus-svc.xml} and
 *       {@code Main.java} -- so zero role-level settings IS the virgin state). Each
 *       class's own {@code ALTER ROLE} lands after its acquire and survives for its
 *       own lifetime only. Falsified, not just argued:
 *       {@code SharedClusterMutationFalsifyTest} poison-writes a cluster-wide role
 *       setting and proves the next acquire clears it.</li>
 *   <li><b>Surefire runs test classes within one fork SEQUENTIALLY</b> (nexus-13eb0
 *       added {@code forkCount}, not same-JVM parallel execution) -- there is never
 *       more than one class's connections live against the shared cluster at a time,
 *       so a stale {@code pg_stat_activity} row from a class that failed to close its
 *       pool cannot race a DIFFERENT class's assertions (it can only stack onto ITS
 *       OWN teardown, which {@link SharedDatabaseHandle#stop()} handles defensively
 *       via {@code pg_terminate_backend} before the {@code DROP DATABASE}).</li>
 * </ul>
 *
 * <p><b>What is NOT safe, and therefore opted out</b> (via
 * {@link PgContainerHelper#startDedicated()}, the same "returns a real, independently
 * booted container" shape {@code PgContainerHelper.newContainer()} already gave
 * {@code PgBouncerTenantIsolationTest}): any class whose test IS the migration
 * PROCESS -- fresh-provisioning ownership semantics, changeset-count deltas,
 * execution-order/rollback depth, or a bare (non-idempotent) {@code CREATE ROLE} --
 * rather than just using an already-migrated schema as a fixture. See
 * {@link PgContainerHelper#startDedicated()}'s javadoc for the roster and the
 * per-class reason.
 */
final class SharedCluster {

    private static final Logger LOG = LoggerFactory.getLogger(SharedCluster.class);

    /** Dedicated template database, migrated exactly once per fork, then only ever
     *  read as a {@code CREATE DATABASE ... TEMPLATE} source -- never connected to
     *  again, so it is never blocked by PostgreSQL's "template db must have no other
     *  connections" rule for {@code CREATE DATABASE ... TEMPLATE}. */
    private static final String TEMPLATE_DB = "nexus_template";

    private static volatile PostgreSQLContainer<?> CONTAINER;
    private static final Object BOOTSTRAP_LOCK = new Object();
    private static final AtomicInteger DB_COUNTER = new AtomicInteger();

    private SharedCluster() {}

    /** Allocate a fresh, already-migrated per-class database on this fork's shared
     *  cluster, cloned from {@link #TEMPLATE_DB}. Boots and migrates the cluster on
     *  first call in this fork JVM; every subsequent call in the same fork just clones.
     *
     *  <p><b>Constraint -- one acquire per class, or re-issue role settings after the
     *  last acquire (nexus-tyiht follow-up):</b> the cluster-wide role-GUC reset below
     *  fires on EVERY acquire. A class that acquires a second handle mid-class
     *  therefore wipes its OWN earlier {@code ALTER ROLE ... SET} -- live connections
     *  keep their session values, but any connection opened after the second acquire
     *  silently loses them. Audited 2026-08-09: no shared-roster class both acquires
     *  twice and issues {@code ALTER ROLE} (the only multi-acquire classes,
     *  {@code TaxonomySchemaLiquibaseTest} / {@code CatalogSchemaLiquibaseTest}, use
     *  strictly sequential try-with-resources handles and set no role GUCs at all).
     *  A future class that needs both must re-issue its {@code ALTER ROLE} after its
     *  final acquire. */
    static SharedDatabaseHandle acquireDatabase() throws SQLException {
        PostgreSQLContainer<?> c = ensureBootstrapped();
        String dbName = "nexus_test_" + DB_COUNTER.incrementAndGet();
        try (Connection admin = controlConnection(c)) {
            admin.setAutoCommit(true);
            try (var st = admin.createStatement()) {
                // nexus-tyiht: cluster-wide role-level GUCs (ALTER ROLE ... SET with no
                // IN DATABASE clause; pg_db_role_setting rows with setdatabase=0) are
                // the one state class bootstraps routinely write that the database
                // boundary does NOT isolate. Reset them ALL before handing out the
                // clone: zero cluster-wide role settings is the exact virgin-cluster
                // baseline (the changelog never issues ALTER ROLE ... SET), so every
                // class starts from the same slate a fresh pre-yhmav container gave it,
                // and a predecessor class's ALTER ROLE can never bleed forward.
                st.execute("DO $$ DECLARE r record; BEGIN "
                    + "  FOR r IN SELECT p.rolname FROM pg_db_role_setting s "
                    + "           JOIN pg_roles p ON p.oid = s.setrole "
                    + "           WHERE s.setdatabase = 0 LOOP "
                    + "    EXECUTE format('ALTER ROLE %I RESET ALL', r.rolname); "
                    + "  END LOOP; "
                    + "END $$");
                st.execute("CREATE DATABASE \"" + dbName + "\" TEMPLATE \"" + TEMPLATE_DB + "\"");
            }
        }
        return new SharedDatabaseHandle(c, dbName);
    }

    /** Raw JDBC connection to {@code dbName} on the shared container, bypassing
     *  Testcontainers' {@code createConnection} entirely (no per-container config
     *  object needed -- just host/port from the already-running container). Package-
     *  visible: {@link SharedDatabaseHandle#stop()} uses this to reach the control
     *  database ({@link PgContainerHelper#DATABASE}) for its DROP DATABASE. */
    static Connection controlConnection(PostgreSQLContainer<?> c) throws SQLException {
        return rawConnect(c, PgContainerHelper.DATABASE);
    }

    private static Connection rawConnect(PostgreSQLContainer<?> c, String dbName) throws SQLException {
        String url = "jdbc:postgresql://" + c.getHost() + ":" + c.getMappedPort(PostgreSQLContainer.POSTGRESQL_PORT)
            + "/" + dbName + "?sslmode=disable";
        return DriverManager.getConnection(url, PgContainerHelper.USERNAME, PgContainerHelper.PASSWORD);
    }

    private static PostgreSQLContainer<?> ensureBootstrapped() throws SQLException {
        PostgreSQLContainer<?> c = CONTAINER;
        if (c != null) {
            return c;
        }
        synchronized (BOOTSTRAP_LOCK) {
            if (CONTAINER != null) {
                return CONTAINER;
            }
            long start = System.nanoTime();
            PostgreSQLContainer<?> fresh = PgContainerHelper.newContainer();
            fresh.start();

            // Everything after start() runs under a catch-all: if template creation or
            // the Liquibase migration fails, the just-booted container would otherwise
            // leak (CONTAINER is only assigned on full success, so nothing would ever
            // stop it; Ryuk reaps eventually, but only at JVM exit). Stop it eagerly
            // and rethrow (code-review-expert finding, nexus-yhmav gate 2026-08-09).
            try {
                try (Connection su = rawConnect(fresh, PgContainerHelper.DATABASE)) {
                    su.setAutoCommit(true);
                    try (var st = su.createStatement()) {
                        st.execute("CREATE DATABASE \"" + TEMPLATE_DB + "\"");
                        // nexus_svc is NOT pre-created here (nexus-v80f2, 2026-08-15, critic
                        // finding S2 -- was previously a redundant `CREATE ROLE ... IF NOT
                        // EXISTS` without NOINHERIT). role-001-nexus-svc.xml is documented as
                        // the FIRST include in the master changelog, so its own idempotent
                        // CREATE ROLE (which DOES carry NOINHERIT) is guaranteed to run before
                        // any later changeset that needs nexus_svc to exist -- this pre-create
                        // was an optimization only (per the removed comment: "not a correctness
                        // requirement"), never load-bearing. Keeping it meant two independent
                        // CREATE ROLE literals had to be hand-kept in lockstep; the shared-
                        // cluster copy silently omitting NOINHERIT is exactly the drift class
                        // that produced this finding -- since this pre-create ran first, it
                        // won the IF-NOT-EXISTS race and role-001's NOINHERIT clause never
                        // executed against this template at all. Removing the duplicate
                        // makes role-001-nexus-svc.xml the SOLE source of truth for every
                        // shared-cluster test: dropping NOINHERIT from its CREATE ROLE now
                        // flips CatalogSchemaLiquibaseTest#nexusSvcRoleIsNoinherit red, where
                        // before it would have had no effect on this path whatsoever.
                    }
                }

                // Migrate TEMPLATE_DB itself, once. Connect directly to it (not the
                // control "postgres" db) so DATABASECHANGELOG and every migrated object
                // land in the template that CREATE DATABASE ... TEMPLATE will copy.
                try (Connection tsu = rawConnect(fresh, TEMPLATE_DB)) {
                    tsu.setAutoCommit(true);
                    var lb = new Liquibase(
                        "db/changelog/db.changelog-master.xml",
                        new ClassLoaderResourceAccessor(),
                        DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                            new JdbcConnection(tsu)));
                    lb.update(new Contexts());
                } catch (Exception e) {
                    throw new SQLException("nexus-yhmav shared-cluster template migration failed", e);
                }
            } catch (RuntimeException | SQLException e) {
                try {
                    fresh.stop();
                } catch (RuntimeException stopFailure) {
                    e.addSuppressed(stopFailure);
                }
                throw e;
            }

            LOG.info("nexus-yhmav shared PG cluster bootstrapped for this fork in {}ms",
                (System.nanoTime() - start) / 1_000_000);
            CONTAINER = fresh;
            return fresh;
        }
    }
}
