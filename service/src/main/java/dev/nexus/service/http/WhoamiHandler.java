package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TenantScope;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.Map;

/**
 * GET /v1/_whoami — authenticated probe endpoint (skeleton only).
 *
 * <p>Exercises the full auth → tenant extraction → GUC stamp path.
 * Implemented via {@link TenantScope#withTenant} so the GUC is stamped before
 * any database work. Returns the tenant principal as JSON.
 *
 * <p>This endpoint is NOT part of the v1 public API surface (prefixed {@code _});
 * it exists to prove the skeleton contract end-to-end in tests. Real endpoints
 * ({@code memory_*} etc.) land in beads .7/.9.
 */
public final class WhoamiHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(WhoamiHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final TenantScope tenantScope;

    public WhoamiHandler(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }

        // Tenant was validated and extracted by AuthFilter
        String tenant = (String) exchange.getAttribute(AuthFilter.ATTR_TENANT);
        if (tenant == null) {
            // Defensive: should not happen if filter chain is correctly wired
            log.error("event=whoami_no_tenant path={}", exchange.getRequestURI().getPath());
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        // Execute within a GUC-stamped transaction — proves the withTenant path
        String body = tenantScope.withTenant(tenant, ctx -> {
            // SELECT current_setting proves GUC was applied
            String stamped = ctx.fetchOne("SELECT current_setting('nexus.tenant', true)")
                                .get(0, String.class);
            try {
                return MAPPER.writeValueAsString(Map.of(
                    "tenant", tenant,
                    "guc_tenant", stamped != null ? stamped : ""
                ));
            } catch (Exception e) {
                throw new RuntimeException("JSON serialization failed", e);
            }
        });

        HttpUtil.send(exchange, 200, body);
    }
}
