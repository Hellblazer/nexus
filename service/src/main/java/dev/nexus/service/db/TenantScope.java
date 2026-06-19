package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.function.Function;

/**
 * RDR-152 GUC-acquire wrapper: the ONLY factory for a DSLContext that touches tenant data.
 *
 * <p>INVARIANT: there is NO public path to a DSLContext that hasn't been stamped with
 * {@code nexus.tenant}. This class is the sole entry point; it has no
 * {@code getDSLContext()} or equivalent method.
 *
 * <p>Protocol (per S0.1 proof + design doc):
 * <ol>
 *   <li>Borrow a pooled {@link Connection} from the {@link DataSource}.</li>
 *   <li>Set {@code autoCommit=false} — mandatory because {@code SET LOCAL} (GUC
 *       {@code is_local=true}) is a no-op outside a transaction.</li>
 *   <li>Execute {@code SELECT set_config('nexus.tenant', ?, true)} with the tenant
 *       bound as a parameter (injection-safe).</li>
 *   <li>Pass the stamped connection to the caller as a {@link DSLContext}.</li>
 *   <li>Commit on success, rollback on exception.</li>
 *   <li>Restore {@code autoCommit=true} and return the connection to the pool.</li>
 * </ol>
 *
 * <p>POOLER CONSTRAINT: {@code set_config(..., true)} is txn-local (SET LOCAL semantics).
 * This is safe under a transaction-mode pooler. A session-mode pooler (e.g. PgBouncer
 * in default mode) would leak the GUC to the next borrower. v1 connects directly to
 * local PostgreSQL with no pooler interposition; if PgBouncer is ever added it MUST
 * be configured in transaction mode.
 */
public final class TenantScope {

    private static final Logger log = LoggerFactory.getLogger(TenantScope.class);

    private final DataSource dataSource;

    public TenantScope(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    /**
     * Execute {@code work} within a transaction stamped with {@code tenant}.
     *
     * <p>EAGER COMPLETION: the transaction is committed and the connection returned to the
     * pool before this method returns. Callers that need to stream results across the txn
     * boundary (e.g. a jOOQ {@code Cursor} held open while writing an HTTP response body)
     * must do so entirely inside the {@code work} lambda — the connection is NOT available
     * after {@code work.apply()} returns. A streaming-cursor variant (taking a
     * {@code Consumer<DSLContext>}) will be added if needed in beads .7/.9; it does NOT
     * reopen the unstamped-context hole because the GUC stamp happens before the context
     * is handed to the caller.
     *
     * @param tenant the tenant principal to stamp (must not be null or blank)
     * @param work   function receiving a stamped {@link DSLContext}; its return value
     *               is returned from this method
     * @param <T>    return type
     * @return whatever {@code work} returns
     * @throws IllegalArgumentException if {@code tenant} is null or blank
     * @throws RuntimeException         if a {@link SQLException} occurs (wraps it) or
     *                                  if {@code work} throws (propagated after rollback)
     */
    /**
     * Execute {@code work} within a transaction stamped with {@code tenant} using the
     * default {@code nexus.tenant} GUC.
     *
     * @see #withTenant(String, String, Function) for an overload with a custom GUC name
     */
    public <T> T withTenant(String tenant, Function<DSLContext, T> work) {
        return withTenant(tenant, "nexus.tenant", work);
    }

    /**
     * Execute {@code work} within a transaction stamped with {@code tenant} using a
     * custom GUC name.
     *
     * <p>This overload is used by {@link ScratchRepository} which stamps
     * {@code nexus.t1_tenant} to avoid conflating T1 and T2 RLS contexts when
     * connections to the same PG server are used for both stores.
     *
     * @param tenant  the tenant principal to stamp (must not be null or blank)
     * @param gucName the GUC parameter name to set (e.g. {@code "nexus.tenant"} or
     *                {@code "nexus.t1_tenant"})
     * @param work    function receiving a stamped {@link DSLContext}
     */
    public <T> T withTenant(String tenant, String gucName, Function<DSLContext, T> work) {
        if (tenant == null || tenant.isBlank()) {
            throw new IllegalArgumentException("tenant must not be null or blank");
        }
        if (gucName == null || gucName.isBlank()) {
            throw new IllegalArgumentException("gucName must not be null or blank");
        }

        Connection conn = null;
        try {
            conn = dataSource.getConnection();
            // Mandatory: SET LOCAL is a no-op outside a transaction.
            // Pool default is autoCommit=true; we toggle to false for the txn.
            conn.setAutoCommit(false);

            // Stamp the GUC — bind-safe parameterized call (S0.1 pattern verbatim).
            // Use string concatenation only for the GUC name (a validated internal
            // constant from code, not user input); value is always parameterized.
            try (var ps = conn.prepareStatement("SELECT set_config('" + gucName + "', ?, true)")) {
                ps.setString(1, tenant);
                ps.execute();
            }

            // Hand stamped context to caller — the ONLY DSLContext path
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            T result = work.apply(ctx);

            conn.commit();
            return result;

        } catch (SQLException e) {
            log.error("event=tenant_scope_sql_error tenant={} guc={}", tenant, gucName, e);
            rollback(conn);
            throw new RuntimeException("SQL error in tenant scope for tenant: " + tenant, e);
        } catch (RuntimeException e) {
            log.debug("event=tenant_scope_rollback tenant={} guc={} reason={}", tenant, gucName, e.getMessage());
            rollback(conn);
            throw e;  // propagate caller exception unchanged
        } finally {
            if (conn != null) {
                // Two independent try-catch blocks so conn.close() is ALWAYS attempted
                // even if setAutoCommit throws (e.g. dead PG connection).
                try {
                    conn.setAutoCommit(true);  // restore pool default before return
                } catch (SQLException e) {
                    log.warn("event=restore_autocommit_failed tenant={}", tenant);
                }
                try {
                    conn.close();  // returns connection to HikariCP pool
                } catch (SQLException e) {
                    log.warn("event=connection_close_failed tenant={}", tenant);
                }
            }
        }
    }

    private void rollback(Connection conn) {
        if (conn != null) {
            try {
                conn.rollback();
            } catch (SQLException e) {
                log.error("event=rollback_failed", e);
            }
        }
    }
}
