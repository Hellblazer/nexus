// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.RequestLedger;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.4): {@code GET /v1/requests/<request_id>}, the status of one
 * keyed request as the caller's tenant sees it. A client that lost a keyed response (a read
 * timeout, a cut connection) polls here instead of resending blind.
 *
 * <p>Always 200 for a well-formed id, with {@code state} one of {@code committed} (plus
 * {@code response_status} and the stored {@code response}), {@code in_flight} (plus
 * {@code started_at} and {@code lease_until}), {@code rolled_back} or {@code unknown}. Another
 * tenant's id reads {@code unknown}: the read runs under that tenant's stamp, so FORCE RLS hides
 * the row. 400 {@code malformed_request_id} for anything but a canonical UUID; 405 for any method
 * but GET. The route never answers 404 itself, so a client can read a 404 as "this engine has no
 * ledger".
 */
public final class RequestHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(RequestHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    static final String PREFIX = "/v1/requests/";

    /** Canonical 8-4-4-4-12 hex only; {@link UUID#fromString} alone accepts short forms like 1-1-1-1-1. */
    private static final Pattern CANONICAL_UUID = Pattern.compile(
        "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}");

    private final RequestLedger ledger;

    public RequestHandler(RequestLedger ledger) {
        this.ledger = ledger;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            log.error("event=request_handler_no_tenant path={}", exchange.getRequestURI().getPath());
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String path = exchange.getRequestURI().getPath();
        String tail = path.startsWith(PREFIX) ? path.substring(PREFIX.length()) : "";
        if (!CANONICAL_UUID.matcher(tail).matches()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"malformed_request_id\"}");
            return;
        }
        UUID requestId = UUID.fromString(tail);

        RequestLedger.Status status;
        try {
            status = ledger.status(tenant, requestId);
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "request_handler",
                    "tenant=" + tenant + " request_id=" + requestId)) {
                log.error("event=request_handler_error tenant={} request_id={} error={}",
                    tenant, requestId, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
            return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(body(requestId, status)));
    }

    private static ObjectNode body(UUID requestId, RequestLedger.Status status) throws IOException {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("request_id", requestId.toString());
        switch (status.kind()) {
            case COMMITTED -> {
                body.put("state", "committed");
                body.put("response_status", status.responseStatus());
                if (status.responseJson() == null) {
                    body.putNull("response");
                } else {
                    body.set("response", MAPPER.readTree(status.responseJson()));
                }
            }
            case IN_FLIGHT -> {
                body.put("state", "in_flight");
                body.put("started_at", status.startedAt().toString());
                body.put("lease_until", status.leaseUntil().toString());
            }
            case ROLLED_BACK -> body.put("state", "rolled_back");
            case UNKNOWN -> body.put("state", "unknown");
        }
        return body;
    }
}
