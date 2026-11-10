// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Record1;
import org.jooq.Record2;
import org.jooq.Select;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;

/**
 * nexus-f3yqd: the bounded read behind {@code GET /v1/catalog/sensitive?limit=N}, split out of
 * {@link CatalogRepository} (which is at its file-size cap): the tenant's live sensitive documents, listed up to a
 * limit, and how many there are in all.
 *
 * <p>Two statements, one transaction ({@code tenantScope.withTenant}, so FORCE row-level security scopes every row
 * to the caller's tenant; the tenant is also an explicit equality, the partition and index key). Both run under ONE
 * {@code statement_timeout} set transaction-locally, the same bound and the same test hook as
 * {@link CatalogRepository#manifestSectionLabelFillable} ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}):
 * a timeout on either statement is a {@code DataAccessException} whose cause chain holds SQLSTATE
 * {@link CatalogRepository#QUERY_CANCELED}, the transaction ends, and no part of the answer leaves this method.
 *
 * <p>Both statements are eligible for {@code idx_catalog_documents_sensitive_live} (catalog-047-1, a partial index on
 * {@code (tenant_id, physical_collection) WHERE sensitive AND deleted_at IS NULL}): the predicate below is the index
 * predicate plus the tenant. The list is ordered by tumbler, which the index does not carry, so the list is a scan of
 * the index (small: the sensitive set is a deliberate marking, not the catalog) and a sort of that set.
 */
public final class CatalogSensitiveReads {

    /** The most documents one call lists; the route answers 400 above it. Same bound as the section-label count. */
    public static final int MAX_LIMIT = 10_000;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /** One listed document. */
    public record Doc(String tumbler, String physicalCollection) {}

    /** The answer: {@code count} is every live sensitive document of the tenant; {@code documents} at most {@code limit} of them. */
    public record Page(int count, int limit, boolean capped, List<Doc> documents) {}

    private final CatalogRepository repo;

    public CatalogSensitiveReads(CatalogRepository repo) {
        this.repo = repo;
    }

    /** The predicate of both statements: the partial index's own, plus the tenant. */
    private static Condition liveSensitive(String tenant) {
        // DSL.condition(field) renders the bare column, which is the index predicate's exact form ("= true" would
        // rely on the planner folding it).
        return CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
            .and(DSL.condition(CATALOG_DOCUMENTS.SENSITIVE))
            .and(CATALOG_DOCUMENTS.DELETED_AT.isNull());
    }

    /**
     * The list statement, {@code fetchLimit} rows (the caller passes {@code limit + 1}, the extra row being how it
     * knows more exist). Public so a plan-shape test can explain the exact statement the route runs.
     */
    public static Select<Record2<String, String>> listQuery(DSLContext ctx, String tenant, int fetchLimit) {
        return ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .from(CATALOG_DOCUMENTS)
            .where(liveSensitive(tenant))
            .orderBy(CATALOG_DOCUMENTS.TUMBLER)
            .limit(fetchLimit);
    }

    /** The count statement. Public for the same reason as {@link #listQuery}. */
    public static Select<Record1<Integer>> countQuery(DSLContext ctx, String tenant) {
        return ctx.selectCount().from(CATALOG_DOCUMENTS).where(liveSensitive(tenant));
    }

    /**
     * @param limit 1 to {@link #MAX_LIMIT}
     * @throws IllegalArgumentException when {@code limit} is outside that range
     */
    public Page list(String tenant, int limit) {
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        }
        return repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(Integer.getInteger(
                CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS)));
            var rows = listQuery(ctx, tenant, limit + 1).fetch();
            int count = countQuery(ctx, tenant).fetchOne(0, Integer.class);
            boolean capped = rows.size() > limit;
            List<Doc> docs = new ArrayList<>(Math.min(rows.size(), limit));
            for (int i = 0; i < Math.min(rows.size(), limit); i++) {
                docs.add(new Doc(rows.get(i).value1(), rows.get(i).value2()));
            }
            return new Page(count, limit, capped, docs);
        });
    }
}
