// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CatalogSensitiveReads;
import dev.nexus.service.db.ManifestFillableCount;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-f3yqd: the catalog's bounded read routes added in engine 0.1.188, split out of {@link CatalogHandler} (which
 * is at its file-size cap). Each is read-only, tenant-scoped, and runs under the section-label count's statement
 * bound ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}); a read that outlasts it answers 503
 * {@code {"error":..., "reason":"statement_timeout"}} with none of the answer. A client that gets a 404 is on an
 * engine before the route and one that gets a 503 is on an engine that could not answer in time; both must read
 * "cannot say", never "none".
 */
final class CatalogBoundedReadRoutes {

    private static final Logger log = LoggerFactory.getLogger(CatalogBoundedReadRoutes.class);

    private final CatalogSensitiveReads sensitiveReads;
    private final ManifestFillableCount fillableCount;

    CatalogBoundedReadRoutes(CatalogSensitiveReads sensitiveReads, ManifestFillableCount fillableCount) {
        this.sensitiveReads = sensitiveReads;
        this.fillableCount = fillableCount;
    }

    /**
     * GET /v1/catalog/sensitive?limit=N: {@code {count, limit, capped, documents:[{tumbler, physical_collection}]}}.
     *
     * <p>{@code count} is every live sensitive document of the tenant; {@code documents} lists at most {@code limit}
     * of them in tumbler order, and {@code capped} is true when more than {@code limit} exist. {@code limit} is 1 to
     * {@link CatalogSensitiveReads#MAX_LIMIT} (default {@link CatalogSensitiveReads#DEFAULT_LIMIT}); anything else is
     * a 400. A new path rather than a parameter on {@code /list}: an old engine would ignore the parameter and answer
     * a 200 that looks like "no sensitive documents" (the {@code /docs/collection-counts-all} rule, nexus-8tnz2).
     */
    void sensitive(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = limitParam(exchange, CatalogSensitiveReads.DEFAULT_LIMIT, CatalogSensitiveReads.MAX_LIMIT);
        if (limit == null) return;
        CatalogSensitiveReads.Page page;
        try {
            page = sensitiveReads.list(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_sensitive_list_timeout tenant={} limit={}", tenant, limit);
            timeout(exchange, "sensitive list exceeded its time bound");
            return;
        }
        List<Map<String, Object>> docs = new ArrayList<>(page.documents().size());
        for (var d : page.documents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", d.tumbler());
            m.put("physical_collection", d.physicalCollection());
            docs.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", page.count());
        body.put("limit", page.limit());
        body.put("capped", page.capped());
        body.put("documents", docs);
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/manifest/fillable?kind=labels|spans&limit=N:
     * {@code {kind, fillable, limit, capped, rule:"title rule 2", stamp:"strict", edge}}.
     *
     * <p>The strict, bounded count of the manifest rows the two upgrade rungs would fill ({@link ManifestFillableCount}
     * has the predicate). {@code kind} is required and exact ({@code labels} or {@code spans}); {@code limit} is 1 to
     * {@link ManifestFillableCount#MAX_LIMIT} (default {@link ManifestFillableCount#DEFAULT_LIMIT}); anything else is a
     * 400. The scan stops at {@code limit} candidate rows (rows that pass every guard but the edges below, strict or not);
     * {@code capped} is true when it did, and then BOTH {@code fillable} and {@code edge} are lower bounds ("at least
     * these") and a client MUST read capped as pending whatever the numbers say (capped with {@code fillable} 0 and
     * {@code edge} equal to {@code limit} included). Only an uncapped answer is exact.
     * The older {@code /manifest/section_label_fillable} stays for old clients; it counts a chunk stamp it cannot read,
     * this route does not ({@code stamp:"strict"}).
     *
     * <p>{@code edge} is informational: the count (cut at {@code limit} too) of rows that pass every guard of
     * {@code fillable} EXCEPT an edge where the SQL form cannot say what the Python rung says: a chunk stamp the strict
     * form rejects but {@code fromisoformat} may read, or a label or page value that is not a string, null or integer
     * (including falsy non-strings Python turns into the empty label) or does not fit INTEGER. Every such row is one the
     * strict {@code fillable} leaves out, so {@code fillable == 0} alone can be a false "converged". A client reads
     * {@code fillable == 0 && edge > 0} (uncapped) as "scan once to see" (the scan's completion record then ends it) and
     * uncapped {@code fillable == 0 && edge == 0} as converged; {@code fillable > 0} is pending whatever {@code edge} says.
     */
    void fillable(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String rawKind = queryParam(exchange, "kind");
        ManifestFillableCount.Kind kind = rawKind == null ? null : ManifestFillableCount.Kind.parse(rawKind);
        if (kind == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"kind is required and must be labels or spans\"}");
            return;
        }
        Integer limit = limitParam(exchange, ManifestFillableCount.DEFAULT_LIMIT, ManifestFillableCount.MAX_LIMIT);
        if (limit == null) return;
        ManifestFillableCount.Result result;
        try {
            result = fillableCount.count(tenant, kind, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_manifest_fillable_timeout tenant={} kind={} limit={}", tenant, kind.wire(), limit);
            timeout(exchange, "manifest fillable count exceeded its time bound");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("kind", result.kind().wire());
        body.put("fillable", result.fillable());
        body.put("limit", result.limit());
        body.put("capped", result.capped());
        body.put("rule", ManifestFillableCount.RULE);
        body.put("stamp", ManifestFillableCount.STAMP);
        body.put("edge", result.edge());
        send(exchange, 200, body);
    }

    // ── shared ───────────────────────────────────────────────────────────────

    /** The {@code limit} query parameter, {@code def} when absent; null after answering a 400 for a bad one. */
    static Integer limitParam(HttpExchange exchange, int def, int max) throws IOException {
        String raw = queryParam(exchange, "limit");
        if (raw == null || raw.isBlank()) return def;
        int limit;
        try {
            limit = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be an integer (1.." + max + ")\"}");
            return null;
        }
        if (limit < 1 || limit > max) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be 1.." + max + "\"}");
            return null;
        }
        return limit;
    }

    static String queryParam(HttpExchange exchange, String key) {
        String query = exchange.getRequestURI().getRawQuery();
        if (query == null) return null;
        for (String part : query.split("&")) {
            String[] kv = part.split("=", 2);
            if (kv.length == 2 && kv[0].equals(key)) {
                return java.net.URLDecoder.decode(kv[1], java.nio.charset.StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    static void timeout(HttpExchange exchange, String message) throws IOException {
        HttpUtil.send(exchange, 503, "{\"error\":" + HttpUtil.jsonString(message) + ",\"reason\":\"statement_timeout\"}");
    }

    static void send(HttpExchange exchange, int status, Map<String, Object> body) throws IOException {
        try {
            HttpUtil.send(exchange, status, CatalogHandler.MAPPER.writeValueAsString(body));
        } catch (JsonProcessingException e) {
            throw new IOException(e);
        }
    }
}
