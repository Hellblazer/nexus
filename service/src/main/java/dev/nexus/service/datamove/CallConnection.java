// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.ExecuteContext;
import org.jooq.SQLDialect;
import org.jooq.conf.RenderQuotedNames;
import org.jooq.conf.Settings;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultConfiguration;
import org.jooq.impl.DefaultExecuteListener;
import org.jooq.impl.DefaultExecuteListenerProvider;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.Properties;

/**
 * The dedicated connection one run's CALL goes through (RDR-246, The move worker step 2): opened for the worker, borrowed
 * by nothing else, in autocommit. {@code run_migration_chunks_committing} COMMITs between steps, so it must be CALLed at
 * top level, and a pooled or transaction-wrapped connection would fail its first COMMIT with 2D000. The session carries
 * {@code nexus.tenant} for the run (the pg_background workers copy the caller's settings, so the move's UPDATEs see the
 * tenant's rows under FORCE row-level security), and the setting is cleared again when the run ends.
 *
 * <p>There is no socket read timeout: one CALL runs every chunk of a run, which can take hours. TCP keep-alive and the
 * cancel are the bounds. {@link #cancel} is what the engine's stop uses: it sets a flag no later statement passes, and it
 * cancels the statement in flight, with {@code cancelSignalTimeout} 2 s (as {@code AdminDdlSession} does) so the driver's
 * cancel connection cannot hold a stop for its default ten seconds.
 *
 * <p>The CALL is the typed routine {@link CommittingProcedure}, run on a jOOQ context over this connection.
 */
