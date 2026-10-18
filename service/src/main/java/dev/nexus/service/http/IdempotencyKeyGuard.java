// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.util.Set;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.5): an {@code Idempotency-Key} on a route that cannot use it is a 400
 * {@code idempotency_key_unsupported} and nothing runs. The engine never ignores a key: an ignored key reads
 * {@code unknown} on {@code GET /v1/requests/<id>}, and a client that polls after a timeout would resend into a
 * second execution (nexus-zxgld gap 4; Sam's ruling 2026-10-18).
 *
 * <p>Registered behind {@link AuthFilter} on every authenticated context. Only the two sweep routes pass a key
 * through; each then judges its own shape (the bounded forms are keyed, the unbounded forms are not). A request
 * with no {@code Idempotency-Key} header is untouched.
 */
public final class IdempotencyKeyGuard extends Filter {

    /** The routes whose handlers judge a key themselves. */
    static final Set<String> KEYED_ROUTES = Set.of(VectorHandler.QUARANTINE_ROUTE, VectorHandler.RESTORE_ROUTE);

    @Override
    public String description() {
        return "refuses an Idempotency-Key on a route that cannot use it (RDR-222 Phase 1)";
    }

    @Override
    public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
        if (exchange.getRequestHeaders().getFirst(KeyedSweep.KEY_HEADER) != null
                && !KEYED_ROUTES.contains(exchange.getRequestURI().getPath())) {
            KeyedSweep.refuse(exchange, "idempotency_key_unsupported");
            return;
        }
        chain.doFilter(exchange);
    }
}
