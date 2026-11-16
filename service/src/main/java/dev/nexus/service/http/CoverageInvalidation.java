// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.TaxonomyCoverage;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * RDR-134 Step 2 (nexus-7fku2.1.2): drops the {@link TaxonomyCoverage} entries a mutating route may have changed, for a
 * handler whose routes are a switch on the path ({@code TaxonomyHandler}, {@code CatalogHandler}).
 *
 * <p>{@link #arm} runs first for a route in {@code ops}; it hooks the response, so the entries are dropped as the
 * response goes out, AFTER the route's repository call has committed and before the client can read again. {@link
 * #finish} runs in the handler's {@code finally}: it drops them again (a handler that threw before sending) and
 * forgets the exchange. A route's body names the collections it changed ({@link #noteBody}); when it names none,
 * the whole tenant is dropped, because under-invalidating hides chunks and over-invalidating costs a recount.
 *
 * <p>The collections are held by exchange identity, not as an {@code HttpExchange} attribute: the JDK server's
 * attribute map is the context's, shared by every request (see {@link HttpUtil#armBeforeSend}).
 */
final class CoverageInvalidation {

    private static final List<String> COLLECTION_KEYS = List.of("collection", "collection_name", "old", "new");

    private final TaxonomyCoverage coverage;
    private final Set<String> ops;
    private final ConcurrentHashMap<HttpExchange, List<String>> named = new ConcurrentHashMap<>();

    /** {@code coverage} may be null (a handler built without a cache): every method is then a no-op. */
    CoverageInvalidation(TaxonomyCoverage coverage, Set<String> ops) {
        this.coverage = coverage;
        this.ops = ops;
    }

    /** True when {@code op} changes a coverage input and the hook is armed; pass the result to {@link #finish}. */
    boolean arm(HttpExchange exchange, String op, String tenant) {
        if (coverage == null || !ops.contains(op)) {
            return false;
        }
        HttpUtil.armBeforeSend(exchange, status -> drop(exchange, tenant));
        return true;
    }

    /** Remember the collections {@code body} names, for the drop. Call it with every parsed request body. */
    void noteBody(HttpExchange exchange, Map<String, Object> body) {
        if (coverage == null) {
            return;
        }
        List<String> names = new ArrayList<>();
        for (String key : COLLECTION_KEYS) {
            if (body.get(key) instanceof String name) {
                names.add(name);
            }
        }
        named.put(exchange, names);
    }

    /** The handler's {@code finally}: drop again if armed, discard the hook, forget the exchange. */
    void finish(HttpExchange exchange, String tenant, boolean armed) {
        if (armed) {
            HttpUtil.discardBeforeSend(exchange);
            drop(exchange, tenant);
        }
        named.remove(exchange);
    }

    private void drop(HttpExchange exchange, String tenant) {
        List<String> collections = named.get(exchange);
        if (collections == null || collections.isEmpty()) {
            coverage.invalidateTenant(tenant);
        } else {
            collections.forEach(c -> coverage.invalidate(tenant, c));
        }
    }
}
