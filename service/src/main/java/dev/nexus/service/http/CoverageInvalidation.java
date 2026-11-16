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
import java.util.function.Supplier;

/**
 * RDR-134 Step 2 (nexus-7fku2.1.2): drops the {@link TaxonomyCoverage} entries a route in a listed set may have
 * changed, for a handler whose routes are a switch on the path ({@code TaxonomyHandler}, {@code CatalogHandler},
 * {@code VectorHandler}). It covers ONLY the routes the handler lists; the writers it does not reach are named in
 * {@link TaxonomyCoverage}'s class javadoc and rely on its ten-minute ceiling.
 *
 * <p><b>A route that answers in the same request</b> calls {@link #arm} first and {@link #finish} in its {@code
 * finally}. {@link #arm} hooks the response, so the entries are dropped as the response goes out, which is after the
 * route's repository call has returned (committed, or failed) and before the client can read again. {@link #finish}
 * drops them only if the hook did not run (an exception before any response, or a handler that armed a hook of its
 * own, as the keyed sweep forms of {@code VectorHandler} do, replacing this one): that drop then lands when the
 * handler returns, just after the response, not before it. It never drops twice, so it cannot wipe an entry the
 * client's next read has already counted.
 *
 * <p><b>A route that answers 202 and runs a background job</b> must NOT be listed: its response goes out before the
 * job writes anything, so a drop there is too early and nothing would drop when the job ends. The job's own body goes
 * through {@link #dropAfter}, which drops the tenant's entries in a {@code finally} when the job returns or throws.
 *
 * <p>A route's body names the collections it changed ({@link #noteBody}); when it names none, or when the route calls
 * {@link #widen} (it also writes onto collections it does not name), the whole tenant is dropped, because
 * under-invalidating hides chunks and over-invalidating costs one recount.
 *
 * <p>The collections are held by exchange identity, not as an {@code HttpExchange} attribute: the JDK server's
 * attribute map is the context's, shared by every request (see {@link HttpUtil#armBeforeSend}).
 */
final class CoverageInvalidation {

    /** Every body key the listed routes use for a collection they change (a rename names two, a restore its origin). */
    private static final List<String> COLLECTION_KEYS = List.of(
        "collection", "collection_name", "old", "new", "old_collection", "new_collection", "origin_collection");

    private final TaxonomyCoverage coverage;
    private final Set<String> ops;
    private final ConcurrentHashMap<HttpExchange, List<String>> named = new ConcurrentHashMap<>();
    /** The exchanges whose armed hook has run: their drop is done. */
    private final Set<HttpExchange> dropped = ConcurrentHashMap.newKeySet();

    /** {@code coverage} may be null (a handler built without a cache): every method is then a no-op. */
    CoverageInvalidation(TaxonomyCoverage coverage, Set<String> ops) {
        this.coverage = coverage;
        this.ops = ops;
    }

    /** True when {@code op} is listed and the hook is armed; pass the result to {@link #finish}. */
    boolean arm(HttpExchange exchange, String op, String tenant) {
        if (coverage == null || tenant == null || !ops.contains(op)) {
            return false;
        }
        HttpUtil.armBeforeSend(exchange, status -> {
            drop(exchange, tenant);
            dropped.add(exchange);
        });
        return true;
    }

    /** Remember the collections {@code body} names, for the drop. Call it with every parsed request body. */
    void noteBody(HttpExchange exchange, Map<String, Object> body) {
        if (coverage == null) {
            return;
        }
        named.put(exchange, collectionsNamedBy(body));
    }

    /** The collection names {@code body} carries under {@link #COLLECTION_KEYS}, in key order. */
    static List<String> collectionsNamedBy(Map<String, Object> body) {
        List<String> names = new ArrayList<>();
        for (String key : COLLECTION_KEYS) {
            if (body.get(key) instanceof String name) {
                names.add(name);
            }
        }
        return names;
    }

    /** The request also writes onto collections its body does not name: drop the whole tenant. Call it after {@link #noteBody}. */
    void widen(HttpExchange exchange) {
        if (coverage != null) {
            named.put(exchange, List.of());
        }
    }

    /** The handler's {@code finally}: drop if the hook never did, discard the hook, forget the exchange. */
    void finish(HttpExchange exchange, String tenant, boolean armed) {
        if (armed) {
            HttpUtil.discardBeforeSend(exchange);
            if (!dropped.remove(exchange)) {
                drop(exchange, tenant);
            }
        }
        named.remove(exchange);
    }

    /** Run a background job's body, then drop every entry of {@code tenant}, whether the body returned or threw. */
    <T> T dropAfter(String tenant, Supplier<T> job) {
        try {
            return job.get();
        } finally {
            if (coverage != null) {
                coverage.invalidateTenant(tenant);
            }
        }
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
