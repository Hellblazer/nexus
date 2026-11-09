// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogLinksRepository;

import java.io.IOException;
import java.io.InputStream;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * RDR-193 Part A4 (nexus-u5m5g.4): the route bodies of {@code POST /v1/catalog/links/upsert_many}, {@code POST
 * /v1/catalog/links/same_content} and the combined branch of {@code GET /v1/catalog/list}, split out of
 * {@link CatalogHandler} (which is over the file-size cap); the handler keeps the dispatch lines. A request of the wrong
 * shape is answered 400 here; an {@link IllegalArgumentException} from the repository reaches the handler's own ladder
 * (also 400).
 */
final class CatalogLinksHandler {

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** At most this many edges, or tumblers, in one call (the batch bind bound every sibling batch route shares). */
    static final int MAX_BATCH_DOC_IDS = 1000;

    private final CatalogLinksRepository links;

    CatalogLinksHandler(CatalogLinksRepository links) {
        this.links = links;
    }

    private static Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return CatalogHandler.MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private static void send(HttpExchange exchange, int status, String body) throws IOException {
        HttpUtil.send(exchange, status, body);
    }

    private static void error(HttpExchange exchange, int status, String message) throws IOException {
        send(exchange, status, "{\"error\":" + CatalogHandler.MAPPER.writeValueAsString(message) + "}");
    }

    /**
     * A deadlock, a serialization failure or a lock wait that ran out ended the call and nothing was written: 503 with
     * reason {@code transient_db_error}, the retryable answer the reconcile commit's poll gives, never a 500.
     */
    private static void transientFailure(HttpExchange exchange, CatalogLinksRepository.TransientDbException e) throws IOException {
        send(exchange, 503, "{\"error\":" + CatalogHandler.MAPPER.writeValueAsString(e.getMessage())
            + ",\"reason\":\"" + CatalogHandler.TRANSIENT_DB_ERROR + "\"}");
    }

