// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantKeyedState;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.BooleanSupplier;

/**
 * {@code POST /v1/_test/tenant-reset} (bead nexus-0bl77, rtg20 phase A; design T2
 * {@code nexus/design-nexus-rtg20-data-reset-pool-2026-10-18}). A test harness that resets a tenant IN PLACE (deletes
 * its rows and re-issues its token, instead of dropping and re-minting the tenant and its partitions) calls this after
 * the DELETE, so the engine forgets everything it cached about that tenant: it runs every {@link TenantKeyedState} for
 * the tenant and answers with the per-evictor counts.
 *
 * <p>Registered by {@code NexusService} ONLY when {@code NX_TEST_TENANT_RESET_ROUTE=1}; otherwise the path is simply
 * absent (404). Boot refuses the flag on a non-loopback bind, {@code /v1/status} reports
 * {@code test_tenant_reset_route} so a cloud gate can assert it is false there, and this handler adds its own
 * guards: operator (root token) only, and 503 while {@code NX_SEARCH_PCI=1}, because per-collection indexes are DDL
 * that a data reset does not drop. The default tenant is never reset.
 *
 * <p>Request: {@code {"tenant": "<name>"}}. Response 200:
 * {@code {"tenant": "<name>", "evicted": {"<evictor>": <count>, ...}}} naming every evictor, in registration order.
 * If an evictor throws, the others still run and the answer is 500 with the counts so far and a {@code failed} object;
 * the caller retires the tenant. The tuple evictor throws when waiters are still registered after its bound, so a
 * 200 means the tenant's waits were all gone, and a 500 naming {@code TupleWaitRegistry} means they were not.
 *
 * <p><b>This is not a quiesce.</b> The route evicts what the engine remembers; it cannot stop a client. The caller
 * must stop its own clients of the tenant (parked waits, writers, retrying readers) BEFORE it calls the route: a
 * request that arrives after the eviction repopulates the caches from the tenant's next-test state, and a wait
 * registered after the tuple groups are dropped is a live wait of the next test.
 */
public final class TenantResetHandler implements HttpHandler {

    public static final String PATH = "/v1/_test/tenant-reset";

    private static final Logger log = LoggerFactory.getLogger(TenantResetHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    private final List<TenantKeyedState> evictors;
    private final BooleanSupplier pciEnabled;

    public TenantResetHandler(List<TenantKeyedState> evictors, BooleanSupplier pciEnabled) {
        this.evictors = List.copyOf(evictors);
        this.pciEnabled = pciEnabled;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (RequestContext.tenant() == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: auth principal not set\"}");
            return;
        }
        if (!"POST".equals(exchange.getRequestMethod().toUpperCase(Locale.ROOT))) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        if (!RequestContext.isOperator()) {
            log.debug("event=tenant_reset_denied reason=operator_required tenant={}", RequestContext.tenant());
            HttpUtil.send(exchange, 403, json(Map.of(
                "error", "operator privilege required: this operation needs the root token")));
            return;
        }
        if (pciEnabled.getAsBoolean()) {
            HttpUtil.send(exchange, 503, json(Map.of("error",
                "tenant reset refused: NX_SEARCH_PCI=1 builds per-collection indexes, which are DDL a data reset "
                    + "does not drop")));
            return;
        }
        String tenant;
        try {
            tenant = requestedTenant(exchange);
        } catch (IllegalArgumentException | com.fasterxml.jackson.core.JsonProcessingException e) {
            HttpUtil.send(exchange, 400, json(Map.of("error", String.valueOf(e.getMessage()))));
            return;
        }
        if (TenantConstants.DEFAULT_TENANT.equals(tenant) || TenantConstants.BOOTSTRAP_ANY_TENANT.equals(tenant)) {
            HttpUtil.send(exchange, 400, json(Map.of("error",
                "refused: the default and wildcard tenants are never reset")));
            return;
        }

        Map<String, Object> evicted = new LinkedHashMap<>();
        Map<String, String> failed = new LinkedHashMap<>();
        for (TenantKeyedState state : evictors) {
            try {
                evicted.put(state.name(), state.evictTenant(tenant));
            } catch (RuntimeException e) {
                log.error("event=tenant_reset_evictor_failed evictor={} tenant={}", state.name(), tenant, e);
                failed.put(state.name(), String.valueOf(e));
            }
        }
        log.info("event=tenant_reset tenant={} evicted={} failed={}", tenant, evicted, failed.keySet());
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("tenant", tenant);
        body.put("evicted", evicted);
        if (!failed.isEmpty()) {
            body.put("failed", failed);
        }
        HttpUtil.send(exchange, failed.isEmpty() ? 200 : 500, json(body));
    }

    private static String requestedTenant(HttpExchange exchange) throws IOException {
        byte[] bytes = exchange.getRequestBody().readAllBytes();
        Map<String, Object> body = bytes.length == 0
            ? Map.of()
            : MAPPER.readValue(new String(bytes, StandardCharsets.UTF_8), MAP_TYPE);
        if (!(body.get("tenant") instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("missing required string field: tenant");
        }
        return s;
    }

    private static String json(Object value) throws IOException {
        return MAPPER.writeValueAsString(value);
    }
}
