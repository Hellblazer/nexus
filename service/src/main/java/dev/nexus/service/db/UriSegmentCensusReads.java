// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record6;
import org.jooq.Select;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;

/**
 * RDR-229 P1.2 (nexus-p2m1x.2): the bounded read behind {@code GET /v1/catalog/uri_segment_census?limit=N}. It groups
 * the tenant's {@code chroma://} and {@code nx-note://} source-URI rows by {@code (table, scheme, segment, collection)}
 * across {@code catalog_documents} ({@code physical_collection}), {@code document_aspects} and
 * {@code document_highlights} ({@code collection}), and returns each group's row count with the content type of its
 * collection's registry row ({@code ''} when the collection has no registry row). The segment is the text between the
 * scheme and the first {@code /}.
 *
 * <p>The engine decides nothing: which segments are file-routed, and which content types are file-typed, are the
 * client's rules ({@code aspect_readers.FILE_ROUTED_PREFIXES}, the residue doctor row), so the two lists cannot drift
 * apart across the wire. The route exists so the client reads a count of residue and never walks the catalog (RDR-241,
 * the walk-site ratchet).
 *
 * <p>The cardinality is the number of distinct {@code (segment, collection)} pairs, a few per collection, not the row
 * count; the page limit bounds the answer, and ONE statement carries {@code count(*) OVER ()}, the group count before
 * the {@code LIMIT}, so the list and its total come from one snapshot (see {@link CatalogSensitiveReads}). It runs in
 * a {@code tenantScope.withTenant} transaction (FORCE row-level security scopes every table; the tenant is also an
 * explicit equality on each) under the transaction-local {@code statement_timeout} of the other bounded reads
 * ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}); a timeout is SQLSTATE {@link CatalogRepository#QUERY_CANCELED}
 * and none of the answer leaves this method. The statement reads every row of the three tables whose URI starts with
 * one of the two schemes (a heap scan or an index range per table, the planner's choice), so its cost is linear in
 * those rows and is bounded by the timeout, which a client reads as "cannot say" (a 503).
 *
 * <p><b>RDR-229 P3.5c.</b> From gate 3 the registry join is on the id each table carries ({@code physical_collection_id},
 * {@code collection_id}) and the group's collection is the registry's current label of that id ({@code ''} for a row with no
 * id or no registry row); before it, the join and the group are on the name column as they were.
 *
 * <p>READ ONLY. No write, no lock beyond the snapshot's.
 */
public final class UriSegmentCensusReads {

    /** The most groups one call lists; the route answers 400 above it. */
    public static final int MAX_LIMIT = 10_000;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 10_000;

    public static final String CHROMA = "chroma://";
    public static final String NOTE = "nx-note://";

    /** One group; {@code table} is the table's name, {@code scheme} is {@code chroma} or {@code nx-note}. */
    public record Group(String table, String scheme, String segment, String collection, String contentType, int rows) {}

    /** The answer: {@code count} is every group of the tenant; {@code groups} at most {@code limit} of them. */
    public record Page(int count, int limit, boolean capped, List<Group> groups) {}

    private final CatalogRepository repo;
    private final Gate3 gate3;

    public UriSegmentCensusReads(CatalogRepository repo) {
        this(repo, Gate3.never());
    }

    public UriSegmentCensusReads(CatalogRepository repo, Gate3 gate3) {
        this.repo = repo;
        this.gate3 = java.util.Objects.requireNonNull(gate3, "gate3");
    }

    private static Select<Record6<String, String, String, String, String, Integer>> perTable(
            CollectionKey k, String tenant, Field<String> tableLabel, Field<String> schemeLabel, String prefix, Table<?> table,
            Field<String> tenantId, Field<String> uri, Field<String> collection, Field<Long> collectionId) {
        var c = CATALOG_COLLECTIONS;
        // The text after the first "://" up to the next "/": for "chroma://x/y" and "nx-note://x/y" alike, "x". Typed
        // jOOQ with INLINE literal arguments, not binds: the select list and the GROUP BY each render this expression,
        // and two bind placeholders would be two different expressions to Postgres ("must appear in the GROUP BY
        // clause"; the INLINE-VS-BIND RULE, RawSqlGateTest).
        Field<String> segment = DSL.function("split_part", String.class,
            DSL.function("split_part", String.class, uri, DSL.inline("://"), DSL.inline(2)),
            DSL.inline("/"), DSL.inline(1));
        // the group's collection: the row's name before gate 3; the registry's current label of its id after (the join is on the id)
        Field<String> coll = DSL.coalesce(k.byId() ? c.NAME : collection, DSL.inline(""));
        Field<String> ctype = DSL.coalesce(c.CONTENT_TYPE, DSL.inline(""));
        return DSL.select(tableLabel.as("tbl"), schemeLabel.as("scheme"), segment.as("segment"),
                          coll.as("coll"), ctype.as("ctype"), DSL.count().as("n"))
            .from(table.leftJoin(c).on(c.TENANT_ID.eq(tenantId).and(k.same(c.NAME, c.COLLECTION_ID, collection, collectionId))))
            .where(tenantId.eq(tenant).and(uri.startsWith(prefix)))
            .groupBy(segment, coll, ctype);
    }

