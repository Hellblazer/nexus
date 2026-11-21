// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.CompletionTenantCounts;

import java.util.Objects;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), review N2: the tenant-scoped, bounded read behind {@code GET /v1/catalog/rdr229_completion_counts}.
 * The counts are {@link CompletionTenantCounts}; this class puts them in the caller's tenant scope and under the statement bound
 * of the other bounded reads ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}). A timeout is SQLSTATE
 * {@link CatalogRepository#QUERY_CANCELED} and none of the answer leaves. READ ONLY.
 */
public final class CompletionTenantCountReads {

    private final CatalogRepository repo;

    public CompletionTenantCountReads(CatalogRepository repo) {
        this.repo = Objects.requireNonNull(repo, "repo");
    }

    public CompletionTenantCounts.Counts read(String tenant) {
        Objects.requireNonNull(tenant, "tenant");
        return repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(Integer.getInteger(
                CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS)));
            return CompletionTenantCounts.readIn(ctx, tenant);
        });
    }
}
