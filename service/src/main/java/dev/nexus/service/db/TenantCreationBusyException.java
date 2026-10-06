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
 * {@code lock_timeout} (SQLSTATE 55P03) or at the caller-side {@code statement_timeout} (57014);
 * {@code TokenStore} retries a bounded number of times and then throws this. The transaction is rolled
 * back, nothing was issued, and the caller may retry, so
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps it to a retryable 503.
 */
public final class TenantCreationBusyException extends RuntimeException {

    private final String tenant;
    private final int attempts;

    public TenantCreationBusyException(String tenant, int attempts, Throwable cause) {
        super("creating the partitions for tenant '" + tenant + "' waited on a lock after " + attempts
            + " attempts; nothing was issued, retry", cause);
        this.tenant = tenant;
        this.attempts = attempts;
    }

    public String tenant() { return tenant; }
    public int attempts() { return attempts; }
}
