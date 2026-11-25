// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), confirmation review N2 and linda's ruling: the counts the doctor needs, for ONE tenant.
 *
 * <p>{@code GET /v1/status} is unauthenticated, so its {@code rdr229_completion} field carries no number that describes tenants
 * ({@link CompletionStatusJson}). The numbers live here, behind {@code GET /v1/catalog/rdr229_completion_counts}, which is
 * authenticated and answers for the CALLER'S tenant only: that tenant's registry rows and live collections, its dangling
 * tombstones by content type and its self-id rows (R7's pre-flight), the registry rows that hold the superseded_by CHECK back,
 * and, for each table whose listed CHECK is NOT VALID, how many of that tenant's rows are orphans (a name no registry row of the
 * tenant carries) and how many are unstamped. No caller sees another tenant's count or a total.
 *
 * <p>READ ONLY, in a {@code tenantScope.withTenant} transaction ({@code CompletionTenantCountReads}: FORCE row-level security scopes every table; each count also
 * carries an explicit tenant equality) under the transaction-local statement bound of the other bounded reads
 * ({@code CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS}); a timeout is SQLSTATE 57014 and none of
 * the answer leaves, which a client reads as "cannot say". The scans are the completion check's own predicates
 * ({@link CompletionCheck#count}), restricted to the tenant: a partitioned table (chunks, taxonomy_centroids) is pruned to the
 * tenant's leaves, and only a table with a NOT VALID CHECK is scanned at all.
 */
public final class CompletionTenantCounts {

    /** A table whose CHECK is NOT VALID and that holds rows of this tenant it refuses. Tables with none are not listed. */
    public record TableCount(String table, long orphans, long unstamped) { }

    /** One tenant's counts. {@code registryRows} is 0 when the registry table is absent. */
    public record Counts(long registryRows, long liveCollections, long supersededByWaiting, CompletionCheck.Dangling dangling,
                         List<TableCount> tables) {
        public Counts {
            tables = List.copyOf(tables);
        }
    }

    /**
     * The counts of {@code tenant} on {@code ctx}, which the caller has scoped to that tenant and bounded ({@code dev.nexus.service.db.CompletionTenantCountReads}).
     */
    public static Counts readIn(DSLContext ctx, String tenant) {
        Set<String> tables = CompletionCheck.tablesPresent(ctx);
        Map<String, List<P31Checks.Row>> byTable = P31Checks.checksByTable();
        Map<String, Boolean> validated = CompletionCheck.readChecks(ctx, byTable, tables);

        List<TableCount> counted = new ArrayList<>();
        for (var e : byTable.entrySet()) {
            if (!tables.contains(e.getKey())) {
                continue;
            }
            // the NOT VALID checks of this table; the NOT NULL guard counts the same rows the name check does, so it is read only when it is alone
            List<P31Checks.Row> notValid = e.getValue().stream()
                .filter(r -> validated.containsKey(r.constraint()) && !validated.get(r.constraint())).toList();
            if (notValid.isEmpty()) {
                continue;
            }
            List<P31Checks.Row> byName = notValid.stream().filter(r -> !"NOT_NULL".equals(r.kind())).toList();
            long orphans = 0;
            long unstamped = 0;
            for (P31Checks.Row row : byName.isEmpty() ? notValid : byName) {
                CompletionCheck.Violators v = CompletionCheck.count(ctx, row, tenant);
                orphans += v.orphans();
                unstamped += v.unstamped();
            }
            if (orphans + unstamped > 0) {
                counted.add(new TableCount(e.getKey(), orphans, unstamped));
            }
        }
        CompletionCheck.Dangling dangling = CompletionCheck.danglingTombstones(ctx, tables, tenant);
        return new Counts(dangling.registryRows(), CompletionCheck.liveCollections(ctx, tables, tenant),
            CompletionCheck.supersededByWaiting(ctx, tables, tenant), dangling, counted);
    }
}
