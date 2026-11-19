// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogRepository;

import java.io.IOException;
import java.io.InputStream;
import java.util.Map;

/**
 * The owner routes of {@link CatalogHandler} that carry no coverage or tumbler-grammar logic of the handler's own
 * (RDR-254 P1-S2): the bodies moved out of {@code CatalogHandler} so it has room under its size ceiling for the move
 * routes. {@code CatalogHandler} keeps a one-line method of the same name for each, because
 * {@code CatalogHandlerEnvelopeConformanceGateTest} scans it for them. The bodies are unchanged.
 */
final class CatalogOwnerRoutes {

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final CatalogRepository repo;

    CatalogOwnerRoutes(CatalogRepository repo) {
        this.repo = repo;
    }

    private static Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return CatalogHandler.MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    /**
     * GET /v1/catalog/owners/list[?include_deactivated=true] — nexus-cw262: the
     * audit escape hatch. Default (param absent or any value other than
     * {@code "true"}) excludes deactivated owners.
     */
    void list(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        boolean includeDeactivated = "true".equals(CatalogBoundedReadRoutes.queryParam(exchange, "include_deactivated"));
        var owners = repo.listOwners(tenant, includeDeactivated);
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    /**
     * POST /v1/catalog/owners/deactivate {"tumbler_prefix": X} — nexus-cw262: the
     * 7kl32 dead-owner GC mutation arm's engine half. Soft-delete, mirrors {@code
     * /delete}'s shape (idempotent 0-return; reversible). Response: {@code
     * {"deactivated": 0|1}}.
     */
    void deactivate(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String prefix = (String) body.get("tumbler_prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumbler_prefix' required\"}"); return;
        }
        int deactivated = repo.deactivateOwner(tenant, prefix);
        HttpUtil.send(exchange, 200, "{\"deactivated\":" + deactivated + "}");
    }

    /**
     * POST /v1/catalog/owners/reactivate {"tumbler_prefix": X} — nexus-cw262: the
     * explicit manual-correction path (clears deactivated_at without a full
     * re-registration). Response: {@code {"reactivated": 0|1}}.
     */
    void reactivate(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String prefix = (String) body.get("tumbler_prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumbler_prefix' required\"}"); return;
        }
        int reactivated = repo.reactivateOwner(tenant, prefix);
        HttpUtil.send(exchange, 200, "{\"reactivated\":" + reactivated + "}");
    }

    /**
     * POST /v1/catalog/owners/sweep_next_seq_drift — the nexus-0ehwe item 5 converge verb.
     *
     * <p>Floors every drifted owner's {@code next_seq} in the tenant to its own high-water
     * mark in one pass and reports which owners were actually below it, so a drift
     * incident's blast radius is KNOWN rather than guessed (nexus-pbawi's owner 1.12 was
     * found only because an operator happened to suspect it). No request body is read; the
     * sweep always covers the whole tenant.
     */
    void sweepNextSeqDrift(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var report = repo.sweepNextSeqDrift(tenant);
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(report));
    }

    void byRepo(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String repoHash = CatalogBoundedReadRoutes.queryParam(exchange, "repo_hash");
        if (repoHash == null || repoHash.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"repo_hash required\"}"); return;
        }
        var owner = repo.ownerByRepoHash(tenant, repoHash);
        if (owner == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(owner));
    }

    void byName(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String name = CatalogBoundedReadRoutes.queryParam(exchange, "name");
        if (name == null || name.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"name required\"}"); return;
        }
        var owners = repo.ownersByName(tenant, name);
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    void headHash(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String prefix   = (String) body.get("tumbler_prefix");
        String headHash = (String) body.get("head_hash");
        if (prefix == null || headHash == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler_prefix and head_hash required\"}"); return;
        }
        int updated = repo.setOwnerHeadHash(tenant, prefix, headHash);
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
    }

    /** GET /v1/catalog/owners/show?tumbler_prefix=X — get owner by tumbler_prefix. */
    void show(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String prefix = CatalogBoundedReadRoutes.queryParam(exchange, "tumbler_prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler_prefix required\"}"); return;
        }
        var owner = repo.ownerByPrefix(tenant, prefix);
        if (owner == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(owner));
    }

    /**
     * POST /v1/catalog/owners/by_type {"owner_type": X, "include_deactivated": bool}
     * — list owners filtered by owner_type. nexus-cw262: {@code include_deactivated}
     * defaults false (excludes deactivated owners) — this is the exact read path the
     * 7kl32 census and doctor's git-hooks dead-owner attribution both use.
     */
    void byType(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String ownerType = (String) body.get("owner_type");
        if (ownerType == null || ownerType.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"owner_type required\"}"); return;
        }
        Object includeDeactivatedRaw = body.get("include_deactivated");
        boolean includeDeactivated = includeDeactivatedRaw instanceof Boolean b && b;
        var owners = repo.ownersByType(tenant, ownerType, includeDeactivated);
        HttpUtil.send(exchange, 200, CatalogHandler.MAPPER.writeValueAsString(Map.of("owners", owners)));
    }
}
