// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Types;
import java.util.Properties;

/**
 * The dedicated connection one run's CALL goes through (RDR-246, The move worker step 2): opened for the worker, borrowed
 * by nothing else, in autocommit. {@code run_migration_chunks_committing} COMMITs between steps, so it must be CALLed at
 * top level, and a pooled or transaction-wrapped connection would fail its first COMMIT with 2D000. The session carries
 * {@code nexus.tenant} for the run (the pg_background workers copy the caller's settings, so the move's UPDATEs see the
 * tenant's rows under FORCE row-level security), and the setting is cleared again when the run ends.
 *
 * <p>There is no socket read timeout: one CALL runs every chunk of a run, which can take hours. TCP keep-alive and the
 * driver's cancel are the only bounds.
 *
 * <p><b>The one SQL text.</b> The CALL goes through JDBC's escape syntax as a {@code CallableStatement}, with
 * {@code escapeSyntaxCallMode=callIfNoReturn} so the driver sends {@code CALL} and not {@code SELECT}; every value is a bind
 * parameter. No jOOQ form exists for a procedure CALL, and a {@code DSL.query} template here would be a new entry in
 * {@code RawSqlGateTest}'s exemption registry, which is at its ceiling.
 */
final class CallConnection implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CallConnection.class);

    private static final int CONNECT_TIMEOUT_SECONDS = 10;
    private static final int LOGIN_TIMEOUT_SECONDS = 30;

    /** The committing entry point, with its first six parameters positional and {@code i_sleep_ms} by name. */
    private static final String CALL = "{call dml_utils.run_migration_chunks_committing("
        + "?::text, ?::text, ?::text, ?::text, ?::integer, ?::integer, i_sleep_ms => ?::integer)}";

    private final Connection conn;
    private final DSLContext ctx;
    private final int backendPid;
    /** The statement in flight, for {@link #cancel} from another thread; null between runs. */
    private volatile CallableStatement running;
    private volatile boolean closed;

    private CallConnection(Connection conn, int backendPid) {
        this.conn = conn;
        this.ctx = DSL.using(conn, SQLDialect.POSTGRES);
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
        props.setProperty("escapeSyntaxCallMode", "callIfNoReturn");
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
     * @throws SQLException what the CALL raised; the caller classifies it
     */
    void call(String sqlText, String drivingSchema, String drivingTable, String runLabel, int chunkSize, int sleepMs)
            throws SQLException {
        try (CallableStatement cs = conn.prepareCall(CALL)) {
            cs.setString(1, sqlText);
            cs.setString(2, drivingSchema);
            cs.setString(3, drivingTable);
            cs.setString(4, runLabel);
            cs.setInt(5, chunkSize);
            cs.setInt(6, 1);
            cs.setObject(7, sleepMs, Types.INTEGER);
            running = cs;
            if (closed) {
                throw new SQLException("the call connection was closed before the CALL started", "57014");
            }
            cs.execute();
        } finally {
            running = null;
        }
    }

    /** True while the connection can still run a statement. */
    boolean usable() {
        try {
            return !closed && !conn.isClosed() && conn.isValid(5);
        } catch (SQLException e) {
            return false;
        }
    }

    /** Asks the server to stop the CALL in flight, if any (the driver's cancel). Safe from any thread. */
    void cancel() {
        CallableStatement cs = running;
        if (cs != null) {
            try {
                cs.cancel();
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
}
