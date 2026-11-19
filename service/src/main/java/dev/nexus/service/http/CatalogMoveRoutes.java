// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogMoves;

import java.io.IOException;
import java.io.InputStream;
import java.util.Map;

/**
 * RDR-254: the two move routes, {@code POST /v1/catalog/documents/move} and {@code POST /v1/catalog/owners/move}. Each
 * reads its request, validates the shape, and answers {@link CatalogMoves}' report as one JSON object; the transaction
 * is {@link CatalogMoves}'. A refusal ({@code CatalogRepository.MergeRefused}) answers 409 through the handler's ladder
 * and a blank or malformed field 400. {@code dry_run} has no default: a request that leaves it out is a 400, so a caller
 * never writes (or never fails to write) by omission.
 */
final class CatalogMoveRoutes {

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final CatalogMoves moves;

    CatalogMoveRoutes(CatalogMoves moves) {
        this.moves = moves;
    }

    /**
     * POST /v1/catalog/documents/move {@code {tumbler, new_source_uri, new_file_path, dry_run}}: re-point a curator
     * document at a new path, absorbing a live row already registered there. Response: the counts (repointed, absorbed,
     * trashed, skipped, unchanged, aspect and highlight rows rewritten or deleted, links remapped, collapsed or dropped)
     * and {@code absorbed_tumblers}, every absorbed tumbler.
     */
    void documentsMove(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String tumbler = text(body, "tumbler");
        if (tumbler == null) { error(exchange, "'tumbler' required"); return; }
        if (!TumblerGrammar.isDocumentTumbler(tumbler)) { grammar(exchange, "tumbler", tumbler); return; }
        String uri = text(body, "new_source_uri");
        if (uri == null) { error(exchange, "'new_source_uri' required"); return; }
        String path = text(body, "new_file_path");
        if (path == null) { error(exchange, "'new_file_path' required"); return; }
        if (!(body.get("dry_run") instanceof Boolean dryRun)) { error(exchange, "'dry_run' must be a boolean"); return; }
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(
            moves.moveDocument(tenant, tumbler, uri, path, dryRun).toMap()));
    }

    /**
     * POST /v1/catalog/owners/move {@code {owner, new_root, new_repo_hash, dry_run}}: keep a repo owner's identity when its
     * repository moves, folding any owner already registered at the new root into it and rewriting its documents' URIs.
     * Response: the counts of {@link #documentsMove} plus {@code owners_folded}.
     */
    void ownersMove(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String owner = text(body, "owner");
        if (owner == null) { error(exchange, "'owner' required"); return; }
        if (!TumblerGrammar.isOwnerPrefix(owner)) { grammar(exchange, "owner", owner); return; }
        String root = text(body, "new_root");
        if (root == null) { error(exchange, "'new_root' required"); return; }
        String hash = text(body, "new_repo_hash");
        if (hash == null) { error(exchange, "'new_repo_hash' required"); return; }
        if (!(body.get("dry_run") instanceof Boolean dryRun)) { error(exchange, "'dry_run' must be a boolean"); return; }
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(
            moves.moveOwner(tenant, owner, root, hash, dryRun).toMap()));
    }

    private static String text(Map<String, Object> body, String key) {
        return body.get(key) instanceof String s && !s.isBlank() ? s : null;
    }

    private static void error(HttpExchange exchange, String message) throws IOException {
        HttpUtil.send(exchange, 400, "{\"error\":" + CatalogHandler.MAPPER.writeValueAsString(message) + "}");
    }

    private static void grammar(HttpExchange exchange, String field, String value) throws IOException {
        HttpUtil.send(exchange, 400,
            "{\"error\":\"tumbler grammar violation\",\"rule\":\"tumbler-grammar\",\"field\":"
            + CatalogHandler.MAPPER.writeValueAsString(field) + ",\"value\":" + CatalogHandler.MAPPER.writeValueAsString(value) + "}");
    }

    private static Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return CatalogHandler.MAPPER.readValue(bytes, MAP_TYPE);
        }
    }
}
