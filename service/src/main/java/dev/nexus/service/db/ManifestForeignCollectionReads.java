// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record5;
import org.jooq.Select;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

/**
 * GH #1611: the bounded read behind {@code GET /v1/catalog/manifest/foreign_collection?limit=N}: the tenant's live
 * documents whose manifest names chunks in a collection other than the document's own {@code physical_collection}.
 * Such a document answers a search scoped to its collection with fewer chunks than its manifest lists (a repo index
 * that resolved a file onto another owner's document wrote its chunks into the repo's collection under that document's
 * tumbler, GH #1611). Split out of {@link CatalogRepository} (at its file-size cap) like {@link CatalogSensitiveReads},
 * whose shape and bounds this copies.
 *
 * <p>ONE statement: each row of the page carries {@code count(*) OVER ()}, the number of such documents before the
 * {@code LIMIT}, so the list and its total come from one snapshot (see {@link CatalogSensitiveReads} for why two
 * statements would not). It runs in a {@code tenantScope.withTenant} transaction (FORCE row-level security scopes both
 * tables; the tenant is also an explicit equality on each) under a transaction-local {@code statement_timeout}, the
 * same bound and test hook as the other bounded reads ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}): a timeout
 * is SQLSTATE {@link CatalogRepository#QUERY_CANCELED} and no part of the answer leaves this method.
 *
 * <p>The predicate compares two columns of a join ({@code m.collection <> d.physical_collection}), so no index on
 * either table can answer it: the statement reads the tenant's whole manifest (a join of the live documents against
 * {@code idx_catalog_chunks_doc_id} or a hash join over a manifest scan, the planner's choice) and groups it by
 * document. That is linear in the manifest and is bounded by the statement timeout, not by the page limit: on a
 * catalog too large to finish in time the route answers 503, which a client reads as "cannot say". A partial or
 * expression index cannot help (the inequality spans two tables), and a denormalised "foreign" flag on the manifest
 * would put a write-path obligation on every writer for a diagnostic; so none is added.
 */
public final class ManifestForeignCollectionReads {

    /** The most documents one call lists; the route answers 400 above it. */
    public static final int MAX_LIMIT = 10_000;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /**
     * One listed document: {@code collections} are the other collections its manifest names (sorted, never including
     * {@code physicalCollection}); {@code chunks} is how many manifest rows name them.
     */
    public record Doc(String tumbler, String physicalCollection, List<String> collections, int chunks) {}

    /** The answer: {@code count} is every such document of the tenant; {@code documents} at most {@code limit} of them. */
    public record Page(int count, int limit, boolean capped, List<Doc> documents) {}

    private final CatalogRepository repo;

    public ManifestForeignCollectionReads(CatalogRepository repo) {
        this.repo = repo;
    }

    /**
     * The statement's row filter: a live document with a physical collection, and a manifest row of the tenant that
     * names a different one. {@code <>} is false for a NULL on either side, so a ghost document (no collection) and a
     * row with no collection are never findings.
     */
    private static Condition foreign(String tenant) {
        var d = CATALOG_DOCUMENTS;
        var m = CATALOG_DOCUMENT_CHUNKS;
        return d.TENANT_ID.eq(tenant)
            .and(m.TENANT_ID.eq(tenant))
            .and(d.DELETED_AT.isNull())
            .and(d.PHYSICAL_COLLECTION.ne(""))
            .and(m.COLLECTION.ne(d.PHYSICAL_COLLECTION));
    }

    /**
     * The one statement: {@code fetchLimit} rows of (tumbler, physical collection, other collections, chunk count,
     * total), the caller passing {@code limit + 1}; the total is the same on every row. Public so a plan-shape test can
     * explain the exact statement the route runs.
     */
    public static Select<Record5<String, String, String[], Integer, Integer>> listQuery(
            DSLContext ctx, String tenant, int fetchLimit) {
        var d = CATALOG_DOCUMENTS;
        var m = CATALOG_DOCUMENT_CHUNKS;
        Field<String[]> others = DSL.arrayAggDistinct(m.COLLECTION).orderBy(m.COLLECTION);
        return ctx.select(d.TUMBLER, d.PHYSICAL_COLLECTION, others, DSL.count(), DSL.count().over())
            .from(d.join(m).on(m.TENANT_ID.eq(d.TENANT_ID).and(m.DOC_ID.eq(d.TUMBLER))))
            .where(foreign(tenant))
            .groupBy(d.TUMBLER, d.PHYSICAL_COLLECTION)
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
            // The total is the window count over the grouped rows, before the LIMIT: count and capped share one snapshot.
            int count = rows.isEmpty() ? 0 : rows.get(0).value5();
            boolean capped = count > limit;
            int n = Math.min(rows.size(), limit);
            List<Doc> docs = new ArrayList<>(n);
            for (int i = 0; i < n; i++) {
                var r = rows.get(i);
                docs.add(new Doc(r.value1(), r.value2(), List.copyOf(Arrays.asList(r.value3())), r.value4()));
            }
            return new Page(count, limit, capped, docs);
        });
    }
}
