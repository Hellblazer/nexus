package dev.nexus.service.db;

/**
 * Shared constants for the nexus multi-tenant Postgres storage service.
 *
 * <p>Locking these values here — rather than relying on independent string
 * literals in the Python client (.7), the ETL (.8), and the service — ensures
 * all layers agree on the GUC name and the default-tenant sentinel.  A drift
 * between layers produces silent RLS misses (query returns zero rows even for
 * data that exists), which is the hardest failure mode to diagnose.
 */
public final class TenantConstants {

    private TenantConstants() {}

    /**
     * PostgreSQL GUC (session-local variable) that carries the active tenant
     * principal.  Set via {@code SELECT set_config('nexus.tenant', tenant, true)}
     * inside every transaction boundary.  Consumed by the RLS policy:
     * {@code USING (tenant_id = current_setting('nexus.tenant', true))}.
     *
     * <p>The {@code true} (missing_ok) argument means an unset GUC returns NULL
     * rather than raising an error, so an unstamped connection sees zero rows
     * (fail-closed).
     */
    public static final String GUC_NAME = "nexus.tenant";

    /**
     * Tenant principal stamped on pre-multi-tenant (single-tenant SQLite) data
     * during the SQLite-to-Postgres ETL in bead nexus-gmiaf.8.
     *
     * <p><strong>Coordination contract:</strong> the thin Python HTTP client
     * (bead nexus-gmiaf.7) MUST supply this same value as the
     * {@code X-Nexus-Tenant} header when querying migrated single-tenant data,
     * or RLS will return zero rows.  The ETL (bead nexus-gmiaf.8) MUST write
     * this value into the {@code tenant_id} column for every migrated row.
     *
     * <p>This sentinel will be revisited in Phase 5 (bead nexus-gmiaf.32) when
     * real per-session or per-user principals replace the single-tenant default.
     *
     * <p>T2 decision record: {@code nexus_rdr/152-default-tenant}.
     */
    public static final String DEFAULT_TENANT = "default";
}
