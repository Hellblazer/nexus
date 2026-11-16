// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A token insert could not finish creating its tenant's partition leaves inside the bound
 * {@link TokenStore} gives it (RDR-225, nexus-3wh8d.13).
 *
 * <p>The first token row of a tenant fires {@code nexus.service_tokens_create_tenant_partitions},
 * which takes ACCESS EXCLUSIVE on each model partition and ShareRowExclusive on the registry and on
 * each table that references {@code chunks}. Behind a conflicting lock it fails at its own
 * {@code lock_timeout} (SQLSTATE 55P03), at the caller-side {@code statement_timeout} (57014) or as a
 * deadlock victim (40P01); {@code TokenStore} retries a bounded number of times and then throws this. The transaction is rolled
 * back, nothing was issued, and the caller may retry, so
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps it to a retryable 503.
 *
 * <p>{@link #restoreHeld()} (RDR-248, nexus-csayy.6) is true when, as the insert gave up, a tenant restore load
 * held the restore-load advisory lock: {@code create_tenant_partitions} refuses its slow path while one does
 * ({@code lock_not_available}, at once). A load lasts minutes, not a lock wait, so the handler asks the caller to
 * wait longer.
 */
public final class TenantCreationBusyException extends RuntimeException {

    private final String tenant;
    private final int attempts;
    private final boolean restoreHeld;

    public TenantCreationBusyException(String tenant, int attempts, Throwable cause) {
        this(tenant, attempts, cause, false);
    }

    public TenantCreationBusyException(String tenant, int attempts, Throwable cause, boolean restoreHeld) {
        super("issuing a token for tenant '" + tenant + "' waited on a lock or ran past its time bound on "
            + attempts + " attempts (a tenant's first token also creates its partitions"
            + (restoreHeld ? "; a tenant restore is loading, which blocks partition creation until it ends" : "")
            + "); nothing was issued, retry", cause);
        this.tenant = tenant;
        this.attempts = attempts;
        this.restoreHeld = restoreHeld;
    }

    public String tenant() { return tenant; }
    public int attempts() { return attempts; }
    /** True when a restore load held the restore key as the insert gave up. */
    public boolean restoreHeld() { return restoreHeld; }
}
