package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.32.3 — token lifecycle admin endpoints. The consumer is the
 * {@code nx tenant} / {@code nx service token} CLI; all SQL lives in {@link TokenStore}.
 *
 * <p>Routes (all POST, all behind {@link AuthFilter} so the caller is authenticated):
 * <ul>
 *   <li>{@code /v1/tenants/create}        {name}                     → mint a tenant's first token</li>
 *   <li>{@code /v1/service-tokens/issue}  {tenant,label?,ttl_seconds?} → issue a bound token</li>
 *   <li>{@code /v1/service-tokens/rotate} {tenant,grace_seconds?}     → zero-downtime overlap rotate</li>
 *   <li>{@code /v1/service-tokens/revoke} {selector}                  → revoke + invalidate cache</li>
 *   <li>{@code /v1/service-tokens/list}   {tenant?}                   → list rows (never plaintext)</li>
 * </ul>
 *
 * <p>The raw token is generated server-side and returned ONCE in the issue/rotate/create
 * response; only its hash is stored. {@code tenant_id='*'} is rejected (the wildcard is the
 * bootstrap sentinel — bead nexus-45ykb). Revoke calls {@link TokenCache#invalidate} on the
 * LIVE cache for immediate effect; rotate likewise invalidates the grace-expiring hashes so
 * their cache entries re-read the new deadline, bounding old-token over-validity to one
 * cache-TTL window rather than leaving it unbounded. Rotate is atomic (one transaction), so a
 * crash cannot strand a tenant with zero live tokens.
 *
 * <p>AUTHORIZATION (nexus-e4130): the root token (the operator credential, resolved
 * server-side via {@code ROOT_TOKEN_LABEL} and surfaced as {@link RequestContext#isOperator()})
 * may administer any tenant. Every ordinary tenant token is confined to its OWN tenant:
 * <ul>
 *   <li>{@code /v1/tenants/create} — OPERATOR ONLY (creating a new tenant is inherently
 *       cross-tenant; a tenant-bound token cannot provision a different tenant).</li>
 *   <li>{@code issue} / {@code rotate} — the body {@code tenant} must equal the caller's
 *       bound tenant (else 403), unless the caller is the operator.</li>
 *   <li>{@code list} — a non-operator is force-scoped to its own tenant (any body
 *       {@code tenant} is ignored), so it cannot enumerate another tenant's tokens.</li>
 *   <li>{@code revoke} — a non-operator's selector is resolved only within its own tenant
 *       (a selector matching only another tenant's token reads as "not found"), so it can
 *       neither revoke nor probe for another tenant's tokens.</li>
 * </ul>
 */
public final class TokenAdminHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(TokenAdminHandler.class);

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule())
        .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
        .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
        .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};
    private static final long DEFAULT_GRACE_SECONDS = 300L;

    private final TokenStore store;
    private final TokenCache cache;
    private final Clock clock;

    public TokenAdminHandler(TokenStore store, TokenCache cache, Clock clock) {
        this.store = store;
        this.cache = cache;
        this.clock = clock;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (RequestContext.tenant() == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: auth principal not set\"}");
            return;
        }
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        if (!"POST".equals(method)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        try {
            switch (path) {
                case "/v1/tenants/create"        -> handleTenantCreate(exchange);
                case "/v1/service-tokens/issue"  -> handleIssue(exchange);
                case "/v1/service-tokens/rotate" -> handleRotate(exchange);
                case "/v1/service-tokens/revoke" -> handleRevoke(exchange);
                case "/v1/service-tokens/list"   -> handleList(exchange);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=token_admin_bad_request path={} error={}", path, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=token_admin_error path={}", path, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    private void handleTenantCreate(HttpExchange ex) throws IOException {
        // nexus-e4130: provisioning a NEW tenant is inherently cross-tenant — only the
        // operator (root) token may do it; a tenant-bound token cannot.
        if (!requireOperator(ex)) {
            return;
        }
        Map<String, Object> body = readBody(ex);
        String name = requireString(body, "name");
        TokenStore.IssuedToken issued = store.issueToken(name, "tenant-create-initial", null);
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "tenant", name, "token", issued.rawToken(), "token_hash", issued.tokenHash()))));
    }

    private void handleIssue(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String tenant = requireString(body, "tenant");
        if (!authorizedForTenant(ex, tenant)) {  // nexus-e4130: self-scope non-operators
            return;
        }
        String label = optString(body, "label");
        Long ttl = optLong(body, "ttl_seconds");
        TokenStore.IssuedToken issued = store.issueToken(tenant, label, ttl);
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "tenant", tenant, "token", issued.rawToken(), "token_hash", issued.tokenHash()))));
    }

    private void handleRotate(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String tenant = requireString(body, "tenant");
        if (!authorizedForTenant(ex, tenant)) {  // nexus-e4130: self-scope non-operators
            return;
        }
        Long grace = optLong(body, "grace_seconds");
        TokenStore.RotationResult result =
            store.rotateTokens(tenant, grace == null ? DEFAULT_GRACE_SECONDS : grace);
        // Invalidate the grace-expiring tokens so their cache entries re-read the new
        // deadline: they stay valid through the grace window then expire precisely at it.
        result.expiredHashes().forEach(cache::invalidate);
        TokenStore.IssuedToken issued = result.issued();
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "tenant", tenant, "token", issued.rawToken(), "token_hash", issued.tokenHash()))));
    }

    private void handleRevoke(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String selector = requireString(body, "selector");
        // nexus-e4130: a non-operator may only revoke within its own tenant. Passing the
        // tenant scope into selector resolution means another tenant's prefix reads as
        // "not found" — no cross-tenant revoke AND no existence probe.
        String scope = RequestContext.isOperator() ? null : RequestContext.tenant();
        var revokedHash = store.revokeToken(selector, scope);
        revokedHash.ifPresent(cache::invalidate);  // immediate effect on the live cache
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("revoked", revokedHash.isPresent());
        revokedHash.ifPresent(h -> resp.put("token_hash", h));
        HttpUtil.send(ex, 200, json(resp));
    }

    private void handleList(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String tenant = optString(body, "tenant");
        // nexus-e4130: a non-operator is force-scoped to its own tenant; any body 'tenant'
        // is ignored so it cannot enumerate another tenant's tokens (or probe existence).
        if (!RequestContext.isOperator()) {
            tenant = RequestContext.tenant();
        }
        var now = clock.instant();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (TokenStore.TokenInfo t : store.listTokens(tenant)) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("token_hash", t.tokenHash());
            row.put("tenant", t.tenantId());
            row.put("label", t.label());
            row.put("created_at", t.createdAt());
            row.put("expires_at", t.expiresAt());
            row.put("revoked_at", t.revokedAt());
            row.put("status", t.status(now));
            rows.add(row);
        }
        HttpUtil.send(ex, 200, json(Map.of("tokens", rows)));
    }

    // ── Authorization (nexus-e4130) ──────────────────────────────────────────────

    /**
     * Allow only the operator (root) token. Sends 403 and returns false otherwise.
     * Used by routes that are inherently cross-tenant (tenant create).
     */
    private boolean requireOperator(HttpExchange ex) throws IOException {
        if (RequestContext.isOperator()) {
            return true;
        }
        log.debug("event=token_admin_denied reason=operator_required tenant={}",
                  RequestContext.tenant());
        HttpUtil.send(ex, 403, json(Map.of(
            "error", "operator privilege required: this operation needs the root token")));
        return false;
    }

    /**
     * Allow the operator, or a tenant token acting on its OWN tenant. Sends 403 and
     * returns false when a non-operator targets a different tenant.
     */
    private boolean authorizedForTenant(HttpExchange ex, String tenant) throws IOException {
        String caller = RequestContext.tenant();
        if (RequestContext.isOperator() || tenant.equals(caller)) {
            return true;
        }
        log.debug("event=token_admin_denied reason=cross_tenant caller={} requested={}",
                  caller, tenant);
        HttpUtil.send(ex, 403, json(Map.of(
            "error", "forbidden: token bound to tenant '" + caller
                + "' may not administer tenant '" + tenant + "'")));
        return false;
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            return Map.of();
        }
        return MAPPER.readValue(new String(bytes, StandardCharsets.UTF_8), MAP_TYPE);
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("missing required string field: " + key);
        }
        return s;
    }

    private static String optString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        return v instanceof String s && !s.isBlank() ? s : null;
    }

    private static Long optLong(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new IllegalArgumentException("field must be a number: " + key);
    }

    private static String json(Object value) {
        try {
            return MAPPER.writeValueAsString(value);
        } catch (Exception e) {
            return "{\"error\":\"serialization failed\"}";
        }
    }
}
