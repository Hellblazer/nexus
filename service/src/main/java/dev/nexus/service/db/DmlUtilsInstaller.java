// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.exception.LiquibaseException;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * nexus-f3yqd.2.11 (RDR-246 Step 4, Install sequence steps 2 and 3): applies the vendored {@code ddl_utils} and
 * {@code dml_utils} changelogs at boot, then lets the nexus walk run, and never fails a boot over them.
 *
 * <p>Called by {@link SchemaMigrator#migrate} once it holds the migration lock and has decided about the index
 * builders, on the migration connection, before the nexus changelog. It checks that {@code pg_background} is
 * installed, the three library roles exist, the engine runs with a separate admin role (the single-role posture
 * has no {@code dml_utils_owner} grant to use), the migration role can {@code SET ROLE dml_utils_owner}, and the
 * {@code liquibase} schema is absent or owned by that role. When all hold it sets the role, walks the
 * {@code ddl_utils} master and then the {@code dml_utils} master, and resets the role, so every library object and
 * both libraries' tracking tables (schema {@code liquibase}) belong to {@code dml_utils_owner} while the migration
 * login stays the only credential. The role is reset BEFORE the nexus walk: vectors-029's owner-read policy binds
 * {@code CURRENT_USER}.
 *
 * <p>Anything missing or failing is logged and kept as a {@link Status} the future {@code DataMoveWorker} reads
 * through {@link #status()}, to report every open move {@code blocked:}; the nexus walk runs regardless. The one
 * exception is a connection that cannot leave {@code dml_utils_owner}: the nexus walk would then create its objects
 * under the wrong owner, so that is a {@link SchemaMigrator.MigrationException}.
 *
 * <p><b>Index builders.</b> The library changesets are walked by Liquibase objects of their own, so they are never
 * in the {@code unrun} list {@link SchemaMigrator} counts schema work from, and a boot whose only pending work is a
 * library install keeps a running index build. That is safe because the library changelogs create objects in their
 * own schemas ({@code ddl_utils*}, {@code dml_utils*}, {@code liquibase}) and take no lock on any {@code nexus}
 * relation.
 *
 * <p><b>The connection is shared.</b> {@code Liquibase.close()} closes the JDBC connection (Phase 1 Step 3b, T2
 * nexus_rdr/246-research-7), so the library walks neither close their {@code Liquibase} nor their {@code Database}:
 * the connection is closed once, by the caller.
 *
 * <p><b>Frozen path.</b> A changeset's filename in the libraries' tracking tables is its resource path, so the
 * {@code db/vendor/<library>/} prefix is part of every applied changeset's identity; {@code
 * DmlUtilsInstallerTest} pins it.
 */
public final class DmlUtilsInstaller {

    private static final Logger log = LoggerFactory.getLogger(DmlUtilsInstaller.class);

    static final String EXTENSION = "pg_background";
    static final String OWNER_ROLE = "dml_utils_owner";
    static final String DML_CALLER_ROLE = "dml_utils_caller";
    static final String DDL_CALLER_ROLE = "ddl_utils_caller";
    static final List<String> LIBRARY_ROLES = List.of(OWNER_ROLE, DML_CALLER_ROLE, DDL_CALLER_ROLE);
    /** Where both libraries keep their tracking tables. */
    static final String LIBRARY_SCHEMA = "liquibase";

    /** One vendored library: its master changelog and its two tracking tables in {@link #LIBRARY_SCHEMA}. */
    record Library(String name, String master, String historyTable, String lockTable) {
        static Library of(String name) {
            return new Library(name, "db/vendor/" + name + "/db.changelog-master.xml",
                name + "_databasechangelog", name + "_databasechangeloglock");
        }
    }

    /** Apply order of the spike (T2 nexus_rdr/246-research-7) and RDR-246: ddl_utils first, dml_utils second. */
    static final List<Library> LIBRARIES = List.of(Library.of("ddl_utils"), Library.of("dml_utils"));

    private static final int REASON_LIMIT = 500;

    /**
     * {@code lock_timeout} for the role window: a library statement queued behind another session's lock (a leftover
     * pg_background worker, a long transaction) ends with 55P03 and the install is FAILED, instead of every boot
     * hanging on it. The session's own value comes back when the window closes.
     */
    static final String LIBRARY_LOCK_TIMEOUT = "5s";

    /**
     * Test seam: runs just before each library's real walk, on the migration connection, inside the role window.
     * {@code DmlUtilsInstallIntegrationTest} injects an {@link Error} through it. Public for the cross-package reason
     * {@link SchemaMigrator#migrate(javax.sql.DataSource, Runnable)} gives: that test lives in {@code dev.nexus.service}
     * with the container fixtures it shares, one package above this class; nothing in production reads it.
     */
    @FunctionalInterface
    public interface LibraryWalkHook {
        void beforeWalk(Connection conn, String library);
    }

    /** The install states. {@link #NOT_RUN} until a walk reaches the installer. */
    public enum State { NOT_RUN, APPLIED, SKIPPED, FAILED }

    /**
     * What the last install pass did, for the move worker. {@code reason} is null when {@link State#APPLIED}, and
     * one line otherwise. {@code sessionRoleAfter} is {@code current_user} read right after the role reset, null
     * when the pass never set the role.
     */
    public record Status(State state, String reason, String sessionRoleAfter) {
        public boolean applied() {
            return state == State.APPLIED;
        }

        /** The text a move reports after {@code blocked:}, or empty when the libraries are in place. */
        public Optional<String> blockedReason() {
            return applied() ? Optional.empty()
                : Optional.of(state == State.NOT_RUN ? "the library install has not run" : reason);
        }
    }

    private final boolean separateAdminRole;
    private final LibraryWalkHook hook;
    private volatile Status status = new Status(State.NOT_RUN, null, null);

    /**
     * @param separateAdminRole false when the engine runs on one database role (no {@code NX_DB_ADMIN_*}); that
     *                          role holds no {@code dml_utils_owner} grant, so the libraries are skipped
     */
    public DmlUtilsInstaller(boolean separateAdminRole) {
        this(separateAdminRole, (conn, library) -> { });
    }

    private DmlUtilsInstaller(boolean separateAdminRole, LibraryWalkHook hook) {
        this.separateAdminRole = separateAdminRole;
        this.hook = hook;
    }

    /**
     * The installer an engine boots with: the libraries are applied only when the admin connection (the migration
     * login) is not the role that serves traffic ({@code NX_DB_ADMIN_USER} differs from {@code NX_DB_USER}). One
     * role holds no {@code dml_utils_owner} grant. The one place that rule lives; {@code Main} calls it.
     */
    public static DmlUtilsInstaller forEngine(String adminUser, String serviceUser) {
        return new DmlUtilsInstaller(!adminUser.equals(serviceUser));
    }

    /** Test seam: as {@code new DmlUtilsInstaller(separateAdminRole)}, with {@code hook} run before each library walk. */
    public static DmlUtilsInstaller withHookForTest(boolean separateAdminRole, LibraryWalkHook hook) {
        return new DmlUtilsInstaller(separateAdminRole, hook);
    }

    /** Whether this installer was built for an engine with a separate admin role. */
    public boolean hasSeparateAdminRole() {
        return separateAdminRole;
    }

    /** The outcome of the latest {@link #install}; {@link State#NOT_RUN} before the first. */
    public Status status() {
        return status;
    }

    /**
     * One install pass on {@code conn}, the migration connection, with the migration lock held. Never throws for a
     * missing prerequisite or a failing library changelog.
     *
     * @throws SchemaMigrator.MigrationException when the connection cannot be returned to its own role
     */
    void install(Connection conn) {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        String skip;
        try {
            skip = skipReason(ctx);
            commit(conn);
        } catch (DataAccessException | SQLException e) {
            rollbackQuietly(conn);
            skip = "the install checks could not run: " + bounded(e.toString());
        }
        if (skip != null) {
            boolean expected = !separateAdminRole;
            status = new Status(State.SKIPPED, skip, null);
            if (expected) {
                log.info("event=data_move_libraries_skipped reason=\"{}\"", skip);
            } else {
                log.warn("event=data_move_libraries_skipped reason=\"{}\"", skip);
            }
            return;
        }

        String failure = null;
        String roleAfter = null;
        String priorLockTimeout = null;
        try {
            try {
                setRole(conn, OWNER_ROLE);
                priorLockTimeout = setting(ctx, "lock_timeout");
                setSetting(conn, "lock_timeout", LIBRARY_LOCK_TIMEOUT);
                createLibrarySchema(ctx, conn);
                for (Library library : LIBRARIES) {
                    hook.beforeWalk(conn, library.name());
                    walk(conn, library);
                }
            } catch (LiquibaseException | SQLException | RuntimeException | StackOverflowError e) {
                failure = failed(conn, e);
            } catch (VirtualMachineError e) {
                throw e; // an OutOfMemoryError or InternalError is not a library failure
            } catch (Error e) {
                // LinkageError and ExceptionInInitializerError, and the native image's MissingReflectionRegistrationError:
                // the vendored changelogs use changeset forms nexus's own do not, so a native-only Error is plausible.
                failure = failed(conn, e);
            }
        } finally {
            // Whatever ended the window, the connection leaves it as it entered: the nexus walk runs as the login.
            try {
                restoreLockTimeout(conn, priorLockTimeout);
            } finally {
                roleAfter = resetRole(conn, ctx);
            }
        }
        status = failure == null ? new Status(State.APPLIED, null, roleAfter)
            : new Status(State.FAILED, "a library changelog failed: " + failure, roleAfter);
    }

    /** Logs a library failure, abandons the open transaction, and returns the one-line reason. */
    private static String failed(Connection conn, Throwable e) {
        String reason = bounded(e.toString());
        log.error("event=data_move_libraries_failed reason=\"{}\"", reason, e);
        rollbackQuietly(conn);
        return reason;
    }

    /** The first failed prerequisite, as one line, or null when every one holds. */
    private String skipReason(DSLContext ctx) {
        if (!separateAdminRole) {
            return "no separate admin role (NX_DB_ADMIN_* unset): the engine's one role holds no "
                + OWNER_ROLE + " grant";
        }
        boolean extension = ctx.fetchExists(DSL.selectOne()
            .from(DSL.table(DSL.name("pg_catalog", "pg_extension")))
            .where(DSL.field(DSL.name("extname"), String.class).eq(EXTENSION)));
        if (!extension) {
            return EXTENSION + " is not installed";
        }
        List<String> present = ctx.select(DSL.field(DSL.name("rolname"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_roles")))
            .where(DSL.field(DSL.name("rolname"), String.class).in(LIBRARY_ROLES))
            .fetch(0, String.class);
        List<String> missing = new ArrayList<>(LIBRARY_ROLES);
        missing.removeAll(present);
        if (!missing.isEmpty()) {
            return "library role " + String.join(", ", missing) + " is missing";
        }
        String schemaOwner = ctx.select(DSL.function("pg_get_userbyid", SQLDataType.VARCHAR,
                DSL.field(DSL.name("nspowner"))))
            .from(DSL.table(DSL.name("pg_catalog", "pg_namespace")))
            .where(DSL.field(DSL.name("nspname"), String.class).eq(LIBRARY_SCHEMA))
            .fetchOne(0, String.class);
        if (schemaOwner != null && !OWNER_ROLE.equals(schemaOwner)) {
            return LIBRARY_SCHEMA + " schema owned by " + schemaOwner;
        }
        Boolean canSetRole = ctx.select(DSL.function("pg_has_role", SQLDataType.BOOLEAN,
                DSL.currentUser(), DSL.val(OWNER_ROLE), DSL.val("SET")))
            .fetchOne(0, Boolean.class);
        if (!Boolean.TRUE.equals(canSetRole)) {
            return "the migration role cannot SET ROLE " + OWNER_ROLE;
        }
        return null;
    }

    /**
     * {@code liquibase} may be absent (the bootstrap makes it, but a bare cluster may lack it): create it as the owner.
     * Only when absent: PostgreSQL checks CREATE on the database before it looks at IF NOT EXISTS, so the statement
     * would fail for an owner without that grant even though the schema is there.
     */
    private static void createLibrarySchema(DSLContext ctx, Connection conn) throws SQLException {
        boolean present = ctx.fetchExists(DSL.selectOne()
            .from(DSL.table(DSL.name("pg_catalog", "pg_namespace")))
            .where(DSL.field(DSL.name("nspname"), String.class).eq(LIBRARY_SCHEMA)));
        if (!present) {
            ctx.createSchema(DSL.name(LIBRARY_SCHEMA)).execute();
        }
        commit(conn);
    }

    /**
     * Walks one library's master. Neither the {@link Liquibase} nor its {@link Database} is closed, see the class
     * comment. A locked tracking-table row is stale under the migration lock, as for nexus's own.
     */
    private static void walk(Connection conn, Library library) throws LiquibaseException {
        Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new LibraryJdbcConnection(conn));
        database.setLiquibaseSchemaName(LIBRARY_SCHEMA);
        database.setDatabaseChangeLogTableName(library.historyTable());
        database.setDatabaseChangeLogLockTableName(library.lockTable());
        MigrationLock.releaseStaleChangelogLock(database);
        Liquibase liquibase = new Liquibase(library.master(), new ClassLoaderResourceAccessor(), database);
        int pending = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression()).size();
        SchemaMigrator.update(liquibase);
        log.info("event=data_move_library_applied library={} changesets={} master={}", library.name(), pending,
            library.master());
    }

    /**
     * The {@link JdbcConnection} a library's {@link Database} wraps. Liquibase keys its per-database caches (the lock
     * service, the history service, the executor) on the {@code Database}, and a {@code Database} is equal to another
     * whose connection is equal; two wrappers of one JDBC connection are equal, so a library walk on the same
     * connection as the nexus walk found the nexus {@code Database}'s lock service and read
     * {@code public.databasechangeloglock}. Identity equality keeps each walk's services its own. {@link #close()}
     * does nothing: the connection is the caller's, and Liquibase closes it with the {@code Database}.
     */
    private static final class LibraryJdbcConnection extends JdbcConnection {
        LibraryJdbcConnection(Connection conn) {
            super(conn);
        }

        @Override
        public boolean equals(Object other) {
            return this == other;
        }

        @Override
        public int hashCode() {
            return System.identityHashCode(this);
        }

        @Override
        public void close() {
            // the connection stays open for the nexus walk
        }
    }

    /**
     * {@code SET ROLE <role>}, as {@code set_config('role', ..., false)} (the same GUC, session scoped), committed:
     * a rolled-back transaction would otherwise undo it.
     */
    private static void setRole(Connection conn, String role) throws SQLException {
        setSetting(conn, "role", role);
    }

    private static String setting(DSLContext ctx, String name) {
        return ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val(name))).fetchOne(0, String.class);
    }

    /** {@code SET <name> = <value>} for the session, as {@code set_config(..., false)}, committed; a null value is {@code RESET}. */
    private static void setSetting(Connection conn, String name, String value) throws SQLException {
        try {
            DSL.using(conn, SQLDialect.POSTGRES)
                .select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value, SQLDataType.VARCHAR), DSL.val(false)))
                .fetchOne();
        } catch (DataAccessException e) {
            throw new SQLException("setting " + name + " failed: " + e.getMessage(), e);
        }
        commit(conn);
    }

    /**
     * Puts the session's {@code lock_timeout} back; the value is null when the window never got that far. When the
     * prior value cannot be set, the setting is reset to its default instead ({@code set_config(name, NULL, false)} is
     * {@code RESET}); when that fails too the connection still carries the window's short timeout, and nexus's
     * walk must not run under it, so that is a {@link SchemaMigrator.MigrationException}, as a role that cannot be
     * reset is.
     */
    private static void restoreLockTimeout(Connection conn, String prior) {
        if (prior == null) {
            return;
        }
        try {
            rollbackQuietly(conn);
            setSetting(conn, "lock_timeout", prior);
            return;
        } catch (SQLException e) {
            log.warn("event=data_move_libraries_lock_timeout_restore_failed prior={} cause=\"{}\"", prior, e.toString());
        }
        try {
            rollbackQuietly(conn);
            setSetting(conn, "lock_timeout", null);
        } catch (SQLException e) {
            throw new SchemaMigrator.MigrationException("the migration connection could not leave the library window's "
                + "lock_timeout of " + LIBRARY_LOCK_TIMEOUT + "; refusing to walk nexus's changelog under it", e);
        }
    }

    /**
     * {@code SET ROLE NONE} (never {@code RESET ROLE}), then the check that the session is its own user again.
     *
     * @return {@code current_user} after the reset
     */
    private static String resetRole(Connection conn, DSLContext ctx) {
        try {
            setRole(conn, "none");
            String current = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
            String session = ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR,
                DSL.val("session_authorization"))).fetchOne(0, String.class);
            commit(conn);
            if (!session.equals(current)) {
                throw new SchemaMigrator.MigrationException("the migration connection is still role " + current
                    + " (session user " + session + ") after SET ROLE NONE; refusing to walk nexus's changelog as it");
            }
            return current;
        } catch (SQLException | DataAccessException e) {
            throw new SchemaMigrator.MigrationException("the migration connection could not leave role "
                + OWNER_ROLE + "; refusing to walk nexus's changelog as it", e);
        }
    }

    /** Ends the open transaction; a connection in autocommit has none. */
    private static void commit(Connection conn) throws SQLException {
        if (!conn.getAutoCommit()) {
            conn.commit();
        }
    }

    private static void rollbackQuietly(Connection conn) {
        try {
            conn.rollback();
        } catch (SQLException e) {
            log.warn("event=data_move_libraries_rollback_failed cause=\"{}\"", e.toString());
        }
    }

    /** One bounded line for a log field and a status reason. */
    static String bounded(String text) {
        String line = String.join(" ", text.split("\\s+"));
        return line.length() <= REASON_LIMIT ? line : line.substring(0, REASON_LIMIT) + "...";
    }
}
