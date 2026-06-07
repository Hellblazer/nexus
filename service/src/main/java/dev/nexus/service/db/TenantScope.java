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
     * @param tenant the tenant principal to stamp (must not be null or blank)
     * @param work   function receiving a stamped {@link DSLContext}; its return value
     *               is returned from this method
     * @param <T>    return type
     * @return whatever {@code work} returns
     * @throws IllegalArgumentException if {@code tenant} is null or blank
     * @throws RuntimeException         if a {@link SQLException} occurs (wraps it) or
     *                                  if {@code work} throws (propagated after rollback)
     */
    public <T> T withTenant(String tenant, Function<DSLContext, T> work) {
        if (tenant == null || tenant.isBlank()) {
            throw new IllegalArgumentException("tenant must not be null or blank");
        }

        Connection conn = null;
        boolean committed = false;
        try {
            conn = dataSource.getConnection();
            // Mandatory: SET LOCAL is a no-op outside a transaction
            conn.setAutoCommit(false);

            // Stamp the GUC — bind-safe parameterized call (S0.1 pattern verbatim)
            try (var ps = conn.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
                ps.setString(1, tenant);
                ps.execute();
            }

            // Hand stamped context to caller — the ONLY DSLContext path
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            T result = work.apply(ctx);

            conn.commit();
            committed = true;
            return result;

        } catch (SQLException e) {
            log.error("event=tenant_scope_sql_error tenant={}", tenant, e);
            rollback(conn);
            throw new RuntimeException("SQL error in tenant scope for tenant: " + tenant, e);
        } catch (RuntimeException e) {
            log.debug("event=tenant_scope_rollback tenant={} reason={}", tenant, e.getMessage());
            rollback(conn);
            throw e;  // propagate caller exception unchanged
        } finally {
            if (conn != null) {
                try {
                    if (!committed) {
                        // rollback() already attempted above; belt-and-suspenders
                    }
                    conn.setAutoCommit(true);
                    conn.close();  // returns to pool
                } catch (SQLException ignored) {
                    log.warn("event=tenant_scope_close_failed tenant={}", tenant);
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
