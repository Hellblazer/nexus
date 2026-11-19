// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record7;
import org.jooq.Select;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;

/**
 * nexus-uwggq.2.5 (RDR-241 Phase 2): the bounded read behind {@code GET /v1/catalog/sensitive/locators?limit=N}: the
 * tenant's live sensitive documents and the live documents that alias a live sensitive one (a merged duplicate holds the
 * same content in its own file), with the fields operator dispatch reads to write a file-tool deny rule without listing
 * the catalog ({@code operators/sensitive_paths.py} {@code _restrict} and {@code _locator}).
 *
 * <p>Same shape as {@link CatalogSensitiveReads}: ONE statement whose every row carries {@code count(*) OVER ()}, so the
 * list and its total are one snapshot; {@code limit + 1} rows are fetched and {@code capped} comes from the window count,
 * never from the row count; it runs in a {@code tenantScope.withTenant} transaction under the section-label count's
 * {@code statement_timeout} ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}, same test hook), so a timeout is a
 * {@code DataAccessException} holding SQLSTATE {@link CatalogRepository#QUERY_CANCELED} and none of the answer leaves.
 *
 * <p>The row set is one scan of the tenant's live rows with {@code sensitive OR alias_of IN (the tenant's live sensitive
 * tumblers)}: a row that is both appears once. No index on {@code alias_of}: the spike (nexus-uwggq.2.1) measured the
 * statement at 17.9 ms at 50,000 live rows, under the 100 ms rule. Empty {@code source_uri}, {@code file_path} and
 * {@code alias_of} come back as {@code ""}, the columns' defaults, never null.
 *
 * <p>Each row carries {@code sensitive}, the row's OWN flag: true for a sensitive row, and for an alias row whatever that
 * row's own column says (false unless it is itself flagged), never the target's. So {@code sensitive_paths._restrict}, which
 * keeps a row that is flagged or whose {@code alias_of} names a flagged row of the same list, consumes the rows unchanged:
 * the alias's target is a live sensitive row and is in the same list, except past the {@code limit} (a capped answer).
 */
public final class CatalogSensitiveLocatorReads {

    /** The most documents one call lists; the route answers 400 above it. */
    public static final int MAX_LIMIT = CatalogSensitiveReads.MAX_LIMIT;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = CatalogSensitiveReads.DEFAULT_LIMIT;

    /** One listed document: the fields the operator reads, and its own {@code sensitive} flag. */
    public record Doc(String tumbler, String sourceUri, String filePath, String aliasOf, String physicalCollection, boolean sensitive) {}

    /** The answer: {@code count} is every row of the set; {@code documents} at most {@code limit} of them. */
    public record Page(int count, int limit, boolean capped, List<Doc> documents) {}

    private final CatalogRepository repo;

    public CatalogSensitiveLocatorReads(CatalogRepository repo) {
        this.repo = repo;
    }

    /**
     * The one statement: {@code fetchLimit} rows of (tumbler, source_uri, file_path, alias_of, physical_collection, sensitive,
     * total), the caller passing {@code limit + 1}. Public so a plan-shape test can explain the exact statement.
     */
    public static Select<Record7<String, String, String, String, String, Boolean, Integer>> listQuery(
            DSLContext ctx, String tenant, int fetchLimit) {
        var d = CATALOG_DOCUMENTS.as("d");
        var s = CATALOG_DOCUMENTS.as("s");
        var sensitiveTumblers = ctx.select(s.TUMBLER).from(s)
            .where(s.TENANT_ID.eq(tenant).and(DSL.condition(s.SENSITIVE)).and(s.DELETED_AT.isNull()));
        return ctx.select(d.TUMBLER, d.SOURCE_URI, d.FILE_PATH, d.ALIAS_OF, d.PHYSICAL_COLLECTION, d.SENSITIVE, DSL.count().over())
            .from(d)
            .where(d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull())
                .and(DSL.condition(d.SENSITIVE).or(d.ALIAS_OF.in(sensitiveTumblers))))
            .orderBy(d.TUMBLER)
            .limit(fetchLimit);
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
            int count = rows.isEmpty() ? 0 : rows.get(0).value7();
            boolean capped = count > limit;
            List<Doc> docs = new ArrayList<>(Math.min(rows.size(), limit));
            for (int i = 0; i < Math.min(rows.size(), limit); i++) {
                var r = rows.get(i);
                docs.add(new Doc(r.value1(), nz(r.value2()), nz(r.value3()), nz(r.value4()), nz(r.value5()), Boolean.TRUE.equals(r.value6())));
            }
            return new Page(count, limit, capped, docs);
        });
    }

    private static String nz(String s) {
        return s == null ? "" : s;
    }
}
