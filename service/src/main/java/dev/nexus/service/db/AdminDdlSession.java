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
import java.util.function.Function;

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
    /**
     * The longest {@code statement_timeout} (or {@code lock_timeout}) a caller may set on this session, in milliseconds: a
     * minute under the socket timeout, so the server gives up before the client's read does and the caller sees the
     * server's SQLSTATE rather than a dropped socket. A configurable bound must be clamped to this.
     */
    public static final long MAX_STATEMENT_TIMEOUT_MS = (SOCKET_TIMEOUT_SECONDS - 60L) * 1000L;
    private static final int LOGIN_TIMEOUT_SECONDS = 30;

    /** SQLSTATE: lock_not_available (the 5 s lock_timeout tripped). */
    private static final String LOCK_NOT_AVAILABLE = "55P03";
    /** SQLSTATE: check_violation (a VALIDATE found a row that breaks the constraint). */
    private static final String CHECK_VIOLATION = "23514";
    /** SQLSTATE: insufficient_privilege. */
    private static final String INSUFFICIENT_PRIVILEGE = "42501";
    /** SQLSTATE: query_canceled (the statement_timeout tripped). */
    private static final String QUERY_CANCELED = "57014";

    /** Why a call was refused; {@code status} is the HTTP status the handler answers. */
    public static final class Refused extends RuntimeException {
        private final int status;
        private final String reason;
        private final String sqlState;

        public Refused(int status, String reason, String message) {
            this(status, reason, message, null);
        }

        /** {@code sqlState} is the SQLSTATE of the failed statement, or null when the refusal is not a statement's. */
        public Refused(int status, String reason, String message, String sqlState) {
            super(message);
            this.status = status;
            this.reason = reason;
            this.sqlState = sqlState;
        }

        /** The SQLSTATE of the statement that failed (55P03, 23514, 57014, ...), or null. */
        public String sqlState() {
            return sqlState;
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
    /**
     * The statement in flight on this session (a DDL, or any jOOQ query: {@link CancelTracker} records it), so another thread
     * (the engine's shutdown) can cancel it; null between statements.
     */
    private volatile Statement running;
    /** Set first by {@link #cancel}: a statement about to start sees it and does not start (see {@link #runDdl}). */
    private volatile boolean cancelled;
    /** The admin values this session was opened with (null for a session handed a connection), for {@link #cancel}'s fresh connection. */
    private AdminConnection admin;
    /** The server backend of this session's connection, or 0 when it is not known. */
    private int backendPid;
    /** When that backend started: with the pid it names this backend and no later one that reuses the pid. */
    private java.time.OffsetDateTime backendStart;
    /** Test seam: the admin values the backend cancel connects with, in place of {@link #admin} (an unreachable server). */
    volatile AdminConnection resendAdminForTests;
    /** The wall-clock bound of {@link #cancel}, in milliseconds (default {@value #DEFAULT_CANCEL_DEADLINE_MS}, 100 to 60000). */
    public static final String CANCEL_DEADLINE_PROPERTY = "nexus.validate.cancel_deadline_ms";
    static final long DEFAULT_CANCEL_DEADLINE_MS = 5_000L;

    /** Test seam: {@link #cancel} skips the driver's statement cancel, so a test can show the backend cancel alone lands. */
    volatile boolean statementCancelDisabledForTests;

    /** Package-private so a test can hand it a connection that fails where a real one rarely does. */
    AdminDdlSession(Settings settings, Connection conn) {
        this.settings = settings;
        this.conn = conn;
        this.ctx = DSL.using(new org.jooq.impl.DefaultConfiguration().set(conn).set(SQLDialect.POSTGRES)
            .set(new org.jooq.impl.DefaultExecuteListenerProvider(new CancelTracker())));
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
        props.setProperty("cancelSignalTimeout", "2");   // the driver's own cancel connection: seconds, not its default ten
        Connection conn;
        try {
            conn = DriverManager.getConnection(admin.url(), props);
            conn.setAutoCommit(true);
        } catch (SQLException e) {
            throw new Refused(503, "admin_connection_failed",
                "the constraint run could not open the admin connection: sqlstate=" + e.getSQLState());
        }
        AdminDdlSession s = new AdminDdlSession(settings, conn);
        s.admin = admin;
        try {
            var pid = DSL.field(DSL.name("pid"), Integer.class);
            var started = s.ctx.select(DSL.function("pg_backend_pid", SQLDataType.INTEGER),
                    DSL.field(DSL.name("backend_start"), SQLDataType.TIMESTAMPWITHTIMEZONE))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(pid.eq(DSL.function("pg_backend_pid", SQLDataType.INTEGER))).fetchOne();
            s.backendPid = started.value1();
            s.backendStart = started.value2();
        } catch (DataAccessException | NullPointerException e) {
            s.backendPid = 0;   // cancel() then relies on the driver's statement cancel alone
        }
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
        Refused refusal = unrestrictedViewRefusal(pointAt);
        if (refusal != null) {
            throw refusal;
        }
    }

    /** {@link #requireUnrestrictedView}'s decision, as the refusal it would throw (null: the session reads every row). */
    private Refused unrestrictedViewRefusal(String pointAt) {
        String role = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
        var attrs = ctx.select(DSL.field(DSL.name("r", "rolsuper"), Boolean.class),
                DSL.field(DSL.name("r", "rolbypassrls"), Boolean.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_roles")).as("r"))
            .where(DSL.field(DSL.name("r", "rolname"), String.class).eq(role))
            .fetchOne();
        if (attrs != null && (Boolean.TRUE.equals(attrs.value1()) || Boolean.TRUE.equals(attrs.value2()))) {
            return null;
        }
        String label = settings.tableLabel();
        for (var p : selectPolicies("RESTRICTIVE")) {
            if (!isTrue(p.value2()) && appliesToSession(p.value1())) {
                return new Refused(503, "admin_cannot_see_all_tenants",
                    "the admin session role " + role + " is narrowed by the RESTRICTIVE policy " + p.value3()
                        + " on " + label + ", whose qual is not true, so the table-wide probe would not read every "
                        + "tenant's rows. Point NX_DB_ADMIN_* at a superuser or BYPASSRLS role, or drop the policy");
            }
        }
        for (var p : selectPolicies("PERMISSIVE")) {
            if (isTrue(p.value2()) && appliesToSession(p.value1())) {
                return null;
            }
        }
        return new Refused(503, "admin_cannot_see_all_tenants",
            "the admin session role " + role + " does not read every tenant's rows of " + label + ": it is neither "
                + "a superuser nor BYPASSRLS, and no permissive SELECT policy with a true qual applies to it, so "
                + "the table-wide probe would read zero rows and the constraint checks would pass over every "
                + "tenant. Point NX_DB_ADMIN_* at " + pointAt);
    }

    /**
     * Run {@code read} where it sees every tenant's rows of the run's table, or refuse
     * ({@code admin_cannot_see_all_tenants}). Three ways to see them, tried in this order:
     * <ol>
     *   <li>the session already does ({@link #requireUnrestrictedView}: superuser, BYPASSRLS, or a true-qual
     *       permissive SELECT policy): {@code read} runs as it is, with no lock;</li>
     *   <li>the session role OWNS the table, as the production {@code nexus_admin} does. FORCE row level security
     *       is lifted for one transaction that is always rolled back, the precedent of
     *       {@code AspectQueueDrain.AdminCensus} (nexus-hbnml): {@code ALTER TABLE ... NO FORCE ROW LEVEL SECURITY}
     *       is transactional DDL, so the rollback puts FORCE back by construction and the table is never left
     *       unforced, whatever fails. No policy changeset is needed. <b>Cost:</b> that ALTER takes ACCESS EXCLUSIVE,
     *       which holds every reader and writer of the table until the rollback, so the transaction is bounded
     *       ({@code lock_timeout} 5 s to get the lock, the migration walk refused, {@code statement_timeout}
     *       {@code ownerStatementTimeout} for the read) and the read gives up, changing nothing, past it;</li>
     *   <li>otherwise the refusal {@link #requireUnrestrictedView} would throw.</li>
     * </ol>
     * A table that is not FORCE row-level-security is read by its owner as it is (the owner is exempt), with no ALTER.
     *
     * @param pointAt               the end of the refusal's "Point NX_DB_ADMIN_* at ..." sentence
     * @param ownerStatementTimeout the {@code statement_timeout} of the owner path: the longest the table lock is held
     * @param read                  the read, given this session's jOOQ context
     */
    public <T> T readEveryTenant(String pointAt, String ownerStatementTimeout, Function<DSLContext, T> read) {
        Refused refusal = unrestrictedViewRefusal(pointAt);
        if (refusal == null) {
            return read.apply(ctx);
        }
        Ownership ownership = ownership();
        if (ownership == null || !ownership.ownedBySession()) {
            throw refusal;
        }
        if (!ownership.forced()) {
            return read.apply(ctx);
        }
        guard(ownerStatementTimeout);   // the migration walk refused; lock_timeout and statement_timeout bounded
        try {
            conn.setAutoCommit(false);
        } catch (SQLException e) {
            throw refusal(e);
        }
        // from here a transaction is open (the ALTER is part of it), and every exit ends it with endUnforcedRead
        T result;
        try {
            ddl("ALTER TABLE %I.%I NO FORCE ROW LEVEL SECURITY", List.of(settings.schema(), settings.table()));
            result = read.apply(ctx);
        } catch (DataAccessException e) {
            RuntimeException thrown = e;
            if (QUERY_CANCELED.equals(e.sqlState())) {
                thrown = new Refused(503, "read_timeout",
                    "the table-wide read of " + settings.tableLabel() + " took longer than "
                        + ownerStatementTimeout + " under the table lock it needs as the table owner, and was "
                        + "stopped; nothing was changed. Run it in a quiet window, or point NX_DB_ADMIN_* at a "
                        + "superuser or BYPASSRLS role, which needs no lock");
            }
            endUnforcedRead(thrown);
            throw thrown;
        } catch (RuntimeException | Error e) {
            endUnforcedRead(e);
            throw e;
        }
        endUnforcedRead(null);
        return result;
    }

    /**
     * End the owner path's transaction: the rollback puts FORCE ROW LEVEL SECURITY back. If the rollback itself
     * fails, the connection is CLOSED, never returned to autocommit: {@code setAutoCommit(true)} on a live
     * connection COMMITS the open transaction, which would leave the table NO FORCE. Closing makes the server abort
     * it (the precedent is {@code AspectQueueDrain.AdminCensus}). The rollback failure never masks {@code inFlight}:
     * it is attached to it as suppressed; with nothing in flight it is the refusal.
     */
    private void endUnforcedRead(Throwable inFlight) {
        try {
            conn.rollback();
        } catch (SQLException e) {
            log.warn("event=admin_ddl_rollback_failed run={} cause=\"{}\" connection=closed", settings.runLabel(),
                e.toString());
            try {
                conn.close();
            } catch (SQLException closeFailure) {
                log.debug("event=admin_ddl_close_failed run={} cause=\"{}\"", settings.runLabel(),
                    closeFailure.toString());
            }
            if (inFlight == null) {
                throw refusal(e);
            }
            inFlight.addSuppressed(e);
            return;
        }
        try {
            conn.setAutoCommit(true);
        } catch (SQLException e) {
            log.warn("event=admin_ddl_autocommit_restore_failed run={} cause=\"{}\"", settings.runLabel(),
                e.toString());
        }
    }

    /** What the session role is to the run's table. */
    private record Ownership(boolean ownedBySession, boolean forced) {}

    /** The table's ownership (does the session role hold the owner's privileges) and FORCE flag; null if absent. */
    private Ownership ownership() {
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
        var row = ctx.select(
                DSL.function("pg_has_role", SQLDataType.BOOLEAN, DSL.currentUser(),
                    DSL.field(DSL.name("cls", "relowner")), DSL.inline("USAGE")),
                DSL.field(DSL.name("cls", "relforcerowsecurity"), Boolean.class))
            .from(cls)
            .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
            .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq(settings.schema()))
            .and(DSL.field(DSL.name("cls", "relname"), String.class).eq(settings.table()))
            .fetchOne();
        return row == null ? null : new Ownership(Boolean.TRUE.equals(row.value1()), Boolean.TRUE.equals(row.value2()));
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
        String sql;
        try {
            sql = ctx.select(DSL.function("format", SQLDataType.CLOB, params)).fetchOne(0, String.class);
        } catch (DataAccessException e) {
            if (QUERY_CANCELED.equals(e.sqlState()) && (cancelled || Thread.currentThread().isInterrupted())) {
                throw refusal(new SQLException("the statement was cancelled before it started", QUERY_CANCELED));
            }
            throw e;
        }
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
            running = st;
            // cancel() sets `cancelled` and then reads `running`; this sets `running` and then reads `cancelled`: either the
            // cancel sees the statement or this sees the cancel, so a stop that lands before the statement starts is seen
            // here (the engine's stop interrupts the thread before it cancels, so the interrupt flag says the same)
            if (cancelled || Thread.currentThread().isInterrupted()) {
                throw new SQLException("the statement was cancelled before it started", QUERY_CANCELED);
            }
            st.execute(sql);
        } finally {
            running = null;
        }
    }

    /** True once {@link #cancel} was called on this session: whatever it was about to do, it must not start. */
    public boolean isCancelled() {
        return cancelled;
    }

    /**
     * Cancels the statement in flight, DDL or query, if any, and makes every later statement of this session refuse to
     * start ({@link #isCancelled}, 57014). Safe from any thread; used by the engine's shutdown (synchronously), so a VALIDATE
     * does not outlive the engine that started it.
     *
     * <p>What it guarantees. A statement whose start comes after this sets the flag is refused before it reaches the server
     * (a DDL in {@link #runDdl}, a jOOQ query in {@link CancelTracker#executeStart}). A statement already past that check is
     * cancelled: first by the driver's cancel on its statement, then, while it is still the statement in flight, by {@code
     * pg_cancel_backend} on this session's backend from a fresh short-lived admin connection, re-sent every 100 ms. The
     * re-send is what covers a cancel that landed in the instant before the server started the statement (the driver sends a
     * statement's cancel once and PostgreSQL drops a signal that reaches an idle backend). It signals only the backend that
     * was recorded at open, by pid AND backend start time, so a pid the server has since reused for another session is never
     * signalled.
     *
     * <p>The whole of it is bounded by a WALL-CLOCK deadline, {@value #CANCEL_DEADLINE_PROPERTY} (default 5 s): each driver or
     * re-send call runs on a daemon thread this waits for only as long as the deadline has left, and a re-send's connection
     * gets connect, login and socket timeouts that fit in what is left, so a server that does not answer cannot hold the
     * engine's shutdown past it. At the deadline this gives up: that one statement runs to its own timeout (the only case not
     * covered), and every statement after it is refused.
     */
    public void cancel() {
        cancelled = true;
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(
            Math.max(100L, Math.min(60_000L, Long.getLong(CANCEL_DEADLINE_PROPERTY, DEFAULT_CANCEL_DEADLINE_MS))));
        for (int attempt = 0; ; attempt++) {
            Statement st = running;
            if (st == null) return;
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0) {
                log.warn("event=admin_ddl_cancel_gave_up run={} backend_pid={}", settings.runLabel(), backendPid);
                return;
            }
            if (attempt == 0 && !statementCancelDisabledForTests) {
                boundedCall(() -> {
                    try {
                        st.cancel();
                    } catch (SQLException e) {
                        log.debug("event=admin_ddl_cancel_failed run={} cause=\"{}\"", settings.runLabel(), e.toString());
                    }
                }, remaining);
            } else {
                boundedCall(() -> cancelBackend(remaining), remaining);
            }
            try {
                Thread.sleep(Math.min(100L, Math.max(1L, (deadline - System.nanoTime()) / 1_000_000L)));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            if (running != st) return;   // it ended (cancelled, or finished first)
        }
    }

    /** Runs {@code call} on a daemon thread and waits at most {@code remainingNanos} for it; a call that hangs is abandoned (it ends on its own timeouts). */
    private static void boundedCall(Runnable call, long remainingNanos) {
        Thread t = new Thread(call, "admin-ddl-cancel");
        t.setDaemon(true);
        t.start();
        try {
            t.join(Math.max(1L, remainingNanos / 1_000_000L));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /**
     * {@code pg_cancel_backend} on this session's backend, from a fresh connection that is closed again, only for the backend
     * with the recorded pid AND start time; failures are logged only. The connection's timeouts fit in {@code remainingNanos}.
     */
    private void cancelBackend(long remainingNanos) {
        AdminConnection target = resendAdminForTests != null ? resendAdminForTests : admin;
        if (target == null || backendPid <= 0 || backendStart == null) return;
        String seconds = Long.toString(Math.max(1L, (remainingNanos + 999_999_999L) / 1_000_000_000L));
        Properties props = new Properties();
        props.setProperty("user", target.user());
        props.setProperty("password", target.password());
        props.setProperty("ApplicationName", settings.applicationName() + "-cancel");
        props.setProperty("connectTimeout", seconds);
        props.setProperty("loginTimeout", seconds);
        props.setProperty("socketTimeout", seconds);
        try (Connection c = DriverManager.getConnection(target.url(), props)) {
            c.setAutoCommit(true);
            var pid = DSL.field(DSL.name("pid"), Integer.class);
            var start = DSL.field(DSL.name("backend_start"), SQLDataType.TIMESTAMPWITHTIMEZONE);
            DSL.using(c, SQLDialect.POSTGRES).select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN, pid))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity"))).where(pid.eq(backendPid)).and(start.eq(backendStart))
                .fetch();
        } catch (SQLException | DataAccessException e) {
            log.debug("event=admin_ddl_backend_cancel_failed run={} cause=\"{}\"", settings.runLabel(), e.toString());
        }
    }

    /** Test seam: replaces the recorded backend start time, to show a pid whose start differs is not signalled. */
    void backendStartForTests(java.time.OffsetDateTime start) {
        this.backendStart = start;
    }

    /** Test seam: the backend start time recorded at open. */
    java.time.OffsetDateTime backendStartForTests() {
        return backendStart;
    }

    /**
     * Records the statement a jOOQ query has in flight on this session, so {@link #cancel} can reach it, and refuses to start
     * one after a cancel or an interrupt (the Dekker partner of {@link #cancel}: it publishes, then reads the flag).
     */
    private final class CancelTracker implements org.jooq.ExecuteListener {
        @Override
        public void executeStart(org.jooq.ExecuteContext c) {
            running = c.statement();
            if (cancelled || Thread.currentThread().isInterrupted()) {
                throw new DataAccessException("the statement was cancelled before it started",
                    new SQLException("the statement was cancelled before it started", QUERY_CANCELED));
            }
        }

        @Override
        public void end(org.jooq.ExecuteContext c) {
            running = null;
        }
    }

    private Refused refusal(SQLException e) {
        String state = e.getSQLState();
        if (LOCK_NOT_AVAILABLE.equals(state)) {
            return new Refused(503, "lock_timeout",
                "the table lock was not available within " + LOCK_TIMEOUT + ". Earlier constraints of this run "
                    + "may already have landed (a run is several statements, none rolled back); a rerun is safe: "
                    + "add skips a constraint that exists and validate skips one that is valid", state);
        }
        if (CHECK_VIOLATION.equals(state)) {
            return new Refused(409, "check_violation",
                "VALIDATE found a row that breaks the constraint; it stays NOT VALID. Count the offenders with "
                    + "the " + settings.offenderCount() + " and fix them first", state);
        }
        if (INSUFFICIENT_PRIVILEGE.equals(state)) {
            return new Refused(503, "insufficient_privilege",
                "the admin role may not alter " + settings.tableLabel() + " (NX_DB_ADMIN_* must name its owner)", state);
        }
        log.warn("event=admin_ddl_failed run={} sqlstate={} cause=\"{}\"", settings.runLabel(), state, e.getMessage());
        return new Refused(500, "ddl_failed", "the constraint statement failed: sqlstate=" + state, state);
    }

    @Override
    public void close() {
        // after a cancel the unlock below is refused like any statement (57014); that is swallowed here, and closing the
        // connection releases the session lock all the same
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