final class CallConnection implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CallConnection.class);

    private static final int CONNECT_TIMEOUT_SECONDS = 10;
    private static final int LOGIN_TIMEOUT_SECONDS = 30;
    private static final String QUERY_CANCELED = "57014";

    private final Connection conn;
    private final DSLContext ctx;
    /** The context the CALL runs on: the DEFAULT dialect makes the routine a callable statement, see CommittingProcedure. */
    private final org.jooq.Configuration callConfiguration;
    private final int backendPid;
    /** The statement in flight, for {@link #cancel} from another thread; null between runs. */
    private volatile Statement running;
    /** Set first by {@link #cancel}: a statement about to start sees it and does not start. */
    private volatile boolean cancelled;
    private volatile boolean closed;

    private CallConnection(Connection conn, int backendPid) {
        this.conn = conn;
        this.ctx = DSL.using(conn, SQLDialect.POSTGRES);
        this.callConfiguration = new DefaultConfiguration().set(conn).set(SQLDialect.DEFAULT)
            .set(new Settings().withRenderQuotedNames(RenderQuotedNames.NEVER))
            .set(new DefaultExecuteListenerProvider(new CancelTracker()));
        this.backendPid = backendPid;
    }

    /** Opens the connection on the admin values; {@code applicationName} names it in {@code pg_stat_activity}. */
    static CallConnection open(AdminConnection admin, String applicationName) throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", applicationName);
        props.setProperty("connectTimeout", Integer.toString(CONNECT_TIMEOUT_SECONDS));
        props.setProperty("loginTimeout", Integer.toString(LOGIN_TIMEOUT_SECONDS));
        props.setProperty("tcpKeepAlive", "true");
        props.setProperty("cancelSignalTimeout", "2");
        // the driver sends a JDBC call escape as CALL, not SELECT
        props.setProperty("escapeSyntaxCallMode", "callIfNoReturn");
        // a text bind reaches the procedure's domain and enum parameters untyped, and the server resolves it
        props.setProperty("stringtype", "unspecified");
        Connection c = DriverManager.getConnection(admin.url(), props);
        try {
            c.setAutoCommit(true);
            int pid = DSL.using(c, SQLDialect.POSTGRES)
                .select(DSL.function("pg_backend_pid", SQLDataType.INTEGER)).fetchOne(0, Integer.class);
            return new CallConnection(c, pid);
        } catch (SQLException | RuntimeException e) {
            try {
                c.close();
            } catch (SQLException suppressed) {
                e.addSuppressed(suppressed);
            }
            throw e instanceof SQLException se ? se : new SQLException("the call connection could not be set up", e);
        }
    }

    /** The server backend of this connection: the pid an operator cancels, and the one the engine logs for each CALL. */
    int backendPid() {
        return backendPid;
    }

    /** {@code SET nexus.tenant} for the session (as {@code set_config(..., false)}, so a bind parameter carries the value). */
    void useTenant(String tenant) {
        setTenantSetting(tenant);
    }

    /**
     * {@code RESET nexus.tenant}, then the check that the session carries none. A connection that cannot be shown clean
     * must not run another tenant's CALL, so a failure here is thrown and the caller closes the connection.
     */
    void clearTenant() {
        setTenantSetting(null);
        String left = ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR,
            DSL.val(TenantScope.DEFAULT_TENANT_GUC), DSL.inline(true))).fetchOne(0, String.class);
        if (left != null && !left.isEmpty()) {
            throw new IllegalStateException("the call connection still carries nexus.tenant after RESET");
        }
    }

    private void setTenantSetting(String value) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(TenantScope.DEFAULT_TENANT_GUC),
            DSL.val(value, SQLDataType.VARCHAR), DSL.inline(false))).fetchOne();
    }

    /**
     * CALLs the committing entry point for one run, threads 1, and returns when the run has done every chunk (or
     * was cancelled in dml_utils, which returns without raising).
     *
     * @throws SQLException what the CALL raised; the caller classifies it. SQLSTATE 57014 when the connection was
     *                      cancelled or closed before the CALL started
     */
    void call(String sqlText, String drivingSchema, String drivingTable, String runLabel, int chunkSize, int sleepMs)
            throws SQLException {
        try {
            new CommittingProcedure(sqlText, drivingSchema, drivingTable, runLabel, chunkSize, sleepMs)
                .execute(callConfiguration);
        } catch (DataAccessException e) {
            SQLException cause = e.getCause(SQLException.class);
            throw cause != null ? cause : new SQLException(e.getMessage(), e.sqlState(), e);
        }
    }

    /** True while the connection can still run a statement. */
    boolean usable() {
        try {
            return !closed && !cancelled && !conn.isClosed() && conn.isValid(5);
        } catch (SQLException e) {
            return false;
        }
    }

    /**
     * Stops this connection: no statement starts after it, and the one in flight, if any, is cancelled (the driver's cancel,
     * bounded by {@code cancelSignalTimeout}). Safe from any thread. The connection is not used again.
     */
    void cancel() {
        cancelled = true;
        Statement st = running;
        if (st != null) {
            try {
                st.cancel();
            } catch (SQLException e) {
                log.debug("event=data_move_call_cancel_failed cause=\"{}\"", e.toString());
            }
        }
    }

    @Override
    public void close() {
        closed = true;
        try {
            conn.close();
        } catch (SQLException e) {
            log.debug("event=data_move_call_close_failed cause=\"{}\"", e.toString());
        }
    }

    /**
     * Publishes the statement a CALL has in flight so {@link #cancel} can reach it, and refuses to start one after a cancel
     * or a close (the partner of {@link #cancel}: it publishes, then reads the flag; cancel sets the flag, then reads the
     * statement, so either the cancel sees the statement or this sees the cancel).
     */
    private final class CancelTracker extends DefaultExecuteListener {
        private static final long serialVersionUID = 1L;

        @Override
        public void executeStart(ExecuteContext c) {
            running = c.statement();
            if (cancelled || closed) {
                throw new DataAccessException("the call connection was cancelled before the CALL started",
                    new SQLException("the call connection was cancelled before the CALL started", QUERY_CANCELED));
            }
        }

        @Override
        public void end(ExecuteContext c) {
            running = null;
        }
    }
}
