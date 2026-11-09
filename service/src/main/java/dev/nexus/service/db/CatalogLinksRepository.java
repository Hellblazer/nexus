// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import org.jooq.Condition;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;

import java.sql.SQLException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS_UPSERT_MANY;

/**
 * RDR-193 Part A4 (nexus-u5m5g.4): the index-time linking calls, split out of {@link CatalogRepository} (which is over
 * the file-size cap): the batch link upsert, the same-content linker and the combined list read. Each runs in
 * {@code tenantScope.withTenant}, so row-level security scopes every row to the caller's tenant.
 */
public final class CatalogLinksRepository {

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LIST_TYPE = new TypeReference<>() {};

    /** SQLSTATE the functions raise for a malformed request ({@code invalid_parameter_value}). */
    private static final String INVALID_PARAMETER = "22023";

    private final TenantScope tenantScope;

    public CatalogLinksRepository(CatalogRepository base) {
        this.tenantScope = base.tenantScope();
    }

    /**
     * Batch link upsert: {@code nexus.catalog_links_upsert_many}, one transaction. Each edge is
     * {@code {from, to, link_type, created_by, from_span?, to_span?}} and each endpoint is {@code {tumbler}} or
     * {@code {owner, file_path}}; the function resolves every endpoint (path endpoints by identity first, through the
     * named owner's repo_root, then by that owner's file_path), locks the resolved rows in sorted order and inserts what
     * resolved, leaving an existing link as it is.
     *
     * @return {@code {created, skipped, unresolved}} where {@code unresolved} is a list of
     *         {@code {index, side, reason}} (reason {@code dangling} or {@code no_path}); a malformed edge is an
     *         {@link IllegalArgumentException} (the function raises SQLSTATE 22023)
     */
    public Map<String, Object> linksUpsertMany(String tenant, List<Map<String, Object>> edges) {
        if (edges == null) {
            throw new IllegalArgumentException("'edges' must be a list");
        }
        final String json;
        try {
            json = CatalogRepository.MAPPER.writeValueAsString(edges);
        } catch (JsonProcessingException e) {
            throw new IllegalArgumentException("'edges' is not JSON: " + e.getMessage(), e);
        }
        return tenantScope.withTenant(tenant, ctx -> {
            var up = CATALOG_LINKS_UPSERT_MANY.call(JSONB.valueOf(json));
            Record r;
            try {
                r = ctx.selectFrom(up).fetchOne();
            } catch (DataAccessException e) {
                for (Throwable t = e; t != null; t = t.getCause()) {
                    if (t instanceof SQLException se && INVALID_PARAMETER.equals(se.getSQLState())) {
                        String m = CatalogRepository.serverMessageOf(t);
                        throw new IllegalArgumentException(m == null ? "invalid edges" : m, e);
                    }
                }
                throw e;
            }
            JSONB unresolved = r.get(up.UNRESOLVED);
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("created", r.get(up.CREATED));
            out.put("skipped", r.get(up.SKIPPED));
            try {
                out.put("unresolved", unresolved == null ? List.of()
                    : CatalogRepository.MAPPER.readValue(unresolved.data(), LIST_TYPE));
            } catch (JsonProcessingException e) {
                throw new IllegalStateException("unresolved is not JSON", e);
            }
            return out;
        });
    }

    /**
     * The same-content linker: {@code nexus.catalog_link_same_content}, one transaction. Each given live pdf or paper
     * member links {@code same-as} to the least tumbler of its content-hash group (created_by
     * {@code content_hash_dedup}); with {@code prune}, the dedup links touching the given tumblers whose endpoints no
     * longer share a settled key are first retracted, one row each (links have no deleted_at, so a retraction is a hard
     * DELETE, as {@code CatalogRepository.deleteLink} is), a link another creator also asserted being kept.
     *
     * @return {@code {created, retracted}}, plus {@code refused: "empty_fetch"} when the prune found candidate links and
     *         no live pdf or paper document
     */
    public Map<String, Object> linkSameContent(String tenant, List<String> tumblers, boolean prune) {
        if (tumblers == null) {
            throw new IllegalArgumentException("'tumblers' must be a list");
        }
        String[] arr = tumblers.toArray(new String[0]);
        return tenantScope.withTenant(tenant, ctx -> {
            JSONB out = dev.nexus.service.jooq.nexus.Routines.catalogLinkSameContent(ctx.configuration(), arr, prune);
            try {
                return CatalogRepository.MAPPER.readValue(out.data(), MAP_TYPE);
            } catch (JsonProcessingException e) {
                throw new IllegalStateException("same_content outcome is not JSON", e);
            }
        });
    }

    /** {@code like} pattern for a literal prefix: backslash, {@code %} and {@code _} escaped with a backslash. */
    private static String likePrefix(String prefix) {
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%";
    }

    /**
     * The combined list read: live documents of one owner prefix whose content_type is any of {@code types}, and/or whose
     * source_uri starts with {@code sourceUriPrefix}; every filter given is applied, a null or empty one is not.
     * {@code limit <= 0} is unbounded (nexus-xoimv), ordered by tumbler. Both the owner prefix and the URI prefix are
     * matched literally ({@code %} and {@code _} escaped). The URI prefix is a file: URI prefix and is not
     * canonicalized (a note URI is matched whole, by {@code CatalogRepository.documentsBySourceUri}).
     */
    public List<Map<String, Object>> documentsByOwnerContentTypes(
            String tenant, String ownerPrefix, List<String> types, String sourceUriPrefix, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition filter = DSL.noCondition();
            if (ownerPrefix != null && !ownerPrefix.isBlank()) {
                filter = filter.and(CATALOG_DOCUMENTS.TUMBLER.like(likePrefix(ownerPrefix + "."), '\\'));
            }
            if (types != null && !types.isEmpty()) {
                filter = filter.and(CATALOG_DOCUMENTS.CONTENT_TYPE.in(types));
            }
            if (sourceUriPrefix != null && !sourceUriPrefix.isBlank()) {
                filter = filter.and(CATALOG_DOCUMENTS.SOURCE_URI.like(likePrefix(sourceUriPrefix), '\\'));
            }
            return ctx.select(CatalogRepository.documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.DELETED_AT.isNull().and(filter))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : Integer.MAX_VALUE)
               .offset(offset)
               .fetch().map(r -> CatalogRepository.docRowFromRecord(r.intoMap()));
        });
    }
}