    /**
     * The one statement: {@code fetchLimit} groups ordered by table, scheme, collection and segment, plus the total
     * group count on every row. One grouped select per table and scheme (the scheme is a literal of its select), under
     * a UNION ALL. Public so a plan-shape test can explain the exact statement the route runs.
     */
    public static Select<?> listQuery(DSLContext ctx, String tenant, int fetchLimit) {
        return listQuery(ctx, tenant, fetchLimit, Gate3.never());
    }

    /** As {@link #listQuery(DSLContext, String, int)}, reading the collection by its id when {@code gate3} holds (read once here). */
    public static Select<?> listQuery(DSLContext ctx, String tenant, int fetchLimit, Gate3 gate3) {
        CollectionKey k = CollectionKey.of(gate3);
        var docs = CATALOG_DOCUMENTS;
        var asp = DOCUMENT_ASPECTS;
        var hl = DOCUMENT_HIGHLIGHTS;
        List<Select<Record6<String, String, String, String, String, Integer>>> parts = List.of(
            perTable(k, tenant, DSL.inline("catalog_documents"), DSL.inline("chroma"), CHROMA, docs, docs.TENANT_ID,
                     docs.SOURCE_URI, docs.PHYSICAL_COLLECTION, docs.PHYSICAL_COLLECTION_ID),
            perTable(k, tenant, DSL.inline("catalog_documents"), DSL.inline("nx-note"), NOTE, docs, docs.TENANT_ID,
                     docs.SOURCE_URI, docs.PHYSICAL_COLLECTION, docs.PHYSICAL_COLLECTION_ID),
            perTable(k, tenant, DSL.inline("document_aspects"), DSL.inline("chroma"), CHROMA, asp, asp.TENANT_ID,
                     asp.SOURCE_URI, asp.COLLECTION, asp.COLLECTION_ID),
            perTable(k, tenant, DSL.inline("document_aspects"), DSL.inline("nx-note"), NOTE, asp, asp.TENANT_ID,
                     asp.SOURCE_URI, asp.COLLECTION, asp.COLLECTION_ID),
            perTable(k, tenant, DSL.inline("document_highlights"), DSL.inline("chroma"), CHROMA, hl, hl.TENANT_ID,
                     hl.SOURCE_URI, hl.COLLECTION, hl.COLLECTION_ID),
            perTable(k, tenant, DSL.inline("document_highlights"), DSL.inline("nx-note"), NOTE, hl, hl.TENANT_ID,
                     hl.SOURCE_URI, hl.COLLECTION, hl.COLLECTION_ID));
        Select<Record6<String, String, String, String, String, Integer>> union = parts.get(0);
        for (int i = 1; i < parts.size(); i++) union = union.unionAll(parts.get(i));
        var u = union.asTable("u");
        Field<String> tbl = u.field("tbl", String.class);
        Field<String> scheme = u.field("scheme", String.class);
        Field<String> segment = u.field("segment", String.class);
        Field<String> coll = u.field("coll", String.class);
        return ctx.select(tbl, scheme, segment, coll, u.field("ctype", String.class),
                          u.field("n", Integer.class), DSL.count().over())
            .from(u)
            .orderBy(tbl, scheme, coll, segment)
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
            var rows = listQuery(ctx, tenant, limit + 1, gate3).fetch();
            int count = rows.isEmpty() ? 0 : rows.get(0).get(6, Integer.class);
            boolean capped = count > limit;
            int n = Math.min(rows.size(), limit);
            List<Group> groups = new ArrayList<>(n);
            for (int i = 0; i < n; i++) {
                var r = rows.get(i);
                groups.add(new Group(r.get(0, String.class), r.get(1, String.class), r.get(2, String.class),
                                     r.get(3, String.class), r.get(4, String.class), r.get(5, Integer.class)));
            }
            return new Page(count, limit, capped, groups);
        });
    }
}
