// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnknownCollectionIdException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23), the route boundary of "every collection-taking route accepts {@code collection_id}
 * and a label" (RDR item 6: a label "is resolved to the id once, by {@code CollectionRegistry}"). For each route of {@link
 * CollectionSelectors} whose request names a collection by id, this filter resolves the id through the registry and replaces
 * the id field with the label field the handler already reads; the handler then runs on the label as it always did. A request
 * that names only labels is not touched, so an old client sees no change on any route.
 *
 * <p>What it refuses, before the handler runs, and only for id input:
 * <ul>
 *   <li>400: a label and an id for one collection, or an id that is not an integer (a JSON integer in a body, a decimal in a
 *       query string; in a list, every element);</li>
 *   <li>404 {@code unknown_collection_id}, naming the id: the tenant has no row with that id, or the row is retired (a rename's
 *       or supersede's tombstone: the id of a collection that no longer answers to it). The delete route alone resolves a
 *       retired row, because it refuses it with its own typed 409 naming the successor.</li>
 * </ul>
 * A label that no row has keeps the handler's own answer (422 {@code unregistered_collection} on the routes that refuse one).
 *
 * <p><b>The delete route gets the id as well as the label.</b> A label can be held by more than one live row only in a state
 * P4.1 forbids and an operator may still need to clear, and a delete by id must remove that one row; so on the delete route the
 * filter also writes {@value #RESOLVED_ID}, which the handler reads. A client-supplied {@value #RESOLVED_ID} is removed
 * from every body of that route first, so only this filter can set it.
 *
 * <p>A body is rewritten through {@link HttpExchange#setStreams}, which keeps the exchange object (the before-send hooks of the
 * vector routes are keyed by it); only a query string needs a wrapper, because the request URI cannot be replaced otherwise.
 */
public final class CollectionSelectorFilter extends Filter {

    private static final Logger log = LoggerFactory.getLogger(CollectionSelectorFilter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The id the filter resolved on the delete route, a JSON integer in the body; never accepted from a client. */
    static final String RESOLVED_ID = "resolved_id";

    private static final Pattern DECIMAL = Pattern.compile("-?[0-9]{1,18}");

    private final CollectionRegistry registry;
    private final TenantScope scope;

    public CollectionSelectorFilter(CollectionRegistry registry, TenantScope scope) {
        this.registry = registry;
        this.scope = scope;
    }

    @Override
    public String description() {
        return "resolves collection_id to the collection's label on the collection-taking routes";
    }

    /** A request this filter answers itself: the status and the JSON body. */
    private static final class Refusal extends RuntimeException {
        final int status;
        final String body;

        Refusal(int status, String body) {
            super(body, null, false, false);
            this.status = status;
            this.body = body;
        }
    }

    @Override
    public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
        CollectionSelectors.Route route = CollectionSelectors.ROUTES.get(exchange.getRequestURI().getPath());
        String tenant = RequestContext.tenant();
        if (route == null || tenant == null) {
            chain.doFilter(exchange);
            return;
        }
        HttpExchange next = exchange;
        try {
            Resolver resolver = new Resolver(tenant, route);
            if (route.where() == CollectionSelectors.Where.QUERY) {
                String rewritten = rewriteQuery(route, resolver, exchange.getRequestURI().getRawQuery());
                if (rewritten != null) {
                    next = new WithQuery(exchange, rewritten);
                }
            } else if (hasBody(exchange.getRequestMethod())) {
                byte[] in = exchange.getRequestBody().readAllBytes();
                exchange.setStreams(new ByteArrayInputStream(rewriteBody(route, resolver, in)), null);
            }
        } catch (Refusal refusal) {
            HttpUtil.send(exchange, refusal.status, refusal.body);
            return;
        } catch (RuntimeException e) {
            String context = "path=" + LogText.escapeControl(exchange.getRequestURI().getPath());
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "collection_selector", context)) {
                log.error("event=collection_selector_error {} error={}", context, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
            return;
        }
        chain.doFilter(next);
    }

    private static boolean hasBody(String method) {
        return "POST".equalsIgnoreCase(method) || "PUT".equalsIgnoreCase(method) || "PATCH".equalsIgnoreCase(method)
            || "DELETE".equalsIgnoreCase(method);
    }

    // ── id to label ────────────────────────────────────────────────────────────────────────────────────

    /** Resolves ids through the registry for one request. */
    private final class Resolver {
        private final String tenant;
        private final CollectionSelectors.Route route;

        Resolver(String tenant, CollectionSelectors.Route route) {
            this.tenant = tenant;
            this.route = route;
        }

        CollectionRow resolve(long id) {
            CollectionRow row;
            try {
                row = registry.lookupId(scope, tenant, id);
            } catch (UnknownCollectionIdException e) {
                throw unknown(id);
            }
            if (row.isSuperseded() && !route.tombstoneOk()) {
                throw unknown(id);
            }
            return row;
        }
    }

    private static Refusal unknown(long id) {
        return new Refusal(404, unknownBody(id));
    }

    /** The typed 404 body of an id no row has: {@code error} in words, {@code reason}, and the {@code collection_id}. */
    static String unknownBody(long id) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", "collection_id " + id + " is not a registered collection");
        body.put("reason", HttpUtil.UNKNOWN_COLLECTION_ID_REASON);
        body.put("collection_id", id);
        return json(body);
    }

    private static Refusal bad(String message) {
        return new Refusal(400, json(Map.of("error", message)));
    }

    private static String json(Map<String, Object> body) {
        try {
            return MAPPER.writeValueAsString(body);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Refusal both(CollectionSelectors.Slot slot) {
        return bad("give either '" + slot.labelField() + "' or '" + slot.idField() + "' for the collection, not both");
    }

    // ── a query string ─────────────────────────────────────────────────────────────────────────────────

    private String rewriteQuery(CollectionSelectors.Route route, Resolver resolver, String raw) {
        if (raw == null || raw.isEmpty()) {
            return null;
        }
        List<String> pairs = new ArrayList<>(List.of(raw.split("&")));
        boolean changed = false;
        for (CollectionSelectors.Slot slot : route.slots()) {
            String idValue = null;
            boolean labelGiven = false;
            List<String> kept = new ArrayList<>(pairs.size());
            boolean idSeen = false;
            for (String pair : pairs) {
                String[] kv = pair.split("=", 2);
                String key = URLDecoder.decode(kv[0], StandardCharsets.UTF_8);
                String value = kv.length > 1 ? URLDecoder.decode(kv[1], StandardCharsets.UTF_8) : "";
                if (key.equals(slot.idField())) {
                    idSeen = true;
                    idValue = idValue == null ? value : idValue;
                    continue;
                }
                labelGiven |= slot.labelFields().contains(key) && !value.isBlank();
                kept.add(pair);
            }
            if (!idSeen) {
                continue;
            }
            if (!DECIMAL.matcher(idValue).matches()) {
                throw bad(slot.idField() + " must be a decimal integer");
            }
            if (labelGiven) {
                throw both(slot);
            }
            kept.add(slot.labelField() + "=" + URLEncoder.encode(resolver.resolve(Long.parseLong(idValue)).label(),
                StandardCharsets.UTF_8));
            pairs = kept;
            changed = true;
        }
        return changed ? String.join("&", pairs) : null;
    }

    // ── a JSON body ────────────────────────────────────────────────────────────────────────────────────

    private byte[] rewriteBody(CollectionSelectors.Route route, Resolver resolver, byte[] in) throws IOException {
        // The delete route's bodies are tiny and are always read, so a client's resolved_id never gets past an escaped key;
        // every other route's body is parsed only when it mentions one of the route's id fields.
        if (!route.tombstoneOk() && !mentionsAnIdField(route, in)) {
            return in;
        }
        JsonNode root;
        try {
            root = MAPPER.readTree(in);
        } catch (IOException e) {
            return in;   // not JSON: the handler refuses it in its own words
        }
        if (!(root instanceof ObjectNode body)) {
            return in;
        }
        boolean changed = route.tombstoneOk() && body.remove(RESOLVED_ID) != null;
        for (CollectionSelectors.Slot slot : route.slots()) {
            if (slot.nested()) {
                if (body.get(slot.nestedIn()) instanceof ArrayNode elements) {
                    for (JsonNode element : elements) {
                        if (element instanceof ObjectNode record) {
                            changed |= rewriteField(slot, resolver, record, route);
                        }
                    }
                }
            } else {
                changed |= rewriteField(slot, resolver, body, route);
            }
        }
        return changed ? MAPPER.writeValueAsBytes(body) : in;
    }

    private static boolean mentionsAnIdField(CollectionSelectors.Route route, byte[] body) {
        for (CollectionSelectors.Slot slot : route.slots()) {
            if (contains(body, ("\"" + slot.idField() + "\"").getBytes(StandardCharsets.UTF_8))) {
                return true;
            }
        }
        return false;
    }

    private static boolean contains(byte[] haystack, byte[] needle) {
        outer:
        for (int i = 0; i + needle.length <= haystack.length; i++) {
            for (int j = 0; j < needle.length; j++) {
                if (haystack[i + j] != needle[j]) {
                    continue outer;
                }
            }
            return true;
        }
        return false;
    }

    /** Replaces {@code slot}'s id in {@code node} by its label; true when it changed {@code node}. */
    private boolean rewriteField(CollectionSelectors.Slot slot, Resolver resolver, ObjectNode node,
                                 CollectionSelectors.Route route) {
        if (!node.has(slot.idField())) {
            return false;
        }
        JsonNode idNode = node.remove(slot.idField());
        if (idNode == null || idNode.isNull()) {
            return true;   // an explicit null names nothing; the id field itself must not reach the handler
        }
        for (String labelField : slot.labelFields()) {
            if (node.hasNonNull(labelField)) {
                throw both(slot);
            }
        }
        if (slot.list()) {
            if (!(idNode instanceof ArrayNode ids)) {
                throw bad(slot.idField() + " must be an array of integers");
            }
            ArrayNode labels = MAPPER.createArrayNode();
            for (JsonNode element : ids) {
                labels.add(resolver.resolve(integer(slot, element)).label());
            }
            node.set(slot.labelField(), labels);
            return true;
        }
        long id = integer(slot, idNode);
        node.put(slot.labelField(), resolver.resolve(id).label());
        if (route.tombstoneOk()) {
            node.put(RESOLVED_ID, id);
        }
        return true;
    }

    private static long integer(CollectionSelectors.Slot slot, JsonNode value) {
        if (!value.isIntegralNumber() || !value.canConvertToLong()) {
            throw bad(slot.idField() + " must be a JSON integer");
        }
        return value.longValue();
    }

    // ── the request URI cannot be replaced on the exchange, so a rewritten query string is served by a wrapper ───────

    /** An exchange that answers {@code getRequestURI} with the rewritten query; every other call is the original's. */
    private static final class WithQuery extends HttpExchange {
        private final HttpExchange delegate;
        private final URI uri;

        WithQuery(HttpExchange delegate, String rawQuery) {
            this.delegate = delegate;
            this.uri = URI.create(delegate.getRequestURI().getRawPath() + "?" + rawQuery);
        }

        @Override public URI getRequestURI() { return uri; }
        @Override public Headers getRequestHeaders() { return delegate.getRequestHeaders(); }
        @Override public Headers getResponseHeaders() { return delegate.getResponseHeaders(); }
        @Override public String getRequestMethod() { return delegate.getRequestMethod(); }
        @Override public HttpContext getHttpContext() { return delegate.getHttpContext(); }
        @Override public void close() { delegate.close(); }
        @Override public InputStream getRequestBody() { return delegate.getRequestBody(); }
        @Override public OutputStream getResponseBody() { return delegate.getResponseBody(); }
        @Override public void sendResponseHeaders(int code, long length) throws IOException {
            delegate.sendResponseHeaders(code, length);
        }
        @Override public InetSocketAddress getRemoteAddress() { return delegate.getRemoteAddress(); }
        @Override public int getResponseCode() { return delegate.getResponseCode(); }
        @Override public InetSocketAddress getLocalAddress() { return delegate.getLocalAddress(); }
        @Override public String getProtocol() { return delegate.getProtocol(); }
        @Override public Object getAttribute(String name) { return delegate.getAttribute(name); }
        @Override public void setAttribute(String name, Object value) { delegate.setAttribute(name, value); }
        @Override public void setStreams(InputStream i, OutputStream o) { delegate.setStreams(i, o); }
        @Override public HttpPrincipal getPrincipal() { return delegate.getPrincipal(); }
    }
}
