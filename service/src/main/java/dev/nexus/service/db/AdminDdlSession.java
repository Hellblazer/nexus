// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Properties;

/**
 * One connection on the engine's admin values ({@link AdminConnection}), the plumbing the operator-run constraint
 * steps share: the chunk-table constraint step (nexus-6pfgb.43) and the catalog TTL CHECK step (nexus-6pfgb.44).
 * Extracted from the first so the second does not copy it.
 *
 * <p>What a session gives: the connection (autocommit, a socket bound above the longest statement), the one-run
 * session advisory lock when it was opened to write, the migration-walk guard and the {@code lock_timeout} /
 * {@code statement_timeout} bounds ({@link #guard}), the state of named constraints from {@code pg_constraint}
 * ({@link #states}), and the one place DDL text reaches the server ({@link #ddl}). DDL is rendered by the server's
 * {@code format()} with {@code %I} and {@code %L}, so no identifier or value is concatenated in Java.
 *
 * <p>Never a startup changeset: a boot-time VALIDATE crash-loops an install whose rows differ from the measured one,
 * so DDL goes out only when an operator's route call sends it.
 */
public final class AdminDdlSession implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(AdminDdlSession.class);

    private static final String LOCK_TIMEOUT = "5s";
    /** Above the longest statement bound a caller sets, so the server gives up before the socket does. */
    private static final int SOCKET_TIMEOUT_SECONDS = 35 * 60;
    private static final int LOGIN_TIMEOUT_SECONDS = 30;

    /** SQLSTATE: lock_not_available (the 5 s lock_timeout tripped). */
    private static final String LOCK_NOT_AVAILABLE = "55P03";
    /** SQLSTATE: check_violation (a VALIDATE found a row that breaks the constraint). */
    private static final String CHECK_VIOLATION = "23514";
    /** SQLSTATE: insufficient_privilege. */
    private static final String INSUFFICIENT_PRIVILEGE = "42501";

    /** Why a call was refused; {@code status} is the HTTP status the handler answers. */
    public static final class Refused extends RuntimeException {
        private final int status;
        private final String reason;

        public Refused(int status, String reason, String message) {
            super(message);
            this.status = status;
            this.reason = reason;
        }

        public int status() {
            return status;
        }

        public String reason() {
            return reason;
        }
    }

    /** One constraint's state on its table. */
    public record State(String name, boolean present, Boolean validated, String definition) {}

    /**
     * What a run is, for the connection's name, its lock and the words of a refusal.
     *
     * @param applicationName the {@code ApplicationName} of the connection
     * @param lockKey         the session advisory lock one run holds (a bigint distinct from every other key)
     * @param runLabel        names the run in "another {runLabel} run holds the lock"
     * @param schema          the schema of the one table the run alters
     * @param table           the table the run alters
     * @param offenderCount   what a failed VALIDATE tells the operator to count the offenders with
     */
    public record Settings(String applicationName, long lockKey, String runLabel, String schema, String table,
                           String offenderCount) {
        String tableLabel() {
            return schema + "." + table;
        }
    }

    private final Settings settings;
    private final Connection conn;
    private final DSLContext ctx;
    private boolean locked;

    private AdminDdlSession(Settings settings, Connection conn) {
        this.settings = settings;
        this.conn = conn;
        this.ctx = DSL.using(conn, SQLDialect.POSTGRES);
    }

    /**
     * Open a session on the admin values. With {@code lock} the run lock is taken as well, and another holder is
     * {@code Refused} 409 {@code constraint_run_in_progress}.
     */
    public static AdminDdlSession open(AdminConnection admin, Settings settings, boolean lock) {
        Objects.requireNonNull(admin, "admin");
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", settings.applicationName());
        props.setProperty("connectTimeout", Integer.toString(BackendReaper.CONNECT_TIMEOUT_SECONDS));
        props.setProperty("loginTimeout", Integer.toString(LOGIN_TIMEOUT_SECONDS));
        props.setProperty("socketTimeout", Integer.toString(SOCKET_TIMEOUT_SECONDS));
        props.setProperty("tcpKeepAlive", "true");
        Connection conn;
        try {
            conn = DriverManager.getConnection(admin.url(), props);
            conn.setAutoCommit(true);
        } catch (SQLException e) {
            throw new Refused(503, "admin_connection_failed",
                "the constraint run could not open the admin connection: sqlstate=" + e.getSQLState());
        }
        AdminDdlSession s = new AdminDdlSession(settings, conn);
        if (lock) {
            try {
                s.lock();
            } catch (RuntimeException e) {
                s.close();
                throw e;
            }
        }
        return s;
    }

    /** The jOOQ context on this connection, for the caller's own typed reads. */
    public DSLContext ctx() {
        return ctx;
    }

    private void lock() {
        if (!Boolean.TRUE.equals(ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                DSL.val(settings.lockKey()))).fetchOne(0, Boolean.class))) {
            throw new Refused(409, "constraint_run_in_progress",
                "another " + settings.runLabel() + " run holds the lock; wait for it to finish");
        }
        locked = true;
    }

    /** Per-statement guards: no migration walk, a short lock wait, and the statement bound of this call. */
    public void guard(String statementTimeout) {
        if (SchemaMigrator.migrationLockHolderPid(ctx) >= 0) {
            throw new Refused(503, "migration_in_progress",
                "a schema migration holds its lock; run the constraint step after the engine has booted");
        }
        setSession("lock_timeout", LOCK_TIMEOUT);
        setSession("statement_timeout", statementTimeout);
    }

    public void setSession(String name, String value) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value),
            DSL.inline(false))).fetch();
    }

    /** The state of each of {@code names} on the run's table, in {@code names} order (absent ones not present). */
    public List<State> states(List<String> names) {
        String schema = settings.schema();
        String table = settings.table();
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
        Field<String> conname = DSL.field(DSL.name("con", "conname"), String.class);
        Field<Boolean> validated = DSL.field(DSL.name("con", "convalidated"), Boolean.class);
        Field<String> def = DSL.function("pg_get_constraintdef", SQLDataType.VARCHAR,
            DSL.field(DSL.name("con", "oid")));
        var rows = ctx.select(conname, validated, def)
            .from(con)
            .join(cls).on(DSL.field(DSL.name("cls", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
            .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
            .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("cls", "relname"), String.class).eq(table))
            .and(conname.in(names))
            .fetch();
        List<State> out = new ArrayList<>();
        for (String name : names) {
            State found = null;
            for (var r : rows) {
                if (name.equals(r.value1())) {
                    found = new State(name, true, r.value2(), r.value3());
                }
            }
            out.add(found != null ? found : new State(name, false, null, null));
        }
        return out;
    }

    /**
     * Refuse unless this session reads every row of the run's table: the role is a superuser or has BYPASSRLS, or a
     * PERMISSIVE policy FOR SELECT (or ALL) with the qual {@code true} applies to it (named, or PUBLIC, or a role it
     * inherits) and no RESTRICTIVE policy for SELECT (or ALL) with any other qual does (nexus-gy3vq: Postgres ANDs
     * every applicable restrictive policy onto the permissive ones, so one that is not {@code true} can hide rows
     * whatever the permissive policy admits). "true" means the qual as pg_policies renders it is the literal
     * {@code true}: an equivalent spelling such as {@code 1 = 1}, or a restrictive policy with no USING qual, is read
     * as narrowing and refused, which fails closed. Anything else reads the rows its (unset) tenant stamp allows,
     * which is none, and a table-wide count would report an empty table as clean.
     *
     * @param pointAt the end of the "Point NX_DB_ADMIN_* at ..." sentence of the permissive refusal: the role that
     *                reads every row of this table
     */
    public void requireUnrestrictedView(String pointAt) {
        String role = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
        var attrs = ctx.select(DSL.field(DSL.name("r", "rolsuper"), Boolean.class),
                DSL.field(DSL.name("r", "rolbypassrls"), Boolean.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_roles")).as("r"))
            .where(DSL.field(DSL.name("r", "rolname"), String.class).eq(role))
            .fetchOne();
        if (attrs != null && (Boolean.TRUE.equals(attrs.value1()) || Boolean.TRUE.equals(attrs.value2()))) {
            return;
        }
        String label = settings.tableLabel();
        for (var p : selectPolicies("RESTRICTIVE")) {
            if (!isTrue(p.value2()) && appliesToSession(p.value1())) {
                throw new Refused(503, "admin_cannot_see_all_tenants",
                    "the admin session role " + role + " is narrowed by the RESTRICTIVE policy " + p.value3()
                        + " on " + label + ", whose qual is not true, so the table-wide probe would not read every "
                        + "tenant's rows. Point NX_DB_ADMIN_* at a superuser or BYPASSRLS role, or drop the policy");
            }
        }
        for (var p : selectPolicies("PERMISSIVE")) {
            if (isTrue(p.value2()) && appliesToSession(p.value1())) {
                return;
            }
        }
        throw new Refused(503, "admin_cannot_see_all_tenants",
            "the admin session role " + role + " does not read every tenant's rows of " + label + ": it is neither "
                + "a superuser nor BYPASSRLS, and no permissive SELECT policy with a true qual applies to it, so "
                + "the table-wide probe would read zero rows and the constraint checks would pass over every "
                + "tenant. Point NX_DB_ADMIN_* at " + pointAt);
    }

    /** The run table's policies of {@code kind} (PERMISSIVE or RESTRICTIVE) that govern SELECT: roles, qual, name. */
    private org.jooq.Result<org.jooq.Record3<String[], String, String>> selectPolicies(String kind) {
        return ctx.select(DSL.field(DSL.name("p", "roles"), SQLDataType.CLOB.array()),
                DSL.field(DSL.name("p", "qual"), String.class),
                DSL.field(DSL.name("p", "policyname"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_policies")).as("p"))
            .where(DSL.field(DSL.name("p", "schemaname"), String.class).eq(settings.schema()))
            .and(DSL.field(DSL.name("p", "tablename"), String.class).eq(settings.table()))
            .and(DSL.field(DSL.name("p", "permissive"), String.class).eq(kind))
            .and(DSL.field(DSL.name("p", "cmd"), String.class).in("SELECT", "ALL"))
            .fetch();
    }

    /** True when a policy's qual, as pg_policies renders it, is the constant true. */
    private static boolean isTrue(String qual) {
        return "true".equalsIgnoreCase(qual == null ? "" : qual.replaceAll("[()\\s]", ""));
    }

    /** True when a policy naming {@code roles} applies to this session (PUBLIC, or a role it is or inherits). */
    private boolean appliesToSession(String[] roles) {
        if (roles == null) {
            return false;
        }
        for (String named : roles) {
            if ("public".equalsIgnoreCase(named) || memberOf(named)) {
                return true;
            }
        }
        return false;
    }

    /** True when the session role has the privileges of {@code named} (is it, or inherits it). */
    private boolean memberOf(String named) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("pg_has_role", SQLDataType.BOOLEAN, DSL.currentUser(),
                DSL.cast(DSL.val(named), SQLDataType.VARCHAR), DSL.inline("USAGE"))).fetchOne(0, Boolean.class));
    }

    /** Render {@code template} on the server (format() with %I, %L and %s) and send the text on this connection. */
    public void ddl(String template, List<Object> args) {
        Field<?>[] params = new Field<?>[args.size() + 1];
        params[0] = DSL.val(template);
        for (int i = 0; i < args.size(); i++) {
            Object a = args.get(i);
            params[i + 1] = a instanceof String[] arr
                ? DSL.val(arr, SQLDataType.CLOB.array()) : DSL.val((String) a);
        }
        String sql = ctx.select(DSL.function("format", SQLDataType.CLOB, params)).fetchOne(0, String.class);
        try {
            runDdl(sql);
        } catch (SQLException e) {
            throw refusal(e);
        }
    }

    // SANCTIONED RAW (RDR-236 Phase 4 Step 3, nexus-6pfgb.43; shared with nexus-6pfgb.44): ALTER TABLE ... ADD
    // CONSTRAINT ... NOT VALID and VALIDATE CONSTRAINT have no typed jOOQ form that takes a rendered CHECK expression.
    // The text is rendered by PostgreSQL's format() with %I and %L (ddl), so no identifier, key or prefix is
    // concatenated in Java. Plain JDBC Statement.execute, as PciBuilderSession does. Single-homed: ddl.
    private void runDdl(String sql) throws SQLException {
        try (Statement st = conn.createStatement()) {
            st.execute(sql);
        }
    }

    private Refused refusal(SQLException e) {
        String state = e.getSQLState();
        if (LOCK_NOT_AVAILABLE.equals(state)) {
            return new Refused(503, "lock_timeout",
                "the table lock was not available within " + LOCK_TIMEOUT + ". Earlier constraints of this run "
                    + "may already have landed (a run is several statements, none rolled back); a rerun is safe: "
                    + "add skips a constraint that exists and validate skips one that is valid");
        }
        if (CHECK_VIOLATION.equals(state)) {
            return new Refused(409, "check_violation",
                "VALIDATE found a row that breaks the constraint; it stays NOT VALID. Count the offenders with "
                    + "the " + settings.offenderCount() + " and fix them first");
        }
        if (INSUFFICIENT_PRIVILEGE.equals(state)) {
            return new Refused(503, "insufficient_privilege",
                "the admin role may not alter " + settings.tableLabel() + " (NX_DB_ADMIN_* must name its owner)");
        }
        log.warn("event=admin_ddl_failed run={} sqlstate={} cause=\"{}\"", settings.runLabel(), state, e.getMessage());
        return new Refused(500, "ddl_failed", "the constraint statement failed: sqlstate=" + state);
    }

    @Override
    public void close() {
        try {
            if (locked) {
                ctx.select(DSL.function("pg_advisory_unlock", SQLDataType.BOOLEAN, DSL.val(settings.lockKey())))
                    .fetchOne();
            }
        } catch (DataAccessException e) {
            log.debug("event=admin_ddl_unlock_failed run={} cause=\"{}\"", settings.runLabel(), e.toString());
        } finally {
            try {
                conn.close();
            } catch (SQLException e) {
                log.debug("event=admin_ddl_close_failed run={} cause=\"{}\"", settings.runLabel(), e.toString());
            }
        }
    }
}
