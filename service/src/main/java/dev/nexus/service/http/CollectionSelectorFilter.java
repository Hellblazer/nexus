// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
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
import dev.nexus.service.db.CollectionSuccessor;
import dev.nexus.service.db.ExpectedCollectionIds;
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
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
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
 *   <li>404 {@code unknown_collection_id}, naming the id: the tenant has no row with that id, and only then (linda's D3 ruling,
 *       2026-11-23);</li>
 *   <li>409 {@code collection_superseded} on EVERY route, the delete included, when the id names a retired row (a rename's or
 *       supersede's tombstone, the id of a collection that no longer answers to it). The body ({@link CollectionSupersededBody})
 *       names the retired row ({@code collection}, {@code collection_id}) and its live successor, found by
 *       {@code superseded_by_id} and not by the tombstone's label text ({@link CollectionSuccessor}): {@code superseded_by} is the
 *       successor's current label, which is the key the client's {@code superseded_write_error_from_body} reads, and
 *       {@code successor_collection_id} its id. A tombstone that names no live successor (the successor was deleted, even if its
 *       label was taken again) answers 409 with neither field. A retired LABEL is not this filter's: it is not an id, and the
 *       delete keeps its 200 for it;</li>
 *   <li>(A request by id alone is never refused for its label: the id is the identity. A one-row rename that lands between this
 *       filter and the write's registry read changes nothing about where the write goes, and the write stamps the row's current
 *       label from its locked read. A request that sent BOTH an id and a label is a 400 here, so none reaches the engine's 409
 *       {@code collection_id_mismatch} for an id and a label that no longer agree, which {@link ExpectedCollectionIds#set} is ready
 *       for.)</li>
 *   <li>500 {@code collection_id_mismatch}: the registry answered an id with a row of another id. A registry inconsistency, an
 *       engine defect and not a caller mistake, so a 500 and never a 409; nothing is retried or guessed. No request reaches it
 *       today, since a read by id selects on the key.</li>
 * </ul>
 * A label that no row has keeps the handler's own answer (422 {@code unregistered_collection} on the routes that refuse one).
 *
 * <p><b>The delete route gets the id as well as the label.</b> A label can be held by more than one live row only in a state
 * P4.1 forbids and an operator may still need to clear, and a delete by id must remove that one row; so on the delete route the
 * filter also writes {@value #RESOLVED_ID}, which the handler reads. A client-supplied {@value #RESOLVED_ID} is removed
 * from every body of that route first, so only this filter can set it. Every body is parsed (never a byte search for the id
 * field, which an escaped key defeats); a body that is not JSON goes to the handler as it came.
 *
 * <p><b>The id travels on to the handler.</b> The handler works by label, and between this filter and the handler an id could
 * be freed and its label taken by another row, so a write by the id would land in that row. So the filter names every id it
 * resolved, by label, in {@link ExpectedCollectionIds} (a {@link ThreadLocal} cleared in a {@code finally}; the handler runs on
 * this thread), and while it does, {@code CollectionRegistry} resolves THAT id inside the handler's transaction,
 * {@code FOR KEY SHARE}, never the label: no row is 404, a retired row 409, a live row proceeds under its own label (linda's
 * ruling on the route boundary's gap). A request by label is untouched.
 *
 * <p>A body is scanned for the id fields with a streaming parser (no document tree is built unless one is found), so a large
 * chunk body that names no id costs one pass over its tokens. A body is rewritten through {@link HttpExchange#setStreams}, which keeps the exchange object (the before-send hooks of the
 * vector routes are keyed by it); only a query string needs a wrapper, because the request URI cannot be replaced otherwise.
 */
public final class CollectionSelectorFilter extends Filter {

    private static final Logger log = LoggerFactory.getLogger(CollectionSelectorFilter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The id the filter resolved on the delete route, a JSON integer in the body; never accepted from a client. */
    static final String RESOLVED_ID = "resolved_id";

    /** An id has at most 18 decimal digits, in a query string and in a body alike (the same bound, so both answer 400 above it). */
    private static final Pattern DECIMAL = Pattern.compile("-?[0-9]{1,18}");
    private static final long MAX_ID = 999_999_999_999_999_999L;

    /** {@code reason} of the 500 for a registry row that is not the row of the id asked for. Wire contract. */
    static final String COLLECTION_ID_MISMATCH_REASON = HttpUtil.COLLECTION_ID_MISMATCH_REASON;

    /**
     * Test seams of THIS filter (one per engine, so a test class that runs beside another cannot take their hook). All null in
     * production. {@code afterResolve} runs after the boundary has named the ids and before the handler;
     * {@code beforeTransactionRead} runs once per request, just before the first by-id read a write transaction makes (after every
     * outside-a-transaction lookup passed); {@code unconsumed} receives a report line for each write route that answered 2xx without
     * reading a named id in its own transaction.
     */
    private volatile Runnable afterResolve;
    private volatile Runnable beforeTransactionRead;
    private volatile Runnable afterTransactionRead;
    private volatile java.util.function.Consumer<String> unconsumedSink;
    private volatile java.util.function.Consumer<String> censusSink;

    /** Sets this filter's test seams (any may be null); {@code afterTransactionRead} runs with the by-id read's lock held. */
    public void testHooks(Runnable afterResolve, Runnable beforeTransactionRead, Runnable afterTransactionRead,
                          java.util.function.Consumer<String> unconsumedSink) {
        this.afterResolve = afterResolve;
        this.beforeTransactionRead = beforeTransactionRead;
        this.afterTransactionRead = afterTransactionRead;
        this.unconsumedSink = unconsumedSink;
        ExpectedCollectionIds.setCensus(unconsumedSink != null);   // the per-transaction census runs only while a test listens
    }

    /** The routes that write under a collection they name by id (a test reads it to say which routes a census must have run); the table's own set. */
    public static java.util.Set<String> writeRoutePaths() {
        return CollectionSelectors.WRITES;
    }

    /**
     * Sets the sink of the per-transaction census's counts (a test seam, null in production): one line per write route request that
     * named an id, {@code "<path> examined=<n> exempt=<n> unknown=<n>"}, with the census on, so a test can refuse a pass that
     * examined no write transaction (the vacuous-gate rule).
     */
    public void censusSink(java.util.function.Consumer<String> sink) {
        this.censusSink = sink;
    }

    /** How the filter reads rows: a seam, so a test can hand it a row the registry never would. */
    interface Rows {
        /** The row of {@code id}, retired or not. @throws UnknownCollectionIdException when the tenant has none */
        CollectionRow byId(String tenant, long id);

        /** The live successor the retired row {@code retiredId} names by {@code superseded_by_id}, if it names one. */
        Optional<CollectionSuccessor.Successor> successorOf(String tenant, long retiredId);
    }

    private final Rows rows;

    public CollectionSelectorFilter(CollectionRegistry registry, TenantScope scope) {
        this(new Rows() {
            @Override public CollectionRow byId(String tenant, long id) {
                return registry.lookupId(scope, tenant, id);
            }

            @Override public Optional<CollectionSuccessor.Successor> successorOf(String tenant, long retiredId) {
                return CollectionSuccessor.of(scope, tenant, retiredId);
            }
        });
    }

    CollectionSelectorFilter(Rows rows) {
        this.rows = rows;
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
            if (route == null && ExpectedCollectionIds.censusOn() && hasBody(exchange.getRequestMethod())
                && exchange.getRequestURI().getPath().startsWith("/v1/catalog/")) {
                documentIdCensus(exchange, chain);
                return;
            }
            chain.doFilter(exchange);
            return;
        }
        HttpExchange next = exchange;
        Resolver resolver = new Resolver(tenant);
        try {
            if (route.where() == CollectionSelectors.Where.QUERY
                || (route.where() == CollectionSelectors.Where.EITHER && !hasBody(exchange.getRequestMethod()))) {
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
        try {
            ExpectedCollectionIds.set(resolver.expected, beforeTransactionRead, afterTransactionRead);
            Runnable hook = afterResolve;
            if (hook != null) {
                hook.run();
            }
            chain.doFilter(next);
        } finally {
            if (route.writes()) {
                reportUnconsumed(next);
            }
            ExpectedCollectionIds.clear();
        }
    }

    /**
     * The document routes name their collection by {@code physical_collection_id} in the body, in the transaction that writes (they
     * are not rows of the route table), so this filter names nothing for them and the census would not see them. With the census on, a
     * catalog write whose body carries that field anywhere is given a carrier that names each id as {@code #<id>}, the key
     * {@code CollectionRegistry#requireIdForWrite} consumes, and the request-level report applies as for any write route: a 2xx that no
     * transaction read the id for is reported. The per-transaction finding is not applied to them: a document write also writes owner and
     * link rows in transactions of their own that name no collection. Test only: nothing here runs with the census off.
     */
    private void documentIdCensus(HttpExchange exchange, Chain chain) throws IOException {
        byte[] in = exchange.getRequestBody().readAllBytes();
        exchange.setStreams(new ByteArrayInputStream(in), null);
        Map<String, Long> named = new LinkedHashMap<>();
        try {
            collectDocumentIds(MAPPER.readTree(in), named);
        } catch (IOException notJson) {
            // the handler refuses a body that is not JSON in its own words
        }
        if (named.isEmpty()) {
            chain.doFilter(exchange);
            return;
        }
        try {
            ExpectedCollectionIds.setDocumentIds(named, beforeTransactionRead, afterTransactionRead);   // the test seams reach document writes too
            chain.doFilter(exchange);
        } finally {
            reportUnconsumed(exchange);
            ExpectedCollectionIds.clear();
        }
    }

    private static void collectDocumentIds(JsonNode node, Map<String, Long> out) {
        if (node == null) {
            return;
        }
        if (node instanceof ObjectNode object) {
            JsonNode id = object.get("physical_collection_id");
            if (id != null && id.isIntegralNumber()) {
                out.put("#" + id.asLong(), id.asLong());
            }
        }
        for (JsonNode child : node) {
            collectDocumentIds(child, out);
        }
    }

    /**
     * A write route that answered 2xx while a named id was never read in its own transaction went on by label: the gap this
     * filter's ids exist to close is open on that route. ERROR in the log, and the test sink when a test set one.
     */
    private void reportUnconsumed(HttpExchange exchange) {
        List<String> left = ExpectedCollectionIds.unconsumed();
        int status = exchange.getResponseCode();
        String path = exchange.getRequestURI().getPath();
        java.util.function.Consumer<String> sink = unconsumedSink;
        java.util.function.Consumer<String> counts = censusSink;
        if (counts != null && status >= 200 && status < 300) {
            int[] n = ExpectedCollectionIds.censusCounts();
            counts.accept(path + " examined=" + n[0] + " exempt=" + n[1] + " unknown=" + n[2]);
        }
        if (status < 200 || status >= 300) {
            return;
        }
        // the per-transaction census: a transaction that wrote and read none of the named ids (the request-level check below
        // passes when ANY transaction of the request read one)
        for (String finding : ExpectedCollectionIds.censusFindings()) {
            log.error("event=collection_id_not_consumed_by_write_transaction path={} finding={}", LogText.escapeControl(path), finding);
            if (sink != null) {
                sink.accept(path + " " + finding);
            }
        }
        if (left.isEmpty()) {
            return;
        }
        // WARN, not ERROR: a by-id write_many whose every document fails requireDocumentExists (no such document) answers 200 without a
        // transaction that reached its registry read, which is no defect; the test sink and the census assert the strict reading
        log.warn("event=collection_id_not_consumed path={} labels={} status={}", LogText.escapeControl(path), left.size(), status);
        if (sink != null) {
            sink.accept(path + " " + left);
        }
    }

    private static boolean hasBody(String method) {
        return "POST".equalsIgnoreCase(method) || "PUT".equalsIgnoreCase(method) || "PATCH".equalsIgnoreCase(method)
            || "DELETE".equalsIgnoreCase(method);
    }

    // ── id to label ────────────────────────────────────────────────────────────────────────────────────

    /** Resolves ids through the registry for one request, and keeps the label each resolved to. */
    private final class Resolver {
        private final String tenant;
        /** The id each label resolved to in this request, for {@link ExpectedCollectionIds}; dies with the request. */
        final Map<String, Long> expected = new LinkedHashMap<>();

        Resolver(String tenant) {
            this.tenant = tenant;
        }

        CollectionRow resolve(long id) {
            CollectionRow row;
            try {
                row = rows.byId(tenant, id);
            } catch (UnknownCollectionIdException e) {
                throw unknown(id);
            }
            if (row.collectionId() != id) {
                log.error("event=collection_id_mismatch tenant={} requested={} resolved={}", tenant, id, row.collectionId());
                throw new Refusal(500, mismatchBody(id, row.collectionId()));
            }
            if (row.isSuperseded()) {
                throw new Refusal(409, CollectionSupersededBody.of(id, row.label(), rows.successorOf(tenant, id)));
            }
            Long prior = expected.putIfAbsent(row.label(), id);
            if (prior != null && prior != id) {
                throw bad("collection_ids " + prior + " and " + id + " name the one label '" + row.label() + "'");
            }
            return row;
        }
    }

    /** The 500 body of a registry row that is not the row of the id asked for: {@code reason} {@code collection_id_mismatch}. */
    static String mismatchBody(long requested, long resolved) {
        return mismatchBody(requested, resolved, "collection_id " + requested + " resolved to a registry row of collection_id " + resolved);
    }

    /** {@link #mismatchBody(long, long)} with the words of the mismatch (another id, or another label for the id). */
    static String mismatchBody(long requested, long resolved, String error) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", error);
        body.put("reason", COLLECTION_ID_MISMATCH_REASON);
        body.put("collection_id", requested);
        body.put("resolved_collection_id", resolved);
        return json(body);
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
            int idCount = 0;
            for (String pair : pairs) {
                String[] kv = pair.split("=", 2);
                String key = URLDecoder.decode(kv[0], StandardCharsets.UTF_8);
                String value = kv.length > 1 ? URLDecoder.decode(kv[1], StandardCharsets.UTF_8) : "";
                if (key.equals(slot.idField())) {
                    idCount++;
                    idValue = idValue == null ? value : idValue;
                    continue;
                }
                // a label field that is present counts, empty or not: a body's "" is non-null and is "both" there too
                labelGiven |= slot.labelFields().contains(key);
                kept.add(pair);
            }
            if (idCount == 0) {
                continue;
            }
            if (idCount > 1) {
                throw bad(slot.idField() + " must be given once");
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
        // A token scan, not a document tree: it finds the id fields (decoded, so an escaped key is still one), the delete
        // route's resolved_id, and a duplicate of an id key, and skips every other value whole. The tree is built only on a hit.
        // A body that is not JSON goes to the handler, which refuses it in its own words; a duplicate id key is this filter's own
        // refusal (a Refusal, not an IOException), so the pass-through below never swallows it.
        boolean hit;
        try {
            hit = scan(route, in);
        } catch (IOException e) {
            return in;
        }
        if (!hit) {
            return in;
        }
        JsonNode root = MAPPER.readTree(in);
        if (!(root instanceof ObjectNode body)) {
            return in;
        }
        boolean changed = route.passesResolvedId() && body.remove(RESOLVED_ID) != null;
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

    /**
     * One streaming pass over the top level of a request body: true when it names an id field of the route (or, on the delete,
     * {@value #RESOLVED_ID}); 400 when an id key is given twice in one object (a query string answers the same, and the tree
     * reader would keep the last silently). An {@link IOException} is a body that is not JSON.
     */
    private static boolean scan(CollectionSelectors.Route route, byte[] in) throws IOException {
        Set<String> topIds = new HashSet<>();
        Set<String> nestedIds = new HashSet<>();
        Set<String> nestedArrays = new HashSet<>();
        for (CollectionSelectors.Slot slot : route.slots()) {
            if (slot.nested()) {
                nestedArrays.add(slot.nestedIn());
                nestedIds.add(slot.idField());
            } else {
                topIds.add(slot.idField());
            }
        }
        boolean hit = false;
        Set<String> seen = new HashSet<>();
        try (JsonParser p = MAPPER.getFactory().createParser(in)) {
            if (p.nextToken() != JsonToken.START_OBJECT) {
                return false;
            }
            while (p.nextToken() == JsonToken.FIELD_NAME) {
                String name = p.currentName();
                JsonToken value = p.nextToken();
                if (topIds.contains(name)) {
                    if (!seen.add(name)) {
                        throw bad(name + " must be given once");
                    }
                    hit = true;
                } else if (route.passesResolvedId() && RESOLVED_ID.equals(name)) {
                    hit = true;
                } else if (value == JsonToken.START_ARRAY && nestedArrays.contains(name)) {
                    hit |= scanRecords(p, nestedIds);
                    continue;   // the array is consumed
                }
                p.skipChildren();
            }
        }
        return hit;
    }

    /** The elements of a nested array, positioned on its START_ARRAY: true when one names an id field; 400 for a repeated key. */
    private static boolean scanRecords(JsonParser p, Set<String> idFields) throws IOException {
        boolean hit = false;
        while (p.nextToken() != JsonToken.END_ARRAY) {
            if (p.currentToken() != JsonToken.START_OBJECT) {
                p.skipChildren();
                continue;
            }
            Set<String> seen = new HashSet<>();
            while (p.nextToken() == JsonToken.FIELD_NAME) {
                String name = p.currentName();
                p.nextToken();
                if (idFields.contains(name)) {
                    if (!seen.add(name)) {
                        throw bad(name + " must be given once");
                    }
                    hit = true;
                }
                p.skipChildren();
            }
        }
        return hit;
    }

    /** Replaces {@code slot}'s id in {@code node} by its label; true when it changed {@code node}. */
    private boolean rewriteField(CollectionSelectors.Slot slot, Resolver resolver, ObjectNode node,
                                 CollectionSelectors.Route route) {
        if (!node.has(slot.idField())) {
            return false;
        }
        JsonNode idNode = node.remove(slot.idField());
        if (idNode == null || idNode.isNull()) {
            // an explicit null is not an id, and silently reading it as "no id" would let the label path run on a request that
            // named a collection field it did not mean
            throw bad(slot.idField() + (slot.list() ? " must be an array of integers" : " must be a JSON integer"));
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
        if (route.passesResolvedId()) {
            node.put(RESOLVED_ID, id);
        }
        return true;
    }

    private static long integer(CollectionSelectors.Slot slot, JsonNode value) {
        if (!value.isIntegralNumber() || !value.canConvertToLong()) {
            throw bad(slot.idField() + " must be a JSON integer");
        }
        long id = value.longValue();
        if (id < -MAX_ID || id > MAX_ID) {
            throw bad(slot.idField() + " must be a decimal integer of at most 18 digits");   // as a query string answers
        }
        return id;
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
