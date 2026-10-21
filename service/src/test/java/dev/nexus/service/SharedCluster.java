// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.ConnectException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Duration;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;

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
 *       of the template clone -- makes every run-once changeset a skip). The runAlways
 *       changesets (the grants blocks, self-heals) are NOT skipped: each replay re-issues
 *       its statements, which is why {@link PgContainerHelper#applyProductSchema} treats the
 *       first call on an untouched clone as a no-op (nexus-0ty8z) instead of replaying them.</li>
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
 *       neither copies nor resets. When this guard was written ~90 test files issued
 *       exactly that statement for their service role's {@code search_path}; as of
 *       nexus-cbo4a batch 9 none do (role-level search_path reliance is retired,
 *       RawSqlGateTest fails loud on the shape, and production sets no search_path at
 *       all -- every reference is schema-qualified), but the database boundary still
 *       does NOT isolate a cluster-wide role setting, so {@link #acquireDatabase()}
 *       enforces isolation itself: before handing out each clone it RESETS every
 *       cluster-wide role-level setting, restoring the exact fresh-cluster baseline
 *       (the master changelog never issues {@code ALTER ROLE ... SET}, so zero
 *       role-level settings IS the virgin state). Falsified, not just argued:
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
 * <p><b>What a template clone is NOT, and what a class that needs it takes instead.</b> A class whose
 * test IS the migration PROCESS -- fresh-provisioning ownership semantics, changeset-count deltas,
 * execution-order/rollback depth -- cannot start from a database already migrated to HEAD. Until
 * nexus-7xfh7 each such class booted a container of its own ({@link PgContainerHelper#startDedicated()});
 * it now takes {@link #acquireEmptyDatabase}, an unmigrated database on this same cluster, because the
 * database (not the container) is the boundary for schema, ownership, changelog history and every
 * relation. A bare {@code CREATE ROLE} under a fixed name is declared exclusive there. What still
 * needs its own container is cluster-level state: role membership and attributes, server settings,
 * the server log, an image, a network, a size. See {@link PgContainerHelper#startDedicated()}'s javadoc
 * for the roster and the per-class reason.
 */
final class SharedCluster {

    private static final Logger LOG = LoggerFactory.getLogger(SharedCluster.class);

    /** Dedicated template database, migrated exactly once per fork, then only ever
     *  read as a {@code CREATE DATABASE ... TEMPLATE} source -- never connected to
     *  again, so it is never blocked by PostgreSQL's "template db must have no other
     *  connections" rule for {@code CREATE DATABASE ... TEMPLATE}. */
    private static final String TEMPLATE_DB = "nexus_template";

    /** The owner-migrated twin of {@link #TEMPLATE_DB} (nexus-0ty8z): the same product schema walked by the
     *  non-superuser {@link PgContainerHelper#TEMPLATE_OWNER}, so every relation is owned by it, as
     *  production's nexus_admin owns them. Built on first use, then only ever a clone source. */
    private static final String OWNED_TEMPLATE_DB = "nexus_template_owned";

    /** How a clone is made: the server's default strategy (WAL_LOG), which logs every page of the template
     *  (about 7.5 MB) for each of ~430 clones a run. {@code STRATEGY FILE_COPY} writes about 56 times less WAL
     *  per clone but checkpoints twice per CREATE DATABASE and added about 1 min 17 s to a 2-fork main run
     *  (measured 2026-10-19, nexus-0ty8z); the data files are copied either way. Left at the default. */
    private static final String CLONE_CLAUSE = "TEMPLATE \"" + TEMPLATE_DB + "\"";
    private static final String OWNED_CLONE_CLAUSE = "TEMPLATE \"" + OWNED_TEMPLATE_DB + "\"";

    private static volatile PostgreSQLContainer<?> CONTAINER;
    private static volatile boolean OWNED_TEMPLATE_READY;
    private static final Object OWNED_TEMPLATE_LOCK = new Object();

    /** Clones of {@link #TEMPLATE_DB} that nothing has touched yet (nexus-0ty8z). A clone starts at the
     *  template's state: master changelog at HEAD, test objects installed, every runAlways grant already
     *  applied. {@link #takePristine} hands that fact to the FIRST {@code applyProductSchema} on it, which
     *  then has nothing to apply; any later call runs the changelog for real. The entry is removed by that call
     *  or, for a clone that never applies, by {@link SharedDatabaseHandle#stop()}. */
    private static final Set<String> PRISTINE_CLONES = ConcurrentHashMap.newKeySet();
    private static final Object BOOTSTRAP_LOCK = new Object();
    private static final AtomicInteger DB_COUNTER = new AtomicInteger();

    /** Exclusive role name to the class that last declared it (nexus-7xfh7), so a leaked role names its leaker. */
    private static final java.util.concurrent.ConcurrentHashMap<String, String> EXCLUSIVE_HOLDERS =
        new java.util.concurrent.ConcurrentHashMap<>();

    /** How long a connect waits for the container's published port to accept a connection.
     *  The observed colima window is milliseconds; 10 s bounds what a permanently refused
     *  port costs each rawConnect (review nexus/review-33prh-code suggestion 1). */
    private static final Duration CONNECT_DEADLINE = Duration.ofSeconds(10);
    private static final Duration CONNECT_RETRY_SLEEP = Duration.ofMillis(100);

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
        return createDatabase(CLONE_CLAUSE, false);
    }

    /** Allocate a fresh, EMPTY per-class database on this fork's shared cluster
     *  ({@code CREATE DATABASE ... TEMPLATE template0}): no {@code nexus} schema, no
     *  relations, only {@code plpgsql}. For a class that is itself the migration
     *  process (it walks the changelog from nothing, or part of the way, and asserts
     *  on the result) but needs no CLUSTER-level state of its own: the database is the
     *  isolation boundary, as it is for {@link #acquireDatabase()}. nexus-7xfh7.
     *
     *  <p>What the database boundary does NOT isolate is cluster-wide state: roles,
     *  role memberships and role-level settings. The role-setting reset runs here
     *  exactly as in {@link #acquireDatabase()}. A role the class creates must carry a
     *  name no other class creates (or be created {@code IF NOT EXISTS}). A class that
     *  must create a role under a name the migration itself fixes ({@code nexus_diag},
     *  say) declares it in {@code exclusiveRoles}: absent at acquire (an acquire that
     *  finds it fails loud and names it, so a leak surfaces at the next user instead of
     *  skewing its result), dropped by {@link SharedDatabaseHandle#stop()} after the
     *  database goes. The cluster also already holds {@code nexus_svc} and every other
     *  role the template migration made, which a virgin container would not, so a class
     *  whose assertion is "this role does not exist yet" stays dedicated. */
    static SharedDatabaseHandle acquireEmptyDatabase(String... exclusiveRoles) throws SQLException {
        return createDatabase("TEMPLATE template0", true, exclusiveRoles);
    }

    /** Allocate a fresh per-class database cloned from the OWNER-migrated template (nexus-0ty8z): the product
     *  schema at HEAD, walked by {@link PgContainerHelper#TEMPLATE_OWNER} (NOSUPERUSER, NOBYPASSRLS, owner of
     *  every relation it created), plus the {@code nexus_test} objects installed by that owner. For a class
     *  whose subject is NOT the walk but needs production's ownership shape (a SECURITY DEFINER function
     *  that must run as a role subject to row-level security, a leaf only its owner may alter).
     *
     *  <p>The template is built once per fork, on first call, so a fork that never asks pays nothing.
     *  Database-level privileges are not copied by {@code CREATE DATABASE ... TEMPLATE} (the owner's
     *  {@code GRANT CREATE ON DATABASE} is on the template, not the clone), so the caller re-runs the owner
     *  provisioning on the clone; {@link PgContainerHelper#startMigratedByOwner()} does. */
    static SharedDatabaseHandle acquireOwnedDatabase() throws SQLException {
        ensureOwnedTemplate();
        return createDatabase(OWNED_CLONE_CLAUSE, false);
    }

    /** True once per clone: for the first {@code applyProductSchema} on a {@code start()} clone, whatever was done to it before. */
    static boolean takePristine(String dbName) {
        return PRISTINE_CLONES.remove(dbName);
    }

    /** Whether the clone still holds its unspent first call (PristineCloneSkipTest). */
    static boolean isPristine(String dbName) {
        return PRISTINE_CLONES.contains(dbName);
    }

    /** Forget a dropped database's pristine entry, so a stopped, never-applied clone does not stay in the set. */
    static void forgetPristine(String dbName) {
        PRISTINE_CLONES.remove(dbName);
    }

    /** The shared cluster's container (booting and migrating the template on first use). */
    static PostgreSQLContainer<?> container() throws SQLException {
        return ensureBootstrapped();
    }

    private static SharedDatabaseHandle createDatabase(String templateClause, boolean empty, String... exclusiveRoles)
            throws SQLException {
        for (String role : exclusiveRoles) {
            if (!role.matches("[a-z_][a-z0-9_]*")) {
                throw new IllegalArgumentException("exclusive role names are plain lower-case identifiers, got: " + role);
            }
        }
        PostgreSQLContainer<?> c = ensureBootstrapped();
        String dbName = "nexus_test_" + DB_COUNTER.incrementAndGet();
        String holder = PgContainerHelper.callerClass();
        long startLsn;
        try (Connection admin = controlConnection(c)) {
            admin.setAutoCommit(true);
            startLsn = DdlCounts.walBytes(admin);
            requireRolesAbsent(admin, exclusiveRoles);
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
                st.execute("CREATE DATABASE \"" + dbName + "\" " + templateClause);
            }
        }
        if (empty && DdlCounts.enabled()) {
            // A template0 database carries no event trigger, so its nested DDL (a function's EXECUTE)
            // would go uncounted. nexus-sz651.
            installDdlProbe(c, dbName);
        }
        DdlCounts.noteDatabase(dbName, DdlCounts.callerClass(), empty, startLsn);
        if (templateClause.equals(CLONE_CLAUSE)) {
            PRISTINE_CLONES.add(dbName);
        }
        for (String role : exclusiveRoles) {
            EXCLUSIVE_HOLDERS.put(role, holder);
        }
        return new SharedDatabaseHandle(c, dbName, java.util.List.of(exclusiveRoles), holder);
    }

    /**
     * Install the DDL probe (the event trigger {@code DdlLogParser} counts nested DDL from) into one
     * empty database. Run inside a {@code DO} block so the install is neither a logged top-level
     * statement nor seen by the trigger it creates: the probe must not count itself. nexus-sz651.
     */
    private static void installDdlProbe(PostgreSQLContainer<?> c, String dbName) throws SQLException {
        String sqlInstall = DdlCounts.probeInstallBlock();
        try (Connection db = rawConnect(c, dbName)) {
            db.setAutoCommit(true);
            try (var st = db.createStatement()) {
                st.execute(sqlInstall);
            }
        }
    }

    /** Forget {@code holder} as the holder of {@code roles}, once its teardown dropped them. */
    static void releaseExclusiveRoles(java.util.List<String> roles, String holder) {
        for (String role : roles) {
            EXCLUSIVE_HOLDERS.remove(role, holder);
        }
    }

    /** Fail loud when a role a class declared exclusive is already on the cluster. */
    private static void requireRolesAbsent(Connection admin, String[] roles) throws SQLException {
        if (roles.length == 0) {
            return;
        }
        java.util.List<String> present = DSL.using(admin, SQLDialect.POSTGRES)
            .select(DSL.field(DSL.name("rolname"), String.class))
            .from(DSL.table(DSL.name("pg_roles")))
            .where(DSL.field(DSL.name("rolname"), String.class).in(roles))
            .fetch(0, String.class);
        if (!present.isEmpty()) {
            StringBuilder who = new StringBuilder();
            for (String role : present) {
                String holder = EXCLUSIVE_HOLDERS.get(role);
                who.append(who.length() == 0 ? "" : ", ").append(role).append(" <- ")
                    .append(holder != null ? "last declared exclusive by " + holder
                        : "not declared exclusive by any class in this fork (another class created it)");
            }
            throw new SQLException("nexus-7xfh7: role(s) " + present + " already exist on the shared cluster, but "
                + "this class declared them exclusive (it creates them itself) [" + who + "]. Another class created the "
                + "same name, or an earlier holder's teardown did not drop it; rename the role or fix the leak.");
        }
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
        return connectWithRetry(
            () -> DriverManager.getConnection(url, PgContainerHelper.USERNAME, PgContainerHelper.PASSWORD),
            c::isRunning, CONNECT_DEADLINE, CONNECT_RETRY_SLEEP);
    }

    /** One connect attempt; may throw {@link SQLException}. */
    @FunctionalInterface
    interface ConnectAttempt<T> {
        T connect() throws SQLException;
    }

    /**
     * Run {@code attempt}, retrying while the port is merely not reachable yet (nexus-33prh).
     *
     * <p>Why: {@code PostgreSQLContainer} readiness is log-only, so {@code start()} can return
     * before the host-side {@code localhost:<port>} forward exists. On a colima VM (Service CI on
     * hellmini-ci) lima sets that forward up asynchronously, and the bootstrap's first connect
     * to the fork's first container got {@code ECONNREFUSED} (develop 998c771de and 69d493b52).
     * Every other connect in this tree goes through {@link FailFastPostgreSQLContainer}'s retry
     * loop; the bootstrap used a bare {@code DriverManager.getConnection}.
     *
     * <p>Retries only when the cause chain holds a {@link ConnectException} (nothing is
     * listening yet), every {@code sleep}, until {@code deadline} passes or {@code running}
     * turns false. Anything else (auth SQLSTATE 28P01/28000, a server-side error) is rethrown at
     * once, so a real failure never waits out the deadline.
     */
    static <T> T connectWithRetry(ConnectAttempt<T> attempt, BooleanSupplier running,
                                  Duration deadline, Duration sleep) throws SQLException {
        long deadlineNanos = System.nanoTime() + deadline.toNanos();
        int attempts = 0;
        while (true) {
            attempts++;
            SQLException failure;
            try {
                return attempt.connect();
            } catch (SQLException e) {
                if (!hasConnectExceptionCause(e)) {
                    throw e;
                }
                failure = e;
            }
            if (!running.getAsBoolean()) {
                throw new SQLException("nexus-33prh: container is not running; stopped retrying the connect"
                    + " after " + attempts + " attempt(s): " + rootMessage(failure), failure);
            }
            if (System.nanoTime() - deadlineNanos >= 0) {
                throw new SQLException("nexus-33prh: published port did not become reachable within "
                    + deadline.toMillis() + "ms (" + attempts + " attempts): " + rootMessage(failure), failure);
            }
            try {
                Thread.sleep(sleep.toMillis());
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw new SQLException("nexus-33prh: interrupted while waiting for the published port: "
                    + rootMessage(failure), failure);
            }
        }
    }

    private static boolean hasConnectExceptionCause(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof ConnectException) {
                return true;
            }
            if (c.getCause() == c) {
                break;
            }
        }
        return false;
    }

    private static String rootMessage(Throwable t) {
        Throwable root = t;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        return root.getClass().getSimpleName() + ": " + root.getMessage();
    }

    /** The one statement that makes an empty, default-template database (a clone source). */
    private static void createBareDatabase(Connection su, String name) throws SQLException {
        try {
            DSL.using(su, SQLDialect.POSTGRES).createDatabase(DSL.name(name)).execute();
        } catch (DataAccessException e) {
            // Callers see the driver's SQLException, as the plain Statement.execute gave them.
            if (e.getCause() instanceof SQLException driver) {
                throw driver;
            }
            throw e;
        }
    }

    /**
     * Build {@link #OWNED_TEMPLATE_DB} once per fork: bootstrap the non-superuser owner, walk the product
     * changelog as that owner through {@link SchemaMigrator} (production's path), and install the
     * {@code nexus_test} objects as that owner (it owns {@code databasechangelog}, so the superuser must not
     * create it first; see {@link PgContainerHelper#installTestObjects}). The pool is closed and its backends
     * awaited gone before the first clone, because {@code CREATE DATABASE ... TEMPLATE} refuses a source that
     * has another session.
     */
    private static void ensureOwnedTemplate() throws SQLException {
        if (OWNED_TEMPLATE_READY) {
            return;
        }
        PostgreSQLContainer<?> c = ensureBootstrapped();
        synchronized (OWNED_TEMPLATE_LOCK) {
            if (OWNED_TEMPLATE_READY) {
                return;
            }
            long start = System.nanoTime();
            try (Connection admin = controlConnection(c)) {
                admin.setAutoCommit(true);
                createBareDatabase(admin, OWNED_TEMPLATE_DB);
            }
            try {
                try (Connection su = rawConnect(c, OWNED_TEMPLATE_DB)) {
                    su.setAutoCommit(true);
                    PgContainerHelper.bootstrapNonSuperuserOwner(su, PgContainerHelper.TEMPLATE_OWNER,
                        PgContainerHelper.TEMPLATE_OWNER_PASSWORD);
                }
                var cfg = new HikariConfig();
                cfg.setJdbcUrl("jdbc:postgresql://" + c.getHost() + ":"
                    + c.getMappedPort(PostgreSQLContainer.POSTGRESQL_PORT) + "/" + OWNED_TEMPLATE_DB + "?sslmode=disable");
                cfg.setUsername(PgContainerHelper.TEMPLATE_OWNER);
                cfg.setPassword(PgContainerHelper.TEMPLATE_OWNER_PASSWORD);
                cfg.setMaximumPoolSize(2);
                try (HikariDataSource owner = new HikariDataSource(cfg)) {
                    SchemaMigrator.migrate(owner);
                    try (Connection oc = owner.getConnection()) {
                        PgContainerHelper.installTestObjectsAfterProductSchema(oc);
                    }
                }
                Runnable fault = ownedTemplateBuildFault;
                if (fault != null) {
                    fault.run();
                }
                awaitNoSessions(c, OWNED_TEMPLATE_DB);
            } catch (Exception e) {
                // A half-built template would make every later owner class die on "already exists" at the
                // CREATE DATABASE above, hiding this failure behind a worse message. Drop it so the next
                // acquire rebuilds from nothing.
                SQLException failure = new SQLException("nexus-0ty8z owner-migrated template build failed", e);
                try {
                    dropOwnedTemplate(c);
                } catch (SQLException dropFailure) {
                    failure.addSuppressed(dropFailure);
                    LOG.error("nexus-0ty8z could not drop the half-built {}; every later owner acquire will fail",
                        OWNED_TEMPLATE_DB, dropFailure);
                }
                throw failure;
            }
            OWNED_TEMPLATE_READY = true;
            LOG.info("nexus-0ty8z owner-migrated template built for this fork in {}ms",
                (System.nanoTime() - start) / 1_000_000);
        }
    }

    /** Drop {@link #OWNED_TEMPLATE_DB} (ending any backend still on it); a no-op when it does not exist. */
    private static void dropOwnedTemplate(PostgreSQLContainer<?> c) throws SQLException {
        try (Connection admin = controlConnection(c)) {
            dropDatabase(admin, OWNED_TEMPLATE_DB);
        }
    }

    /**
     * The one DROP DATABASE statement of this package's per-fork cluster: {@code WITH (FORCE)} (PostgreSQL 13+)
     * ends the remaining backends and drops in one statement, so there is no terminate-then-drop window for a
     * new connection to slip into. Autocommit is switched on, as the statement cannot run in a transaction block.
     */
    static void dropDatabase(Connection admin, String dbName) throws SQLException {
        admin.setAutoCommit(true);
        try (var st = admin.createStatement()) {
            st.execute("DROP DATABASE IF EXISTS \"" + dbName + "\" WITH (FORCE)");
        }
    }

    /** Test seam (SharedClusterOwnedTemplateRetryTest): forget the owner-migrated template, so the next acquire rebuilds it. */
    static void resetOwnedTemplate() throws SQLException {
        PostgreSQLContainer<?> c = ensureBootstrapped();
        synchronized (OWNED_TEMPLATE_LOCK) {
            dropOwnedTemplate(c);
            OWNED_TEMPLATE_READY = false;
        }
    }

    /** Test seam: runs after the owner walk and before the template is declared ready; null outside the retry test. */
    static volatile Runnable ownedTemplateBuildFault;

    /** Wait (bounded) for every backend on {@code dbName} to be gone: a pool's close returns before its backends exit. */
    private static void awaitNoSessions(PostgreSQLContainer<?> c, String dbName) throws SQLException {
        long deadline = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        try (Connection admin = controlConnection(c)) {
            var datname = DSL.field(DSL.name("datname"), String.class);
            while (true) {
                int open = DSL.using(admin, SQLDialect.POSTGRES).fetchCount(
                    DSL.table(DSL.name("pg_stat_activity")), datname.eq(dbName));
                if (open == 0) {
                    return;
                }
                if (System.nanoTime() - deadline >= 0) {
                    throw new SQLException("nexus-0ty8z: " + open + " session(s) still on " + dbName + " after 15 s");
                }
                try {
                    Thread.sleep(50);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    throw new SQLException("interrupted waiting for " + dbName + " to go idle", ie);
                }
            }
        }
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
            ((FailFastPostgreSQLContainer) fresh).ddlSharedCluster();
            fresh.start();

            // Everything after start() runs under a catch-all: if template creation or
            // the Liquibase migration fails, the just-booted container would otherwise
            // leak (CONTAINER is only assigned on full success, so nothing would ever
            // stop it; Ryuk reaps eventually, but only at JVM exit). Stop it eagerly
            // and rethrow (code-review-expert finding, nexus-yhmav gate 2026-08-09).
            try {
                try (Connection su = rawConnect(fresh, PgContainerHelper.DATABASE)) {
                    su.setAutoCommit(true);
                    createBareDatabase(su, TEMPLATE_DB);
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
                    // nexus-0ty8z: the nexus_test objects every class installs after the master walk
                    // (PgContainerHelper#applyProductSchema) are part of the template, so a clone that asks for
                    // them again finds them done. They are the same superuser-run changelog a class would run.
                    PgContainerHelper.installTestObjectsAfterProductSchema(tsu);
                    // nexus-sz651: DDL and WAL logged before this line are the template's
                    // migration; after it, the clones and the tests' own. Best effort: a missing
                    // mark leaves the run unmeasured (the ratchet refuses it), never fails the boot.
                    DdlCounts.markReady(tsu);
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
