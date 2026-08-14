package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.sql.SQLWarning;
import java.sql.Statement;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
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
 *   <li>Execute {@code SELECT set_config(?, ?, true)} with both the GUC name and the
 *       tenant bound as parameters (injection-safe; nexus-4okz4 increment 4 — the name
 *       was concatenated pre-increment-4 on a since-corrected belief that set_config
 *       could not bind it).</li>
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

    /**
     * Default tenant GUC (T2 / catalog RLS context). Derived from
     * {@link TenantConstants#GUC_NAME} so the allowlist and the RLS policies can
     * never drift to different literals (a drift is a silent RLS miss — see the
     * class comment on {@link TenantConstants}).
     */
    public static final String DEFAULT_TENANT_GUC = TenantConstants.GUC_NAME;

    /** T1-scratch tenant GUC (kept distinct so T1 and T2 RLS contexts never conflate). */
    public static final String T1_TENANT_GUC = "nexus.t1_tenant";

    /**
     * Allowlist of GUC names {@link #withTenant(String, String, Function)} may stamp
     * (nexus-utnjt). The GUC name is now BOUND, not interpolated (nexus-4okz4 increment
     * 4 — {@code set_config(...)} is a regular function; every argument, including the
     * setting name, accepts a bind parameter, correcting the pre-increment-4 belief
     * that it could not). The allowlist stays: today every caller passes a
     * {@code static final} literal, and this is the only durable guard against a
     * future caller passing a request-derived name straight through as a live GUC —
     * a programming-error guard now, not an injection necessity. This set is the
     * single source of truth — the two literals above are members of it.
     */
    private static final Set<String> PERMITTED_GUCS = Set.of(DEFAULT_TENANT_GUC, T1_TENANT_GUC);

    /**
     * Per-DataSource admission semaphores (wave review, Java-tree audit High-1).
     *
     * <p>The HTTP layer is virtual-thread-per-request with NO inherent bound on how
     * many requests concurrently attempt DB work against the fixed HikariCP pool.
     * Every convoy fix so far (batched INSERTs, embed-before-borrow,
     * registration-in-own-txn) is per-call-site; this is the SYSTEMIC backpressure:
     * at most {@code 2 * maximumPoolSize} callers may be inside {@link #withTenant}
     * at once (poolSize active + poolSize queued), everyone else gets the same
     * typed retryable pool-exhaustion signal HikariCP's connectionTimeout produces
     * ({@link SQLTransientConnectionException} in the cause chain →
     * {@code HttpUtil.isPoolExhausted} → 503) — without first burning a
     * connectionTimeout wait per surplus caller.
     *
     * <p>Identity-keyed and static because production constructs MULTIPLE
     * TenantScope instances over the SAME DataSource (NexusService + Main's
     * PgVectorRepository) — a per-instance semaphore would multiply the bound.
     * Fair, so a burst cannot starve early arrivals.
     */
    private static final Map<DataSource, Semaphore> ADMISSION =
        Collections.synchronizedMap(new IdentityHashMap<>());

    private static final int DEFAULT_POOL_SIZE = 10;
    private static final long DEFAULT_TIMEOUT_MS = 30_000L;

    private final DataSource dataSource;
    private final Semaphore admission;
    private final long admissionTimeoutMs;

    public TenantScope(DataSource dataSource) {
        this.dataSource = dataSource;
        int poolSize = DEFAULT_POOL_SIZE;
        long timeoutMs = DEFAULT_TIMEOUT_MS;
        if (dataSource instanceof com.zaxxer.hikari.HikariDataSource hikari) {
            int configured = hikari.getMaximumPoolSize();
            if (configured > 0) {
                poolSize = configured;
            }
            long ct = hikari.getConnectionTimeout();
            if (ct > 0) {
                timeoutMs = ct;
            }
        }
        this.admissionTimeoutMs = timeoutMs;
        final int permits = poolSize * 2;
        this.admission = ADMISSION.computeIfAbsent(
            dataSource, ds -> new Semaphore(permits, true));
    }

    /** Test-only: admission bound + timeout injected directly (stub DataSource path). */
    TenantScope(DataSource dataSource, int admissionPermits, long admissionTimeoutMs) {
        this.dataSource = dataSource;
        this.admissionTimeoutMs = admissionTimeoutMs;
        this.admission = ADMISSION.computeIfAbsent(
            dataSource, ds -> new Semaphore(admissionPermits, true));
    }

    /**
     * Execute {@code work} within a transaction stamped with {@code tenant} using the
     * default {@code nexus.tenant} GUC.
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
     * @see #withTenant(String, String, Function) for an overload with a custom GUC name
     */
    public <T> T withTenant(String tenant, Function<DSLContext, T> work) {
        return withTenant(tenant, DEFAULT_TENANT_GUC, work);
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
        // nexus-utnjt: the GUC name is interpolated into the set_config SQL (PG takes no
        // bind parameter for the setting name), so reject anything not on the allowlist
        // BEFORE borrowing a connection. Blocks SQL injection into the session-GUC namespace.
        if (!PERMITTED_GUCS.contains(gucName)) {
            throw new IllegalArgumentException(
                    "gucName not permitted: " + gucName + " (allowed: " + PERMITTED_GUCS + ")");
        }

        // Admission control (see ADMISSION doc): bound concurrent DB work BEFORE
        // borrowing a connection. Rejection surfaces as the SAME typed retryable
        // signal as HikariCP connectionTimeout (SQLTransientConnectionException in
        // the cause chain), so every handler's sendTypedDbError ladder maps it to
        // a 503 with zero new error-mapping surface.
        boolean admitted;
        try {
            admitted = admission.tryAcquire(admissionTimeoutMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new RuntimeException("interrupted awaiting DB admission for tenant: " + tenant, ie);
        }
        if (!admitted) {
            log.warn("event=tenant_scope_admission_timeout tenant={} guc={} timeout_ms={}",
                tenant, gucName, admissionTimeoutMs);
            throw new RuntimeException(
                "DB admission queue full for tenant: " + tenant,
                new SQLTransientConnectionException(
                    "admission queue full after " + admissionTimeoutMs + "ms (retryable)"));
        }
        try {
            return stampAndRun(tenant, gucName, work);
        } finally {
            admission.release();
        }
    }

    private <T> T stampAndRun(String tenant, String gucName, Function<DSLContext, T> work) {
        Connection conn = null;
        try {
            conn = dataSource.getConnection();
            // Mandatory: SET LOCAL is a no-op outside a transaction.
            // Pool default is autoCommit=true; we toggle to false for the txn.
            conn.setAutoCommit(false);

            // Hand stamped context to caller — the ONLY DSLContext path
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);

            // Stamp the GUC via the set_config(...) FUNCTION (nexus-4okz4 increment 4):
            // unlike the `SET LOCAL x = y` STATEMENT syntax (which cannot bind the
            // setting name — no parameter placeholder is legal there), set_config is a
            // regular SQL function and ALL THREE arguments bind, including the name —
            // PgSession.setLocal already established this exact idiom for the same
            // function. The PERMITTED_GUCS allowlist check above stays as defense in
            // depth (a programming-error guard, not an injection necessity now that the
            // name is bound rather than concatenated).
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                    DSL.val(gucName), DSL.val(tenant), DSL.inline(true)))
               .fetch();

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

    /**
     * Result of attempting {@code VACUUM (ANALYZE)} on one table (nexus-0ys55).
     *
     * @param vacuumed   true iff PostgreSQL actually performed the vacuum — i.e. the
     *                   statement raised no warning. False means PostgreSQL warned-and-
     *                   skipped the table (see permission caveat on {@link
     *                   #vacuumAnalyze(List)}).
     * @param durationMs wall-clock time of the statement, whether it vacuumed or was
     *                   skipped (a skip is still a round trip worth reporting).
     * @param detail     null on a clean vacuum; the PostgreSQL warning text otherwise.
     */
    public record TableVacuumResult(boolean vacuumed, long durationMs, String detail) {}

    /**
     * Allowlist of schema-qualified tables {@link #vacuumAnalyze(List)} may target
     * (nexus-0ys55), mirroring the {@link #PERMITTED_GUCS} discipline above. VACUUM
     * cannot bind-parameterize a table identifier (PostgreSQL takes no bind parameter
     * for a relation name in DDL/maintenance statements), so callers pass literal
     * strings; every caller today passes a {@code static final} constant, but this set
     * is the only durable guard against a future caller passing a request-derived name.
     *
     * <p>LOCKSTEP (nexus-0ys55): must name the SAME three tables as {@code
     * CatalogRepository.PURGE_VACUUM_TABLES} (the only caller today) and the
     * {@code grants-005-chunks-unify-maintain} changeset in {@code
     * grants-nexus-svc.xml} (the MAINTAIN grant that makes a real vacuum possible
     * for the {@code nexus_svc} role this allowlist is otherwise silently pointless
     * for). {@code TenantScopeVacuumMaintainGrantParityTest} pins the Java-side half
     * of that agreement AND cross-checks the changelog text directly. Package-private
     * (not private) so that test can read it directly.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.48 part 2):
     * {@code nexus.chunks_384/768/1024} collapsed into ONE unified {@code
     * nexus.chunks} table — the five-table allowlist ({@code grants-003}'s
     * frozen list, superseded by {@code grants-005}) is now three tables. See
     * {@code vectors-004-unify-chunks.xml}'s header for the grants-boot-brick
     * analysis that motivated {@code grants-005}.
     */
    static final Set<String> VACUUM_ALLOWED_TABLES = Set.of(
        "nexus.chunks", "nexus.catalog_document_chunks", "nexus.catalog_documents");

    /**
     * Runs {@code VACUUM (ANALYZE)} on each of {@code qualifiedTableNames}, one
     * statement per table, on a SINGLE dedicated connection borrowed directly from the
     * pool — deliberately NOT via {@link #withTenant}. Two independent reasons this
     * bypasses the normal gateway:
     * <ol>
     *   <li>VACUUM cannot run inside a transaction block. {@link #withTenant} always
     *       stamps the GUC under {@code autoCommit=false} (mandatory for {@code SET
     *       LOCAL} semantics) and commits when the caller's lambda returns — there is
     *       no point inside that lifecycle where a bare connection is both stamped and
     *       out of a transaction. Callers MUST invoke this only after any related
     *       {@code withTenant} call has already returned (i.e. already committed).</li>
     *   <li>VACUUM is not tenant-scoped work in the RLS sense — it operates on the
     *       whole physical table across every tenant's rows. There is no GUC to stamp
     *       and nothing to scope.</li>
     * </ol>
     *
     * <p>PERMISSION CAVEAT (mirrors the ANALYZE note in {@code grants-nexus-svc.xml}
     * lines 124-162): PostgreSQL does NOT throw when a non-owner without the {@code
     * MAINTAIN} privilege (PG17+) vacuums a table it does not own — {@code vacuum_rel}
     * emits a WARNING (observed on PG17: {@code permission denied to vacuum "<table>",
     * skipping it}) and silently no-ops that table. A bare {@code execute()} returning
     * without exception
     * is therefore NOT proof of a real vacuum. This method inspects {@link
     * Statement#getWarnings()} after each statement and reports {@code vacuumed=false}
     * with the warning text for any table PostgreSQL actually skipped, so a caller
     * never reports {@code vacuumed=true} on a silent no-op.
     *
     * <p>Does NOT go through the {@link #ADMISSION} semaphore that bounds concurrent
     * {@link #withTenant} callers: this is a rare, post-commit administrative step
     * (today: only purge-trash's execute path, itself already rate-limited by being an
     * explicit operator-triggered call), and {@code dataSource.getConnection()} already
     * provides its own backpressure via HikariCP's pool + connectionTimeout.
     *
     * @param qualifiedTableNames schema-qualified table names, validated against
     *                            {@link #VACUUM_ALLOWED_TABLES}
     * @return per-table result, in call order
     * @throws IllegalArgumentException if any name is not in the allowlist
     * @throws RuntimeException wrapping a {@link SQLException} that aborts the whole
     *                          batch (a connection-level failure — distinct from a
     *                          per-table permission skip, which is reported, not thrown)
     */
    // SANCTIONED RAW (nexus-0ys55): VACUUM is PostgreSQL maintenance syntax, not DML —
    // jOOQ has no typed DSL form for it at all (same category as ChashSqlIdioms'
    // refreshAliasStats ANALYZE call, RawSqlGateTest.SANCTIONED_METHODS). Table names
    // are validated against VACUUM_ALLOWED_TABLES above BEFORE this string is built, so
    // this is not an injection surface despite the concatenation.
    public Map<String, TableVacuumResult> vacuumAnalyze(List<String> qualifiedTableNames) {
        for (String table : qualifiedTableNames) {
            if (!VACUUM_ALLOWED_TABLES.contains(table)) {
                throw new IllegalArgumentException(
                    "table not permitted for VACUUM: " + table + " (allowed: " + VACUUM_ALLOWED_TABLES + ")");
            }
        }

        Map<String, TableVacuumResult> results = new LinkedHashMap<>();
        try (Connection conn = dataSource.getConnection()) {
            // VACUUM requires autocommit; be explicit rather than trust pool state —
            // stampAndRun always restores autoCommit=true before returning a connection
            // to the pool, but this method does not rely on that being the ONLY writer.
            conn.setAutoCommit(true);
            for (String table : qualifiedTableNames) {
                long startNanos = System.nanoTime();
                String detail = null;
                try (Statement stmt = conn.createStatement()) {
                    stmt.execute("VACUUM (ANALYZE) " + table);
                    SQLWarning warning = stmt.getWarnings();
                    if (warning != null) {
                        detail = warning.getMessage();
                    }
                }
                long durationMs = (System.nanoTime() - startNanos) / 1_000_000;
                boolean vacuumed = detail == null;
                results.put(table, new TableVacuumResult(vacuumed, durationMs, detail));
                log.info("event=vacuum_analyze_table table={} vacuumed={} duration_ms={} detail={}",
                    table, vacuumed, durationMs, detail);
            }
            return results;
        } catch (SQLException e) {
            log.error("event=vacuum_analyze_connection_failed tables={}", qualifiedTableNames, e);
            throw new RuntimeException("VACUUM (ANALYZE) failed", e);
        }
    }
}
