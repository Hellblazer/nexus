// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.Chash;
import dev.nexus.service.resolver.ResolveResult;
import dev.nexus.service.resolver.UnknownSchemeException;
import dev.nexus.service.resolver.UriSchemeResolverRegistry;
import dev.nexus.service.vectors.PgVectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * URI-scheme resolver HTTP endpoint (RDR-169 G3, bead nexus-aphki) — the
 * read-time resolution surface {@link UriSchemeResolverRegistry} was built
 * for but, until this handler, had nothing calling it in production.
 *
 * <p>Route: {@code POST /v1/vectors/resolve}. Registered as its own context
 * (a longer prefix than {@code /v1/vectors}), so {@code com.sun.net.httpserver}'s
 * longest-prefix-match routing dispatches here rather than into {@link
 * VectorHandler}'s default 404 arm — the two handlers never see each other's
 * requests.
 *
 * <h2>Request body — two forms</h2>
 * <pre>
 *   {"source_uri": "chroma://coll/chash"}         // resolve a URI directly
 *   {"collection": "...", "chash": "&lt;64-hex&gt;"}   // resolve via a chunk's metadata
 * </pre>
 *
 * <p>The {@code (collection, chash)} form looks up the chunk through {@link
 * PgVectorRepository#get}'s existing metadata read (no new SQL), reads
 * {@code metadata.source_uri} from the returned row, and resolves THAT URI.
 * This is the reference-only chunk's own address record (RDR-169 G4): a
 * reference-only row stores no {@code chunk_text}, only a precomputed
 * embedding and metadata naming where the real content lives. Retention is
 * derived from whether the row's {@code chunk_text} came back null — the
 * {@code chunk_text IS NULL <=> retention='reference-only'} CHECK
 * biconditional (RDR-169 Phase B fix round 1 item 5) makes this exact
 * without a second read of the {@code retention} column.
 *
 * <h2>Response</h2>
 * <p>200: {@code {"content": "...", "source_uri": "...", "retention": "..."}}
 * — {@code retention} is present only for the {@code (collection, chash)}
 * form (the {@code source_uri} form has no row to report retention on).
 *
 * <p>Errors:
 * <ul>
 *   <li>400 — malformed request (both forms given, neither given, bad chash).</li>
 *   <li>404 — {@code (collection, chash)} form: no such chunk, or the chunk
 *       carries no {@code metadata.source_uri} to resolve.</li>
 *   <li>422 — the URI's scheme has no registered handler ({@link
 *       UnknownSchemeException}); names the scheme and the registered set.
 *       This is the honest answer for a client-side scheme ({@code file://},
 *       {@code obsidian://}, {@code x-devonthink-item://}) — a managed engine
 *       cannot reach a tenant's local machine, and the registry is scoped to
 *       server-reachable schemes only (see its class javadoc).</li>
 *   <li>502 — the registered handler could not resolve the URI (a dangling
 *       {@code https://} URL, a missing {@code chroma://} row) — carries the
 *       handler's {@code errorReason}/{@code errorDetail}, never a stack trace.</li>
 *   <li>503 — {@code (collection, chash)} form with no {@link
 *       PgVectorRepository} wired (matches {@link VectorHandler}'s
 *       absent-backend pattern).</li>
 * </ul>
 */
public final class ResolveHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ResolveHandler.class);

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final UriSchemeResolverRegistry registry;
    private final PgVectorRepository pgRepo;

    /**
     * @param registry the scheme registry (must not be null — the route has nothing
     *                 to dispatch to without one)
     * @param pgRepo   backs the {@code (collection, chash)} form's metadata read; may
     *                 be null — that form then answers 503, matching {@link
     *                 VectorHandler}'s absent-backend pattern. The {@code source_uri}
     *                 form works regardless (it never touches {@code pgRepo} directly;
     *                 a {@code chroma://} URI still routes through the registry's own
     *                 handler, which may itself be absent — that path answers 422).
     */
    public ResolveHandler(UriSchemeResolverRegistry registry, PgVectorRepository pgRepo) {
        if (registry == null) {
            throw new IllegalArgumentException("registry must not be null");
        }
        this.registry = registry;
        this.pgRepo = pgRepo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        try {
            requireMethod(exchange, method, "POST");
            String tenant = requireTenant(exchange);
            Map<String, Object> body = readBody(exchange);

            String sourceUri  = optString(body, "source_uri");
            String collection = optString(body, "collection");
            String chash      = optString(body, "chash");

            if (sourceUri != null && (collection != null || chash != null)) {
                throw new IllegalArgumentException(
                    "resolve: pass 'source_uri' OR ('collection' + 'chash'), not both");
            }

            if (sourceUri != null) {
                resolveAndRespond(exchange, sourceUri, tenant, null);
                return;
            }

            if (collection == null || chash == null) {
                throw new IllegalArgumentException(
                    "resolve: requires 'source_uri', or both 'collection' and 'chash'");
            }
            Chash.requireCanonical(chash, "chash");
            resolveByChash(exchange, tenant, collection, chash);
        } catch (SkipHandlerException e) {
            // Response already sent (405 / 401 guard) — nothing further.
        } catch (IllegalArgumentException e) {
            log.debug("event=resolve_bad_request error={}", e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "resolve_handler", "")) {
                log.error("event=resolve_handler_error", e);
                HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
            }
        }
    }

    /**
     * The {@code (collection, chash)} form: fetch the chunk's document + metadata
     * through {@link PgVectorRepository#get} (existing single-id read, no new SQL),
     * derive retention from whether the document came back null, then resolve the
     * chunk's {@code metadata.source_uri}.
     */
    private void resolveByChash(HttpExchange exchange, String tenant, String collection, String chash)
            throws IOException {
        if (pgRepo == null) {
            HttpUtil.send(exchange, 503, json(Map.of(
                "error", "resolve by (collection, chash) requires a pgvector repository")));
            return;
        }

        Map<String, Object> lookup = pgRepo.get(tenant, collection, List.of(chash), 1, 0);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) lookup.get("ids");
        if (ids == null || ids.isEmpty()) {
            HttpUtil.send(exchange, 404, json(Map.of(
                "error", "no chunk found for collection='" + collection + "' chash='" + chash + "'")));
            return;
        }

        @SuppressWarnings("unchecked")
        List<String> documents = (List<String>) lookup.get("documents");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metadatas = (List<Map<String, Object>>) lookup.get("metadatas");
        String chunkText = documents.get(0);
        Map<String, Object> metadata = metadatas.get(0);
        // chunk_text IS NULL <=> retention='reference-only' (CHECK biconditional,
        // RDR-169 Phase B fix round 1 item 5) — derivable without a second read of
        // the retention column itself.
        String retention = chunkText != null ? "full" : "reference-only";

        Object rawSourceUri = metadata != null ? metadata.get("source_uri") : null;
        if (!(rawSourceUri instanceof String resolvedSourceUri) || resolvedSourceUri.isBlank()) {
            HttpUtil.send(exchange, 404, json(Map.of(
                "error", "chunk (" + collection + ", " + chash + ") carries no metadata.source_uri")));
            return;
        }

        resolveAndRespond(exchange, resolvedSourceUri, tenant, retention);
    }

    /**
     * Dispatch {@code uri} through the registry and write the response. {@code
     * retention}, when non-null, is the retention of the ROW the caller looked up
     * by chash (not of whatever {@code uri} itself resolves to) — absent entirely
     * for the {@code source_uri} request form, which names no row.
     */
    private void resolveAndRespond(HttpExchange exchange, String uri, String tenant, String retention)
            throws IOException {
        ResolveResult result;
        try {
            result = registry.resolve(uri, tenant);
        } catch (UnknownSchemeException e) {
            HttpUtil.send(exchange, 422, json(Map.of(
                "error", "no handler registered for scheme '" + e.scheme() + "'; registered schemes: "
                         + registry.registeredSchemes(),
                "scheme", e.scheme(),
                "registered_schemes", registry.registeredSchemes())));
            return;
        }

        if (!result.isOk()) {
            HttpUtil.send(exchange, 502, json(Map.of(
                "error", result.errorReason(),
                "detail", result.errorDetail())));
            return;
        }

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("content", result.text());
        response.put("source_uri", result.sourceUri());
        if (retention != null) {
            response.put("retention", retention);
        }
        HttpUtil.send(exchange, 200, json(response));
    }

    // ── Request parsing / shared helpers (mirrors VectorHandler's idiom) ────────

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream is = exchange.getRequestBody()) {
            byte[] bytes = is.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private String optString(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        String s = val.toString();
        return s.isBlank() ? null : s;
    }

    private void requireMethod(HttpExchange exchange, String actual, String expected) throws IOException {
        if (!expected.equalsIgnoreCase(actual)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            throw new SkipHandlerException();
        }
    }

    /**
     * The SERVER-RESOLVED tenant for this request (same defense-in-depth guard as
     * {@link VectorHandler#requireTenant} — AuthFilter already rejects unauthenticated
     * requests before this handler runs).
     */
    private String requireTenant(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null || tenant.isBlank()) {
            HttpUtil.send(exchange, 401, json(Map.of("error", "no resolved tenant for request")));
            throw new SkipHandlerException();
        }
        return tenant;
    }

    private String json(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (Exception e) {
            log.error("event=json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
    }

    private static final class SkipHandlerException extends RuntimeException {
        SkipHandlerException() { super(null, null, true, false); }
    }
}