    /**
     * POST /v1/catalog/links/upsert_many — the batch link upsert, one transaction. Body: {@code {"edges": [{"from": E,
     * "to": E, "link_type": "...", "created_by": "...", "from_span"?: "...", "to_span"?: "..."}, ...]}} where an
     * endpoint {@code E} is {@code {"tumbler": "..."}} or {@code {"owner": "...", "file_path": "..."}}. At most
     * {@value #MAX_BATCH_DOC_IDS} edges; more is a 400, as is an edge of the wrong shape (the 400 names its index).
     * There is no metadata and no allow_dangling.
     *
     * <p>Response: {@code {"created": N, "skipped": N, "unresolved": [{"index", "side", "reason"}]}}; {@code reason} is
     * {@code dangling} (a tumbler that is not a live document after alias resolution) or {@code no_path} (a path that
     * matched no document of the named owner or of its repository, or a path that leaves the owner's repo_root). An existing link is skipped and keeps its fields.
     * A path present only in another repository is {@code no_path}: the cross-repository drop is the design, not a gap.
     * A deadlock, a serialization failure or a lock wait that ran out answers 503 {@code transient_db_error}: nothing was
     * written, submit the call again.
     */
    void upsertMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        if (!(body.get("edges") instanceof List<?> edges)) {
            error(exchange, 400, "'edges' must be a list"); return;
        }
        if (edges.size() > MAX_BATCH_DOC_IDS) {
            error(exchange, 400, "too many edges (max " + MAX_BATCH_DOC_IDS + ")"); return;
        }
        List<Map<String, Object>> checked = new ArrayList<>(edges.size());
        for (int i = 0; i < edges.size(); i++) {
            String problem = edgeProblem(edges.get(i));
            if (problem != null) {
                error(exchange, 400, "edge " + i + ": " + problem); return;
            }
            @SuppressWarnings("unchecked") Map<String, Object> edge = (Map<String, Object>) edges.get(i);
            checked.add(edge);
        }
        try {
            send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(links.linksUpsertMany(tenant, checked)));
        } catch (CatalogLinksRepository.TransientDbException e) {
            transientFailure(exchange, e);
        }
    }

    /** Why {@code edge} is not a link edge, or null when it is: from and to endpoints, a link_type, optional strings. */
    private static String edgeProblem(Object edge) {
        if (!(edge instanceof Map<?, ?> m)) return "must be an object";
        for (String side : new String[] {"from", "to"}) {
            if (!(m.get(side) instanceof Map<?, ?> end)) return "'" + side + "' must be an object";
            boolean byTumbler = end.get("tumbler") instanceof String t && !t.isBlank();
            boolean byPath = end.get("owner") instanceof String o && !o.isBlank()
                && end.get("file_path") instanceof String f && !f.isBlank();
            if (end.get("tumbler") != null && !(end.get("tumbler") instanceof String)) return "'" + side + ".tumbler' must be a string";
            if (!byTumbler && !byPath) return "'" + side + "' must be {tumbler} or {owner, file_path}";
        }
        if (!(m.get("link_type") instanceof String lt) || lt.isBlank()) return "'link_type' must be a non-blank string";
        for (String key : new String[] {"created_by", "from_span", "to_span"}) {
            if (m.get(key) != null && !(m.get(key) instanceof String)) return "'" + key + "' must be a string";
        }
        return null;
    }

    /**
     * POST /v1/catalog/links/same_content — the same-content (same-as) linker. Body: {@code {"tumblers": [...],
     * "prune": true|false}} ({@code prune} defaults to false; at most {@value #MAX_BATCH_DOC_IDS} tumblers). Response:
     * {@code {"created": N, "retracted": N}}, plus {@code "refused": "empty_fetch"} when the prune found candidate links
     * and the tenant has no live pdf or paper.
     */
    void sameContent(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        if (!(body.get("tumblers") instanceof List<?> l)) {
            error(exchange, 400, "'tumblers' must be a list"); return;
        }
        if (l.stream().anyMatch(o -> !(o instanceof String s) || s.isBlank())) {
            error(exchange, 400, "every 'tumblers' element must be a non-blank string"); return;
        }
        if (l.size() > MAX_BATCH_DOC_IDS) {
            error(exchange, 400, "too many tumblers (max " + MAX_BATCH_DOC_IDS + ")"); return;
        }
        Object rawPrune = body.get("prune");
        if (rawPrune != null && !(rawPrune instanceof Boolean)) {
            error(exchange, 400, "'prune' must be a boolean"); return;
        }
        @SuppressWarnings("unchecked") List<String> tumblers = (List<String>) l;
        try {
            send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(
                links.linkSameContent(tenant, tumblers, Boolean.TRUE.equals(rawPrune))));
        } catch (CatalogLinksRepository.TransientDbException e) {
            transientFailure(exchange, e);
        }
    }

    // ── GET /list, the combined branch ──────────────────────────────────

    /** Every non-blank value of a repeated query parameter, in request order (empty when absent). */
    static List<String> queryParamAll(HttpExchange exchange, String key) {
        List<String> out = new ArrayList<>();
        String query = exchange.getRequestURI().getRawQuery();
        if (query == null) return out;
        for (String part : query.split("&")) {
            String[] kv = part.split("=", 2);
            if (kv.length == 2 && kv[0].equals(key)) {
                String v = URLDecoder.decode(kv[1], StandardCharsets.UTF_8);
                if (!v.isBlank()) out.add(v);
            }
        }
        return out;
    }

    /**
     * Whether {@code GET /list} takes the combined branch: an owner given with one or more {@code content_type} values
     * (so an owner plus one type now filters by the owner, which the content_type branch ignored), or a
     * {@code source_uri_prefix}. A request with neither reaches the branches it always reached.
     */
    static boolean wantsCombinedList(String owner, List<String> contentTypes, String sourceUriPrefix) {
        return sourceUriPrefix != null && !sourceUriPrefix.isBlank()
            || owner != null && !owner.isBlank() && !contentTypes.isEmpty();
    }

    /** The combined read; {@code limit <= 0} is unbounded, the rule the other filter branches follow. */
    List<Map<String, Object>> list(String tenant, String owner, List<String> contentTypes, String sourceUriPrefix,
                                   int limit, int offset) {
        return links.documentsByOwnerContentTypes(tenant, owner, contentTypes, sourceUriPrefix, limit, offset);
    }
}
